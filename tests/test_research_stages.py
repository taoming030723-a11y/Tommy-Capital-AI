"""Research visibility, gated execution and prospective measurement invariants."""
import copy
from datetime import date, datetime

import pandas as pd
import pytest

from tommy_capital.coverage import technical_results, is_history_shortfall
from tommy_capital.data import DataError, SHANGHAI, daily_history
from tommy_capital.scoring import balanced_score, ranked_top20, confirmed_ranking_eligible
from tommy_capital.research import freeze_snapshot, evaluate_snapshot, validate_snapshot
from tommy_capital.review import review_report
from test_balanced_top20 import full_technical, frames, report_fixture
from test_core import good_row, candles


@pytest.mark.parametrize("missing", ["monthly", "weekly", "daily", "volume"])
def test_short_periods_cannot_change_unconfirmed_research_rank(missing):
    tech = full_technical()
    if missing == "monthly":
        tech["monthly_recovery"]["previous_j"] = tech["monthly_j"] + 1
    elif missing == "weekly":
        tech["weekly_trend"] = False
    elif missing == "daily":
        tech["daily_trend"] = False
    else:
        tech["daily_volume"]["abnormal"] = False
        tech["first_volume_breakout"]["active"] = False
    weak = frames()
    for frame in weak.values():
        frame.update(signals=[], above_ma20=False)
        frame["structure"]["ma20_rising"] = False
        frame["macd"].update(dif=0, dea=0, histogram=0, previous_histogram=0)
    strongest = balanced_score(good_row(), tech, "A", minute_frames=frames())
    weakest = balanced_score(good_row(), tech, "A", minute_frames=weak)
    assert strongest["score"] == weakest["score"]
    assert strongest["score_inputs_complete"]
    for period in ("60", "30", "15"):
        assert strongest["score_breakdown"]["technical"][period]["raw_score"] > 0
        assert strongest["score_breakdown"]["technical"][period]["score"] == 0
    row = {"code": "000001", "eligible": True, "strategic_eligible": False,
           "route": "A", "technical": tech, **strongest}
    assert ranked_top20([row]) == [row]
    assert not confirmed_ranking_eligible(row)


@pytest.mark.parametrize("sector", ["银行Ⅱ", "证券Ⅱ", "保险Ⅱ", "多元金融"])
def test_financial_risk_missing_never_earns_generic_cash_full_points(sector):
    row = {**good_row(), "industry": sector, "cfo_ttm": 1000000}
    scored = balanced_score(row, full_technical(), "A", minute_frames=frames())
    assert scored["score_breakdown"]["fundamental"]["cash_quality"] == 0
    assert not scored["score_inputs_complete"]
    assert not scored["fundamental_quality"]["cash_conversion_applicable"]
    assert "fundamental.financial_sector_risk_review" in scored["score_missing_inputs"]


@pytest.mark.parametrize("review_day", [8, 11])
def test_model_review_preserves_real_inputs_and_cannot_claim_new_scan(review_day):
    source = report_fixture(25)
    before = copy.deepcopy(source)
    now = datetime(2026, 10, review_day, 21, tzinfo=SHANGHAI)
    reviewed = review_report(source, now, "a"*64)
    assert source == before
    assert reviewed["scan_type"] == reviewed["scan_phase"] == "model_review"
    assert reviewed["session"] == "2026-10-08"
    assert reviewed["model_review"]["new_market_requests"] == 0
    assert reviewed["errors"] == before["errors"]
    originals = {r["code"]: r for r in before["rankings"]}
    for row in reviewed["rankings"]:
        old = originals[row["code"]]
        assert row["technical"] == old["technical"] and row["minute_frames"] == old["minute_frames"]
        assert row["financial_routes"] == old["financial_routes"]
    with pytest.raises(ValueError, match="真实收盘"):
        review_report(reviewed, now, "a"*64)


