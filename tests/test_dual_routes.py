"""Offline causal/evidence/permission fixtures; never actual market reports."""
import copy
import json
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tommy_capital.cli import DEFAULTS, run
from tommy_capital.data import SHANGHAI, latest_finance
from tommy_capital.inflection import load_evidence, leading_check, valuation_check
from tommy_capital.reporting import render_markdown
from tommy_capital.strategy import base_checks, evaluate
from tommy_capital import technical
from test_core import good_row, good_technical

TODAY = date(2026, 10, 7)


def leading_evidence():
    return {"facts": {"new_product_mass_delivery": True,
                      "future_catalyst": {"description": "测试中的未来交付事件", "expected_date": "2026-12-01"}},
            "verified_ids": ["delivery", "catalyst"], "records": [],
            "valuation_model": {"method": "normalized_forward_profit", "reviewed": True,
                "as_of": "2026-10-07", "forecast_year": 2027, "source_ids": ["delivery", "catalyst"],
                "assumption_basis": "测试情景假设，不是市场事实",
                "scenarios": {"bear": {"net_profit_cny": 180, "pe": 15},
                              "base": {"net_profit_cny": 200, "pe": 20},
                              "bull": {"net_profit_cny": 300, "pe": 20}}}}


def losing_row():
    return {**good_row(), "profit_ytd": -100, "profit_ttm": -300, "profit_yoy": -20,
            "revenue_yoy": 14.31, "cfo_per_share_ytd": -1, "pe_ttm": np.nan,
            "gross_margin": 10, "previous_same_gross_margin": 8, "leading_evidence": leading_evidence()}


def recovering_technical():
    return {**good_technical(), "monthly_j": 27,
            "monthly_recovery": {"previous_j": 19, "min_j_3_months": 12, "min_j_6_months": 8},
            "price_compression": {"experienced": True},
            "first_volume_breakout": {"active": True}}


def test_monthly_path_recovers_past20_without_raising_j_threshold():
    result = evaluate(good_row(), recovering_technical(), DEFAULTS, TODAY)
    assert result["eligible"] and result["route"] == "A" and result["monthly_recovery_watch"]
    assert not result["monthly_low_j_watch"] and result["strategic_eligible"]
    for recovery in [{"previous_j": 28, "min_j_3_months": 12},
                     {"previous_j": 19, "min_j_3_months": 21}, {}]:
        tech = {**recovering_technical(), "monthly_recovery": recovery}
        assert not evaluate(good_row(), tech, DEFAULTS, TODAY)["eligible"]


def test_current_low_j_enters_watch_but_falling_j_cannot_confirm_strategy():
    tech = {**recovering_technical(), "monthly_j": 12,
            "monthly_recovery": {"previous_j": 19, "min_j_3_months": 12, "min_j_6_months": 8}}
    result = evaluate(good_row(), tech, DEFAULTS, TODAY)
    assert result["eligible"] and not result["strategic_eligible"] and not result["t_trend_confirmed"]


def test_complete_monthly_history_and_recovery_only_uses_six_completed_bars():
    monthly = pd.DataFrame({"j": [8, 12, 19, 19, 29, 35]}, index=pd.date_range("2026-04-30", periods=6, freq="ME"))
    result = technical.monthly_recovery_observation(monthly)
    assert result["route_a_path"] is True and result["route_b_path"] is True
    monthly.loc[monthly.index[-3:], "j"] = [27, 29, 35]
    result = technical.monthly_recovery_observation(monthly)
    assert not result["route_a_path"] and result["route_b_path"]


def test_negative_profit_branch_requires_all_evidence_and_valuation():
    row = losing_row()
    assert base_checks(row, DEFAULTS, TODAY)
    result = evaluate(row, recovering_technical(), DEFAULTS, TODAY)
    assert result["eligible"] and result["route"] == "B" and result["strategic_eligible"]
    assert result["leading"]["profit_label"] == "盈利尚未兑现"
    row["leading_evidence"]["valuation_model"] = None
    result = evaluate(row, recovering_technical(), DEFAULTS, TODAY)
    assert not result["eligible"] and any("估值" in r for r in result["reasons"])


@pytest.mark.parametrize("problem", ["no_delivery", "past_catalyst", "unknown_date", "no_core", "no_sources", "unreviewed", "bad_margin", "future_model", "too_high_pe", "no_compression", "missing_ttm"])
def test_leading_route_never_turns_unknown_or_expensive_data_into_pass(problem):
    row, tech = losing_row(), recovering_technical()
    evidence = row["leading_evidence"]
    if problem == "no_delivery":
        evidence["facts"].pop("new_product_mass_delivery")
    elif problem == "past_catalyst":
        evidence["facts"]["future_catalyst"]["expected_date"] = "2026-08-05"
    elif problem == "unknown_date":
        evidence["facts"]["future_catalyst"].pop("expected_date")
    elif problem == "no_core":
        row["previous_same_gross_margin"] = np.nan
    elif problem == "no_sources":
        evidence["verified_ids"] = []
    elif problem == "unreviewed":
        evidence["valuation_model"]["reviewed"] = False
    elif problem == "bad_margin":
        row["market_cap"] = 3800
    elif problem == "future_model":
        evidence["valuation_model"]["as_of"] = "2026-10-09"
    elif problem == "too_high_pe":
        evidence["valuation_model"]["scenarios"]["base"]["pe"] = 61
    elif problem == "no_compression":
        tech["price_compression"]["experienced"] = False
    else:
        row["profit_ttm"] = None
    assert not evaluate(row, tech, DEFAULTS, TODAY)["eligible"]


