"""Synthetic fixtures validate math and control flow, never serve as live inputs."""
import argparse
import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from tommy_capital.cli import DEFAULTS, clean, run
from tommy_capital.data import (DataError, Provider, SHANGHAI, completed_session,
                               latest_finance, normalize_spot, normalize_finance)
from tommy_capital.strategy import base_checks, evaluate, prepare_universe
from tommy_capital.technical import aggregate, bars, confirmed_divergences, indicators, tactical


def history_frame():
    records = []
    for period, profit, eps, growth in [("2025-06-30", 40, .4, 5), ("2025-12-31", 100, 1.0, 10),
                                        ("2026-03-31", 30, .3, 15), ("2026-06-30", 60, .6, 20)]:
        records.append({"code": "000001", "period": pd.Timestamp(period), "profit_ytd": profit,
                        "eps_ytd": eps, "profit_yoy": growth})
    return pd.DataFrame(records)


def good_row():
    return {"code": "000001", "name": "测试公司", "price": 12, "market_cap": 2400,
            "turnover": 50_000_000, "profit_ytd": 60, "profit_ttm": 120, "revenue_yoy": 10,
            "profit_yoy": 20, "cfo_per_share_ytd": .5, "previous_same_cfo_per_share_ytd": .4,
            "revenue_ytd": 1000, "revenue_ttm": 2000, "pe_ttm": 20, "pb": 2,
            "industry_pe_samples": 12, "industry_pe_ratio": .8,
            "period": pd.Timestamp("2026-06-30"), "fundamental_improving": True}


def good_technical():
    return {"daily_trend": True, "weekly_trend": True, "monthly_trend": True,
            "monthly_j": 10, "volume_ratio20": 2,
            "monthly_recovery": {"previous_j": 5, "min_j_3_months": 5, "min_j_6_months": 5},
            "weekly_structure": {"confirmed": True},
            "weekly_base": {"detected": True}, "daily_volume": {"abnormal": True}}


def candles(count=80):
    price = np.arange(count, dtype=float) + 100
    return pd.DataFrame({"open": price, "close": price, "high": price + 1,
                         "low": price - 1, "volume": np.ones(count)},
                        index=pd.date_range("2026-01-01", periods=count, freq="h"))


def test_ttm_uses_cumulative_bridge_and_missing_stays_missing():
    frame = history_frame()
    actual = latest_finance(frame).iloc[0]
    assert actual.profit_ttm == 120
    assert actual.eps_ttm_approx == pytest.approx(1.2)
    assert actual.fundamental_improving
    assert pd.isna(latest_finance(frame[frame.period != pd.Timestamp("2025-06-30")]).iloc[0].profit_ttm)


def test_annual_ttm_does_not_double_count():
    result = latest_finance(history_frame().iloc[:2]).iloc[0]
    assert result.profit_ttm == 100


def test_missing_adjacent_report_cannot_claim_improvement():
    history = history_frame()
    history = history[history.period != pd.Timestamp("2026-03-31")]
    assert not latest_finance(history).iloc[0].fundamental_improving


