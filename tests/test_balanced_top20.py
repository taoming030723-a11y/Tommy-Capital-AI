"""Offline scoring/report fixtures; never used as live market inputs."""
import copy
import json
from datetime import date, datetime

import pandas as pd
import pytest

from tommy_capital.cli import DEFAULTS, make_summary, write_outputs
from tommy_capital.data import SHANGHAI
from tommy_capital.reporting import render_markdown
from tommy_capital.scoring import (balanced_score, ranked_top20, SCORING_VERSION,
                                   SCORE_WEIGHTS, TECHNICAL_WEIGHTS, FUNDAMENTAL_WEIGHTS)
from tommy_capital.strategy import evaluate
from tommy_capital.technical import execution_observation, minute_observation
from test_core import good_row, good_technical, candles
from test_dual_routes import losing_row, recovering_technical


def observation():
    return {"status": "ok", "last_bar": "2026-10-08T15:00:00", "above_ma20": True,
            "macd": {"dif": 1., "dea": .5, "histogram": 1., "previous_histogram": .5},
            "signals": [{"direction": "bullish", "indicator": "MACD_DIF", "price_current": 10.,
                         "confirmed_at": "2026-10-08T15:00:00"}],
            "structure": {"ma20_rising": True}}


def full_technical():
    return {**good_technical(), "timeframes": {p: observation() for p in ["monthly", "weekly", "daily"]},
            "daily_divergences": observation(), "weekly_base": {"detected": True, "range_pct": 5},
            "daily_volume": {"abnormal": True, "threshold": 2., "ratio20": 2.},
            "first_volume_breakout": {"active": True}, "daily_bar_date": "2026-10-08"}


def frames():
    return {p: observation() for p in ["60", "30", "15"]}


def test_exact_half_weights_and_auditable_bounded_scores():
    assert sum(FUNDAMENTAL_WEIGHTS.values()) == sum(TECHNICAL_WEIGHTS.values()) == 50
    row = {**good_row(), "revenue_yoy": 40, "profit_yoy": 60, "cfo_per_share_ytd": 1.,
           "previous_same_cfo_per_share_ytd": .5, "pe_ttm": .00001, "pb": .00001, "industry_pe_ratio": .00001}
    result = balanced_score(row, full_technical(), "A", minute_frames=frames())
    assert result["score_inputs_complete"]
    assert result["score"] == 100 and result["fundamental_score"] == result["technical_score"] == 50
    assert result["scoring_version"] == SCORING_VERSION
    assert sum(result["score_breakdown"]["fundamental"].values()) == 50
    assert sum(v["score"] for v in result["score_breakdown"]["technical"].values()) == 50


@pytest.mark.parametrize("period", list(TECHNICAL_WEIGHTS))
def test_macd_bottom_counts_each_of_six_periods_without_kdj_substitution(period):
    technical, minute = full_technical(), frames()
    reference = balanced_score(good_row(), technical, "A", minute_frames=minute)
    frame = technical["timeframes"][period] if period in technical["timeframes"] else minute[period]
    frame["signals"][0]["indicator"] = "KDJ_J"
    result = balanced_score(good_row(), technical, "A", minute_frames=minute)
    assert result["technical_score"] < reference["technical_score"]
    assert result["score_breakdown"]["technical"][period]["parts"]["confirmed_macd_bottom"] == 0


def test_small_period_observations_change_ranking_without_granting_eligibility():
    tech = full_technical()
    tech.update(monthly_trend=False)
    before = evaluate(good_row(), tech, DEFAULTS, date(2026, 10, 8))
    assert before["eligible"] and not before["t_trend_confirmed"]
    better = balanced_score(good_row(), tech, "A", minute_frames=frames())
    missing = balanced_score(good_row(), tech, "A", minute_frames={"15": observation()})
    rows = [{"code": "000001", **missing}, {"code": "000002", **better}]
    assert ranked_top20(rows)[0]["code"] == "000002"
    assert not missing["score_inputs_complete"]
    assert missing["score_breakdown"]["technical"]["60"]["score"] == 0
    assert better["fundamental_score"] == missing["fundamental_score"]
    assert better["technical_score"] - missing["technical_score"] == 13


def test_top_conflict_overheat_penalties_and_missing_inputs_are_not_reweighted():
    tech, minute = full_technical(), frames()
    reference = balanced_score(good_row(), tech, "A", minute_frames=minute)
    minute["15"]["signals"].append({"direction": "bearish", "indicator": "KDJ_J"})
    minute["15"]["structure"]["overheated"] = True
    result = balanced_score(good_row(), tech, "A", minute_frames=minute)
    assert reference["technical_score"] - result["technical_score"] == 3
    assert result["score_breakdown"]["technical"]["15"]["score"] == 2
    minute["60"] = {"status": "unavailable", "signals": []}
    result = balanced_score(good_row(), tech, "A", minute_frames=minute)
    assert result["technical_score"] == 39
    assert "technical.60" in result["score_missing_inputs"]
    assert result["fundamental_score"] == reference["fundamental_score"]