def test_fifteen_minute_absence_cannot_veto_a_strategic_candidate():
    tech = {**recovering_technical(), "minute15": {"status": "ok", "signals": []}}
    result = evaluate(good_row(), tech, DEFAULTS, TODAY)
    assert result["eligible"] and result["strategic_eligible"]


def breakout_frame():
    index = pd.bdate_range(end="2026-09-30", periods=50)
    frame = pd.DataFrame({"open": 10., "close": 10., "high": 10.2, "low": 9.8, "volume": 100.}, index=index)
    frame.iloc[43] = [10.1, 10.9, 11., 10., 150.]
    frame.iloc[44:] = [11., 11.8, 12., 10.9, 250.]
    return frame


def test_first_breakout_uses_previous20_and_does_not_repeat_after_price_rise():
    frame = breakout_frame()
    result = technical.first_volume_breakout(frame)
    event = result["first_event"]
    assert result["event_count"] == 1 and result["active"]
    assert event["confirmed_at"] == frame.index[43].date().isoformat()
    assert event["ratio20"] == 1.5 and event["baseline_end"] == frame.index[42].date().isoformat()
    assert technical.first_volume_breakout(frame.iloc[:44])["first_event"] == event
    assert not technical.first_volume_breakout(frame.iloc[:43])["detected"]
    frame.iloc[-1, frame.columns.get_loc("close")] = 10.1
    assert technical.first_volume_breakout(frame)["state"] == "failed_platform"


def test_no_first_breakout_for_weak_close_or_insufficient_rvol():
    frame = breakout_frame().iloc[:44].copy()
    frame.iloc[-1, frame.columns.get_loc("volume")] = 149
    assert not technical.first_volume_breakout(frame)["detected"]
    frame.iloc[-1, frame.columns.get_loc("volume")] = 200
    frame.iloc[-1, frame.columns.get_loc("close")] = 10.4
    assert not technical.first_volume_breakout(frame)["detected"]


def test_cashflow_compares_same_prior_year_ytd_never_adjacent_cumulative_quarter():
    history = pd.DataFrame([
        {"code": "000001", "period": pd.Timestamp(p), "profit_ytd": 100, "eps_ytd": 1.,
         "profit_yoy": 10, "revenue_ytd": 1000, "gross_margin": 10, "cfo_per_share_ytd": cfo}
        for p, cfo in [("2025-06-30", .8), ("2025-12-31", 2.), ("2026-03-31", .1), ("2026-06-30", .5)]])
    row = latest_finance(history).iloc[0]
    assert row.previous_same_cfo_per_share_ytd == .8 and not row.cfo_not_deteriorating
    assert row.revenue_ttm == 1000
    assert any("上年同期" in r for r in base_checks({**good_row(), **row.to_dict()}, DEFAULTS, TODAY))