@pytest.mark.parametrize('daily_only', [True, False])
@pytest.mark.parametrize('monthly_low', [True, False])
def test_end_to_end_pipeline_emits_candidate_with_source_lineage(tmp_path, daily_only, monthly_low):
    from tommy_capital.data import FINANCE_COLUMNS
    class RecordedProvider:
        """Synthetic integration fixture; deliberately not a live-data claim."""
        lineage = []
        def fetch(self, function, **kwargs):
            self.lineage.append({"function": function, "test_fixture": True})
            if function == "tool_trade_date_hist_sina":
                return pd.DataFrame({"trade_date": ["2026-09-30", "2026-10-08", "2026-12-31"]})
            if function == "spot_sina_full":
                return pd.DataFrame({"代码": [f"60000{i}" for i in range(1, 7)], "名称": ["测试"]*6,
                                     "最新价": [20]*6, "总市值": [2400]*6, "成交额": [5e7]*6,
                                     "市净率": [2]*6, "市盈率-动态": [99]*6, "行情时刻": ['09:49:59']*6})
            if function == "finance_em_named":
                period = kwargs["date"]
                # Not-yet-announced September result is empty, not fabricated.
                if period == "20260930":
                    return pd.DataFrame()
                source = {key: [20]*6 for key in FINANCE_COLUMNS}
                net = 100 if period.endswith("1231") else (60 if period == "20260630" else 40)
                source.update({"股票代码": [f"60000{i}" for i in range(1, 7)], "股票简称": ["测试"]*6,
                               "最新公告日期": [str(pd.Timestamp(period) + pd.Timedelta(days=20))]*6,
                               "所处行业": ["测试行业"]*6, "净利润-净利润": [net]*6,
                               "每股收益": [net/100]*6, "每股经营现金流量": [.5]*6})
                return pd.DataFrame(source)
            if function == "daily_tx_recent":
                dates = pd.bdate_range("2018-01-01", "2026-09-30")
                prices = np.linspace(10, 80, len(dates))
                if monthly_low:
                    prices[-126:] = np.linspace(75, 35, 126)
                # A provider may expose the in-progress day: it must not enter
                # the completed-day volume baseline or monthly observation.
                dates = dates.append(pd.DatetimeIndex(["2026-10-08"]))
                prices = np.append(prices, 90)
                frame = pd.DataFrame({"日期": dates, "开盘": prices, "收盘": prices, "最高": prices+1,
                                     "最低": prices-1, "成交量": [100]*(len(dates)-1)+[100000], "成交额": [5e7]*len(dates)})
                return frame[frame['日期'] <= pd.Timestamp(kwargs['end_date'])] if kwargs.get('adjust') == '' else frame
            if function == "monthly_tx_qfq":
                raw = self.fetch("daily_tx_recent", end_date=kwargs["end_date"], adjust="qfq")
                monthly = aggregate(bars(raw), "ME", pd.Timestamp(kwargs["end_date"]).date()).reset_index()
                return monthly.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                    "high": "最高", "low": "最低", "volume": "成交量"})
            if function == 'minute_sina_raw':
                assert monthly_low and not daily_only and kwargs['period'] in ['15', '30', '60']
                period = int(kwargs['period'])
                times = [pd.Timestamp(day) + pd.Timedelta(hours=h, minutes=m)
                         for day in pd.bdate_range('2026-08-01','2026-09-30')
                         for h,m in [(9,45),(10,0),(10,15),(10,30),(10,45),(11,0),(11,15),(11,30),
                                     (13,15),(13,30),(13,45),(14,0),(14,15),(14,30),(14,45),(15,0)]
                         if ((h * 60 + m - (9 * 60 + 30 if h < 12 else 13 * 60)) % period == 0)]
                if period == 15:
                    times += [pd.Timestamp('2026-10-08 09:45'), pd.Timestamp('2026-10-08 10:00')]
                values = np.linspace(10, 20, len(times))
                return pd.DataFrame({'时间':times, '开盘':values, '收盘':values, '最高':values+1, '最低':values-1, '成交量':[100]*len(times)})
            pytest.fail('unexpected provider call')
    args = argparse.Namespace(config=None, holdings=None, refresh=False, limit=1, daily_only=daily_only, output=str(tmp_path))
    assert run(args, provider=RecordedProvider(), now=datetime(2026, 10, 8, 9, 50, tzinfo=SHANGHAI)) == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "partial" and not report["scan_complete"]
    assert report["coverage"]["unscanned"] == 5
    assert len(report["rankings"]) == int(monthly_low)
    assert report["selection_rule"] == "dual_route_monthly_recovery"
    card = report["rankings"][0] if monthly_low else report["observations"][0]
    assert (card["technical"]["monthly_j"] < 20) is monthly_low
    assert not card["strategic_eligible"]
    assert card["pe_ttm"] == 20
    assert card["technical"]["daily_volume"]["confirmed_at"] == "2026-09-30"
    assert card["technical"]["daily_volume"]["ratio20"] == 1
    assert card["tactical"]["status"] == ('not_requested' if daily_only else 'not_held')
    if not daily_only and monthly_low:
        assert report['rankings'][0]['minute15']['last_bar'] == '2026-10-08T09:45:00'
        assert report['coverage']['minute15_completed'] == 1
        assert report['coverage']['minute60_completed'] == 1
        assert report['coverage']['minute30_completed'] == 1
        assert card['minute_frames']['60']['last_bar'] == '2026-09-30T15:00:00'
        assert not card['t_trend_confirmed']  # Every pool member is still scored.
        assert card['score'] == pytest.approx(card['fundamental_score'] + card['technical_score'])
        assert report['session'] == '2026-09-30'
    if not monthly_low:
        assert report["coverage"]["minute15_requested"] == 0
        assert card["minute15"]["status"] == "not_requested"
    assert report["lineage"]