def test_stale_combined_history_error_is_retried_without_retrying_proven_short_history():
    old = "日线不足250根，或最新K线未覆盖已收盘交易日"
    assert not is_history_shortfall(old)
    assert is_history_shortfall("日线历史不足250根（实际99根）")
    calls = []
    def inspect(row, fresh):
        calls.append(fresh)
        if not fresh:
            raise DataError(old)
        return row, None
    result = list(technical_results([{"code": "000001"}], inspect, 1))
    assert calls == [False, True] and result[0][1]["code"] == "000001"


def test_opening_filters_forming_day_and_stale_primary_still_uses_backup():
    class Provider:
        def fetch(self, function, **kwargs):
            close = [8, 9] if function == "daily_tx_recent" else [8, 10, 99]
            dates = ["2026-10-07", "2026-10-08"] if len(close) == 2 else ["2026-10-07", "2026-10-09", "2026-10-12"]
            return pd.DataFrame({"日期": dates, "收盘": close})
    raw, warning = daily_history(Provider(), "000001", None, "20261009", "20261012", required_session=date(2026,10,9))
    assert raw["收盘"].tolist() == [8, 10] and warning


def snapshot_fixture():
    report = report_fixture(1)
    report.update(ranking_complete=False, finished_at="2026-10-09T20:51:00+08:00", session="2026-10-09")
    return freeze_snapshot(report)


CALENDAR = pd.to_datetime(["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-12-31"])


def prices(code, start, end):
    return pd.DataFrame({"日期": CALENDAR[1:6], "开盘": [100.]*5, "收盘": [110.]*5,
                         "最高": [111.]*5, "最低": [99.]*5})


def test_forecast_requires_future_prices_and_uses_next_open_plus_both_costs():
    snapshot = snapshot_fixture()
    def no_prices(*args):
        pytest.fail("Unmatured forecasts must not fetch or manufacture future prices")
    pending = evaluate_snapshot(snapshot, CALENDAR, date(2026,10,9), no_prices)
    assert all(r["status"] == "pending" for r in pending)
    observed = evaluate_snapshot(snapshot, CALENDAR, date(2026,10,16), prices, 20)
    first = observed[0]
    assert first["entry_session"] == first["exit_session"] == "2026-10-12"
    assert first["gross_price_return_pct"] == pytest.approx(10.)
    assert first["hypothetical_net_return_pct"] == pytest.approx((110*.998/(100*1.002)-1)*100)
    assert observed[1]["status"] == "observed" and observed[2]["status"] == "pending"


@pytest.mark.parametrize("problem", ["missing_session", "duplicate", "single_price", "invalid_close", "invalid_high", "invalid_range"])
def test_unevaluable_or_unexecutable_windows_never_become_observed_returns(problem):
    def bad_prices(code, start, end):
        frame = prices(code,start,end)
        if problem == "missing_session":
            return frame.iloc[1:]
        if problem == "duplicate":
            return pd.concat([frame,frame.iloc[:1]])
        if problem == "single_price":
            frame.loc[0,"最低"] = frame.loc[0,"最高"]
        if problem == "invalid_close":
            frame.loc[0,"收盘"] = float("inf")
        if problem == "invalid_high":
            frame.loc[0,"最高"] = float("nan")
        if problem == "invalid_range":
            frame.loc[0,"最高"] = 95.
        return frame
    observed = evaluate_snapshot(snapshot_fixture(), CALENDAR, date(2026,10,12), bad_prices)
    assert observed[0]["status"] == "missing" and "gross_price_return_pct" not in observed[0]


def test_snapshot_tampering_and_historical_decision_backdating_are_rejected():
    snapshot = snapshot_fixture()
    snapshot["top20"][0]["score"] += 1
    with pytest.raises(ValueError, match="改写"):
        validate_snapshot(snapshot)
    report = report_fixture(1)
    report.update(ranking_complete=False, finished_at="2026-10-11T14:00:00+08:00", session="2026-10-09", scan_phase="model_review")
    snapshot = freeze_snapshot(report)
    assert snapshot["decision_at"].startswith("2026-10-11") and snapshot["data_session"] == "2026-10-09"
    assert evaluate_snapshot(snapshot, CALENDAR, date(2026,10,9), prices)[0]["status"] == "pending"