def test_official_evidence_is_refetched_and_content_failures_do_not_pass(tmp_path):
    ledger = {"schema_version": 1, "companies": {"600001": {"official_domains": ["example.com"],
        "facts": [{"id": "delivery", "field": "new_product_mass_delivery", "value": True,
                   "published_at": "2026-10-07", "url": "https://example.com/20261007/test",
                   "match_all": ["测试中的规模交付"]}]}}}
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(ledger))
    class Session:
        def __init__(self, text):
            self.text, self.calls = text, 0
        def get(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(content=self.text.encode(), text=self.text, apparent_encoding="utf-8", raise_for_status=lambda: None)
    source = Session("<p>测试中的规模交付</p>")
    evidence, stats = load_evidence(path, TODAY, source)
    assert source.calls == 1 and evidence["600001"]["facts"]["new_product_mass_delivery"] is True
    assert stats["lineage"][0]["sha256"]
    evidence, stats = load_evidence(path, TODAY, Session("页面已移除"))
    assert not evidence["600001"]["facts"] and stats["source_failures"]


def test_t_requires_confirmed_holding_even_if_all_execution_checks_pass():
    provider = SimpleNamespace(fetch=lambda *a, **k: pytest.fail("must not fetch"))
    review = {"entry_watch": True, "frames": {}, "conditions": {"confirmed": True}}
    assert technical.tactical(provider, "600001", True, False, TODAY, execution=review)["status"] == "not_held"
    assert not technical.tactical(provider, "600001", False, True, TODAY, execution=review).get("entry_eligible")
    assert technical.tactical(provider, "600001", True, True, TODAY, execution=review)["entry_eligible"]


@pytest.mark.parametrize("problem", [None, "early_60m", "no_volume", "not_reclaimed", "far_from_support", "top_conflict", "overheated"])
def test_divergence_execution_requires_structure_volume_support_and_current60m(monkeypatch, problem):
    now = datetime(2026, 10, 8, 9 if problem == "early_60m" else 14, 50, tzinfo=SHANGHAI)
    calendar = pd.DataFrame({"trade_date": ["2026-09-30", "2026-10-08"]})
    minute = {"status": "ok", "signals": [{"direction": "bullish", "price_current": 100}],
              "structure": {"low_qfq": 101, "close_qfq": 103, "previous_close_qfq": 102,
                            "price_reclaimed": True, "ratio20": 1.5}}
    if problem == "no_volume":
        minute["structure"]["ratio20"] = .9
    elif problem == "not_reclaimed":
        minute["structure"]["price_reclaimed"] = False
    elif problem == "far_from_support":
        minute["structure"]["low_qfq"] = 105
    elif problem == "top_conflict":
        minute["signals"].append({"direction": "bearish", "price_current": 105})
    elif problem == "overheated":
        minute["structure"]["overheated"] = True
    def frame(provider, code, period, cutoff, daily):
        close = np.linspace(90, 100, 80)
        return pd.DataFrame({"open": close, "close": close, "high": close + 1, "low": close - 1,
                             "volume": 100}, index=pd.date_range(end=cutoff, periods=80, freq=f"{period}min"))
    monkeypatch.setattr(technical, "minute_frame", frame)
    monkeypatch.setattr(technical, "confirmed_divergences", lambda *a, **k: {"status": "ok", "signals": []})
    result = technical.execution_observation(None, "600001", True, now, calendar, None, minute, DEFAULTS)
    assert result["entry_watch"] is (problem is None)


@pytest.mark.parametrize("low,close,expected", [(101, 102, False), (99, 102, True), (99, 100.4, False)])
def test_minute_status_does_not_call_every_ma_reclaim_a_pullback(monkeypatch, low, close, expected):
    index = pd.date_range(end="2026-09-30 15:00", periods=40, freq="15min")
    data = pd.DataFrame({"open": 100.5, "close": 100.5, "high": 103., "low": 99., "volume": 100.}, index=index)
    data.loc[index[-1], ["low", "close"]] = [low, close]
    monkeypatch.setattr(technical, "minute_frame", lambda *a, **k: data)
    monkeypatch.setattr(technical, "indicators", lambda frame: frame.assign(ma20=100., dif=0., dea=1., macd=-2., k=20., d=30., j=40.))
    monkeypatch.setattr(technical, "confirmed_divergences", lambda *a, **k: {"status": "ok", "signals": []})
    result = technical.minute_observation(None, "600001", index[-1], None)
    assert result["structure"]["price_reclaimed"]
    assert result["structure"]["pullback_rebound"] is expected
    assert ("回踩反弹" in result["execution_status"]) is expected


def test_closing_phase_cannot_publish_old_session_or_premarket_as_current_close(tmp_path):
    class CalendarOnly:
        lineage = []
        def fetch(self, function, **kwargs):
            assert function == "tool_trade_date_hist_sina"
            return pd.DataFrame({"trade_date": ["2026-09-30", "2026-10-08"]})
    args = SimpleNamespace(config=None, holdings=None, limit=0, refresh=False, scan_phase="closing", output=str(tmp_path))
    assert run(args, provider=CalendarOnly(), now=datetime(2026, 10, 8, 9, 50, tzinfo=SHANGHAI)) == 2
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "failed" and any("收盘任务" in x for x in report["errors"])


def test_new_report_is_honest_about_recovering_high_j_evidence_missing_and_t_empty():
    card = {**good_row(), **evaluate(good_row(), recovering_technical(), DEFAULTS, TODAY),
            "technical": recovering_technical(), "minute15": {"status": "ok", "signals": [], "structure": {"golden_cross": True}}}
    report = {"selection_rule": "dual_route_monthly_recovery", "scan_phase": "pre_run", "scan_type": "closed_session",
              "generated_at": "2026-10-07T21:00:00+08:00", "session": "2026-09-30", "status": "partial",
              "scan_complete": False, "rankings": [card], "observations": [], "errors": [], "divergences": [],
              "coverage": {"leading_evidence_missing": 20}, "config": DEFAULTS}
    markdown = render_markdown(report)
    assert "27.00" in markdown and "恢复路径" in markdown and "B证据未齐20只" in markdown
    assert "不是10月8日开盘或收盘结果" in markdown and "scan_complete=false" in markdown
    assert "已确认持仓且做T底背离进场观察合格：0只" in markdown
    assert "15分钟观察状态" in markdown and "金叉" in markdown