def test_staleness_missing_and_negative_earnings_excluded():
    today = date(2026, 10, 5)
    assert base_checks(good_row(), DEFAULTS, today) == []
    for field, val in [("profit_ttm", np.nan), ("cfo_per_share_ytd", None), ("profit_ytd", -1),
                       ("industry_pe_samples", 2), ("period", pd.Timestamp("2024-12-31"))]:
        row = good_row(); row[field] = val
        assert base_checks(row, DEFAULTS, today)


def test_low_monthly_j_enters_observation_without_overriding_trend_confirmation():
    technical = good_technical(); technical.update(monthly_trend=False, monthly_j=10)
    result = evaluate(good_row(), technical, DEFAULTS, date(2026, 10, 5))
    assert result["monthly_low_j_watch"]
    assert result["eligible"]
    assert result["strategic_eligible"] and not result["t_trend_confirmed"]
    assert "monthly_trend 未确认" in result["t_reasons"]


def test_small_timeframe_only_runs_on_eligible_holdings():
    class NoCalls:
        def fetch(self, *a, **k):
            pytest.fail("should never call minute provider")
    provider = NoCalls()
    assert tactical(provider, "000001", True, False, date(2026, 9, 30))["status"] == "not_held"
    assert tactical(provider, "000001", False, True, date(2026, 9, 30))["status"] == "strategy_not_passed"


def test_complete_periods_exclude_unfinished_month_and_week():
    daily = candles(60); daily.index = pd.date_range("2026-08-01", periods=60, freq="D")
    assert aggregate(daily, "ME", date(2026, 9, 15)).index[-1] == pd.Timestamp("2026-08-31")
    assert aggregate(daily, "W-FRI", date(2026, 9, 15)).index[-1] <= pd.Timestamp("2026-09-15")


def test_calendar_holiday_and_before_close():
    calendar = pd.DataFrame({"trade_date": ["2026-09-30", "2026-10-09", "2026-12-31"]})
    assert completed_session(calendar, datetime(2026, 10, 5, 21, 0, tzinfo=SHANGHAI)) == date(2026, 9, 30)
    assert completed_session(calendar, datetime(2026, 10, 9, 14, 0, tzinfo=SHANGHAI)) == date(2026, 9, 30)
    with pytest.raises(DataError):
        completed_session(pd.DataFrame({"trade_date": ["2025-12-31"]}), datetime(2026, 10, 5, tzinfo=SHANGHAI))


def test_ttm_pe_not_dynamic_and_symbol_filter():
    raw = pd.DataFrame({"代码": [1, "600001", "900001", "920001", "302132"], "名称": ["A", "B", "C", "D", "E"],
                        "最新价": [12]*5, "总市值": [2400]*5, "成交额": [5e7]*5,
                        "市净率": [2]*5, "市盈率-动态": [99]*5})
    normalized = normalize_spot(raw)
    assert set(normalized.code) == {"000001", "600001", "920001", "302132"}
    finance = pd.DataFrame({"code": ["000001", "600001"], "name": ["A", "B"],
                            "profit_ttm": [120, 120], "industry": ["测试", "测试"]})
    universe = prepare_universe(normalized, finance, DEFAULTS)
    assert universe.pe_ttm.iloc[0] == 20
    assert universe.pe_dynamic.iloc[0] == 99


def test_unknown_and_future_announcement_never_used():
    from tommy_capital.data import FINANCE_COLUMNS
    source = {c: [1, 1] for c in FINANCE_COLUMNS}
    source.update({"股票代码": ["000001", "000002"], "最新公告日期": [None, "2026-10-10"]})
    assert normalize_finance(pd.DataFrame(source), date(2026, 6, 30), date(2026, 10, 5)).empty


def test_flat_kdj_is_finite_and_warmup_explicit():
    frame = candles(); frame[["open", "close", "high", "low"]] = 100
    output = indicators(frame)
    assert output.j.iloc[:8].isna().all()
    assert output.j.iloc[-1] == 50
    assert output.dif.iloc[-1] == 0


def test_pivots_require_following_bars():
    frame = candles()
    frame.loc[frame.index[-2], "low"] = 1
    result = confirmed_divergences(frame)
    assert result["signals"] == []