def test_short_complete_monthly_history_is_not_reported_as_no_divergence():
    tech = full_technical()
    tech["timeframes"]["monthly"].update(divergence_status="insufficient_bars", signals=[])
    result = balanced_score(good_row(), tech, "A", minute_frames=frames())
    assert not result["score_inputs_complete"]
    assert "technical.monthly.macd_divergence_history" in result["score_missing_inputs"]


def test_loss_route_retains_verified_evidence_and_valuation_and_no_positive_cash_bonus():
    row = losing_row()
    technical = {**full_technical(), **recovering_technical()}
    decision = evaluate(row, technical, DEFAULTS, date(2026, 10, 8))
    assert decision["eligible"] and decision["route"] == "B"
    result = balanced_score(row, technical, "B", decision["leading"], frames())
    assert "TTM亏损" in result["profit_status"]
    assert result["score_breakdown"]["fundamental"]["earnings_or_leading"] == 8
    assert result["score_breakdown"]["fundamental"]["cash_quality"] == 0
    assert result["score_breakdown"]["fundamental"]["valuation"] > 0
    row["leading_evidence"]["valuation_model"] = None
    assert not evaluate(row, technical, DEFAULTS, date(2026, 10, 8))["eligible"]


def report_fixture(count=30):
    technical = full_technical()
    pool = []
    for index in range(count):
        row = {**good_row(), "code": f"{600000 + index:06d}", "name": f"报告测试股{index:02d}"}
        decision = evaluate(row, technical, DEFAULTS, date(2026, 10, 8))
        pool.append({**row, **decision, **balanced_score(row, technical, "A", minute_frames=frames()),
                     "score": 30. + index, "technical": copy.deepcopy(technical), "minute_frames": frames(),
                     "minute15": observation(), "tactical": {"status": "not_held"},
                     "execution": {"status": "wait_for_confirmation", "entry_watch": False}, "confirmed_holding": False})
    return {"system": "test fixture", "selection_rule": "dual_route_monthly_recovery", "status": "partial",
            "scan_complete": False, "scan_phase": "closing", "scan_type": "after_close",
            "generated_at": "2026-10-08T16:00:00+08:00", "finished_at": "2026-10-08T16:05:00+08:00",
            "session": "2026-10-08", "minute15_cutoff": "2026-10-08T15:00:00",
            "scoring_version": SCORING_VERSION, "score_weights": SCORE_WEIGHTS, "display_limit": 20,
            "rankings": pool, "observations": [{"code": "000099", "name": "池外隐藏公司"}],
            "excluded": [], "divergences": [{"code": "000099", "name": "池外隐藏公司", "timeframe": "daily"}],
            "errors": ["600000 日线：隐藏低分公司的数据错误"], "config": DEFAULTS, "coverage": {}}


def test_human_report_top20_from_global_ranking_with_no_other_stock_lists(tmp_path):
    report = report_fixture()
    report["rankings"].reverse()  # Renderer sorts, rather than trusting first20.
    markdown = render_markdown(report)
    for index in range(10):
        assert f"报告测试股{index:02d}" not in markdown
    for index in range(10, 30):
        assert f"报告测试股{index:02d}" in markdown
    assert "池外隐藏公司" not in markdown and "隐藏低分公司的" not in markdown
    assert "<details>" not in markdown and "30 / 20只" in markdown
    write_outputs(tmp_path, report)
    assert (tmp_path / "report.md").read_text() == markdown
    html = (tmp_path / "report.html").read_text()
    assert html.count("<section>") == 20 and "池外隐藏公司" not in html
    assert "报告测试股00" not in html
    full = json.loads((tmp_path / "report.json").read_text())
    assert len(full["rankings"]) == 30
    assert len(pd.read_csv(tmp_path / "rankings.csv")) == 30
    summary = make_summary(report)
    assert summary["candidate_count"] == 30 and len(summary["top_candidates"]) == 20
    assert summary["top_candidates"][0]["code"] == "600029"


def test_fewer20_candidates_and_ties_are_deterministic_no_padding():
    report = report_fixture(3)
    for row in report["rankings"]:
        row["score"] = 40
    assert [r["code"] for r in ranked_top20(list(reversed(report["rankings"])))] == ["600000", "600001", "600002"]
    assert len(make_summary(report)["top_candidates"]) == 3
    assert "3 / 3只" in render_markdown(report)


def test_execution_reuses_verified_frames_without_duplicate_calls():
    minute = observation()
    minute["structure"].update(low_qfq=10., close_qfq=11., previous_close_qfq=10.5,
                                price_reclaimed=True, ratio20=1.5)
    prefetched = frames()
    prefetched["30"]["signals"] = []
    result = execution_observation(None, "600000", True, datetime(2026, 10, 8, 16, tzinfo=SHANGHAI),
        None, None, minute, DEFAULTS, prefetched)
    assert result["entry_watch"] and result["frames"]["60"] is prefetched["60"]


@pytest.mark.parametrize("period", [30, 60])
def test_extra_periods_preserve_60bar_history_requirement(monkeypatch, period):
    monkeypatch.setattr('tommy_capital.technical.minute_frame', lambda *a, **k: candles(59))
    result = minute_observation(None, "600000", None, None, period)
    assert result["status"] == "insufficient_bars" and result["signals"] == []