def test_malformed_prices_and_schema_are_rejected():
    raw = pd.DataFrame({"日期": ["2026-09-30"], "开盘": [10], "收盘": [10],
                        "最高": [9], "最低": [8], "成交量": [1]})
    with pytest.raises(DataError):
        bars(raw)
    with pytest.raises(DataError):
        normalize_spot(pd.DataFrame({"代码": ["000001"]}))


def test_live_failure_generates_diagnostic_and_nonzero_exit(tmp_path):
    class Broken:
        lineage = []
        def fetch(self, *a, **k):
            raise DataError("network unavailable")
    args = argparse.Namespace(config=None, holdings=None, refresh=False, limit=5, output=str(tmp_path))
    assert run(args, provider=Broken(), now=datetime(2026, 10, 5, 21, 0, tzinfo=SHANGHAI)) == 2
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["mode"] == "live" and report["status"] == "failed"
    assert report["rankings"] == [] and not report["scan_complete"]
    assert (tmp_path / "report.html").exists()


def test_timeout_has_no_demo_fallback(tmp_path, monkeypatch):
    import subprocess
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("test", 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(DataError, match="未使用模拟数据"):
        Provider(cache=tmp_path, timeout=1, retries=1).fetch("stock_zh_a_spot_em")
    assert not list(tmp_path.glob("*.json"))


def test_expired_cache_cannot_hide_network_failure(tmp_path, monkeypatch):
    import time
    import subprocess
    from types import SimpleNamespace
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=pd.DataFrame({"x": [1]}).to_json(orient="split")))
    provider = Provider(cache=tmp_path, retries=1)
    provider.fetch("stock_zh_a_spot_em")
    cache_file = next(tmp_path.glob("*.json"))
    content = json.loads(cache_file.read_text()); content["fetched_epoch"] = time.time() - 500
    cache_file.write_text(json.dumps(content))
    def broken(*a, **k):
        raise subprocess.TimeoutExpired("test", 1)
    monkeypatch.setattr(subprocess, "run", broken)
    with pytest.raises(DataError):
        provider.fetch("stock_zh_a_spot_em", ttl=1)


def test_json_null_instead_of_nan():
    assert json.dumps(clean({"a": np.nan, "b": np.bool_(True)}), allow_nan=False) == '{"a": null, "b": true}'


def test_daily_fallback_keeps_real_source_and_column_mapping():
    from tommy_capital.data import daily_history
    class Backup:
        def fetch(self, function, **kwargs):
            if function == "daily_tx_recent":
                raise DataError("primary unavailable")
            assert function == "stock_zh_a_hist"
            return candles().reset_index(names="日期").rename(columns={"open":"开盘", "close":"收盘", "high":"最高", "low":"最低", "volume":"成交量"})
    frame, warning = daily_history(Backup(), "600001", "20180101", "20260930")
    assert warning and "腾讯" in warning
    assert len(bars(frame)) == 80


def test_daily_failure_preserves_all_provider_causes():
    from tommy_capital.data import daily_history
    class AllMissing:
        def fetch(self,function,**kwargs):
            raise DataError({'daily_tx_recent':'requested qfq missing', 'stock_zh_a_hist':'connection closed',
                             'daily_sina_adjusted':'missing real factor'}[function])
    with pytest.raises(DataError,match='requested qfq missing.*connection closed.*missing real factor'):
        daily_history(AllMissing(),'920193','20180101','20260930')


def test_daily_third_source_keeps_requested_adjustment_and_warnings():
    from tommy_capital.data import daily_history
    calls=[]
    class ThirdSource:
        def fetch(self,function,**kwargs):
            calls.append((function,kwargs))
            assert kwargs['adjust']=='qfq'
            if function!='daily_sina_adjusted':
                raise DataError(function+' unavailable')
            return candles().reset_index(names='日期').rename(columns={'open':'开盘','close':'收盘',
                                    'high':'最高','low':'最低','volume':'成交量'})
    frame, warning=daily_history(ThirdSource(),'600001','20180101','20260930','20261007')
    assert len(bars(frame))==80
    assert [c[0] for c in calls]==['daily_tx_recent','stock_zh_a_hist','daily_sina_adjusted']
    assert calls[-1][1]['anchor_date']=='20261007' and '腾讯' in warning and '东财' in warning and '新浪' in warning
