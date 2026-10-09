"""Regression fixtures for financial quality, causal signals, and universe coverage."""
import copy
from datetime import date

import pandas as pd
import pytest

from tommy_capital.coverage import technical_results, ranking_completeness
from tommy_capital.data import DataError, merge_finance_quality, latest_finance
from tommy_capital.data import daily_history
from tommy_capital.scoring import balanced_score, ranked_top20
from tommy_capital.technical import confirmed_divergences, execution_observation
from test_balanced_top20 import full_technical, frames, report_fixture, observation
from test_core import good_row, history_frame, candles


def test_profit_growth_cannot_replace_core_earnings_and_cash_conversion():
    sound = good_row()
    fragile = {**sound, "profit_yoy": 10000, "previous_same_profit_ytd": .1,
               "core_profit_ttm": 12, "cfo_ttm": 12, "cfo_ytd": 5}
    strong = balanced_score(sound, full_technical(), "A", minute_frames=frames())
    weak = balanced_score(fragile, full_technical(), "A", minute_frames=frames())
    assert weak["fundamental_quality"]["low_base_flag"]
    assert weak["fundamental_quality"]["cash_conversion_ttm"] == pytest.approx(.1)
    assert strong["fundamental_score"] > weak["fundamental_score"]
    assert strong["technical_score"] == weak["technical_score"]
    # Earnings growth no longer directly determines any of the quality allocations.
    different_rate = balanced_score({**sound, "profit_yoy": 10000}, full_technical(), "A", minute_frames=frames())
    assert strong["fundamental_score"] == different_rate["fundamental_score"]


def test_cash_totals_not_per_share_basis_and_unknown_quality_never_rescaled():
    row = good_row()
    before = balanced_score(row, full_technical(), "A", minute_frames=frames())
    after = balanced_score({**row, "eps_ytd": 1000, "cfo_per_share_ytd": 10000}, full_technical(), "A", minute_frames=frames())
    assert before["fundamental_score"] == after["fundamental_score"]
    missing = balanced_score({**row, "cfo_ttm": None}, full_technical(), "A", minute_frames=frames())
    assert not missing["score_inputs_complete"]
    assert missing["fundamental_score"] < before["fundamental_score"]
    assert missing["technical_score"] == before["technical_score"]
    assert "fundamental.cfo_ttm" in missing["score_missing_inputs"]


def test_statement_period_publication_and_income_bridge_are_validated():
    history = history_frame()
    history["revenue_ytd"] = [400, 1000, 300, 600]
    quality = {"income": [], "cashflow": []}
    for _, row in history.iterrows():
        base = dict(code=row.code, period=row.period, quality_announced_at=row.period+pd.Timedelta(days=20))
        quality["income"].append(pd.DataFrame([{**base, "core_profit_ytd": row.profit_ytd*.9,
            "statement_profit_ytd": row.profit_ytd, "statement_revenue_ytd": row.revenue_ytd}]))
        quality["cashflow"].append(pd.DataFrame([{**base, "cfo_ytd": row.profit_ytd*.5}]))
    actual = latest_finance(merge_finance_quality(history, quality, date(2026, 10, 9))).iloc[0]
    assert actual.core_profit_ttm == pytest.approx(108)
    assert actual.cfo_ttm == pytest.approx(60)
    assert actual.cash_conversion_ttm == pytest.approx(.5)
    # Reconciled totals must match the original source version.
    quality["income"][-1].loc[0, "statement_profit_ytd"] = 12345
    actual = latest_finance(merge_finance_quality(history, quality, date(2026, 10, 9))).iloc[0]
    assert pd.isna(actual.core_profit_ttm) and pd.isna(actual.cfo_ttm)
    quality["income"][-1].loc[0, "quality_announced_at"] = pd.Timestamp("2026-10-10")
    actual = latest_finance(merge_finance_quality(history, quality, date(2026, 10, 9))).iloc[0]
    assert pd.isna(actual.core_profit_ytd)


@pytest.mark.parametrize("problem", ["weekly_trend", "daily_trend", "weekly_structure", "strategic", "missing_score"])
def test_maximum_short_period_score_cannot_promote_unconfirmed_stock(problem):
    rows = report_fixture(2)["rankings"]
    rows[1]["score"] = 100
    if problem in ("weekly_trend", "daily_trend"):
        rows[1]["technical"][problem] = False
    elif problem == "weekly_structure":
        rows[1]["technical"]["weekly_structure"]["confirmed"] = False
    elif problem == "strategic":
        rows[1]["strategic_eligible"] = False
    else:
        rows[1]["score_inputs_complete"] = False
    assert [r["code"] for r in ranked_top20(rows)] == [rows[0]["code"]]
    assert len(rows) == 2  # The unconfirmed row is still auditable in the observation pool.


def test_weak_macd_evidence_has_no_score_or_t_execution_authority():
    row, tech, minute = good_row(), full_technical(), frames()
    original = balanced_score(row, tech, "A", minute_frames=minute)
    minute["15"]["signals"][0]["quality_passed"] = False
    weak = balanced_score(row, tech, "A", minute_frames=minute)
    assert weak["technical_score"] == original["technical_score"] - 2
    del minute["15"]["signals"][0]["quality_passed"]
    assert balanced_score(row, tech, "A", minute_frames=minute)["technical_score"] == weak["technical_score"]


@pytest.mark.parametrize("defect", ["tiny_price", "tiny_dif", "wrong_zero", "no_rebound", "invalidated"])
def test_causal_macd_strength_rejects_weak_pivots(monkeypatch, defect):
    from tommy_capital import technical
    data = candles(60)
    data[["open", "close", "low", "high"]] = [100, 100, 99, 101]
    data.loc[data.index[35], "low"] = 95
    data.loc[data.index[52], "low"] = 94.99 if defect == "tiny_price" else 94
    data.loc[data.index[52], "close"] = 96
    data.loc[data.index[55], "close"] = 96 if defect == "no_rebound" else 98
    if defect == "invalidated":
        data.loc[data.index[59], "low"] = 93
    def oscillator(raw):
        result = raw.assign(atr14=2., dif=-1., j=50.)
        result.loc[result.index[35], "dif"] = 1. if defect == "wrong_zero" else -2.
        result.loc[result.index[52], "dif"] = 2. if defect == "wrong_zero" else -1.999 if defect == "tiny_dif" else -1.
        return result
    monkeypatch.setattr(technical, "indicators", oscillator)
    result = confirmed_divergences(data)
    assert not any(s["indicator"] == "MACD_DIF" for s in result["signals"])
    assert any(s["indicator"] == "MACD_DIF" and not s["quality_passed"] for s in result["weak_signals"])
    truncated = confirmed_divergences(data.iloc[:55])  # Third right bar not available yet.
    assert not any(s["current_pivot"] == data.index[52].isoformat() for s in truncated["signals"])


def test_selective_recovery_scans_financially_excluded_and_never_duplicates():
    rows = [{"code": f"{i:06}", "financially_passed": i == 0} for i in range(4)]
    calls = []
    def inspect(row, fresh):
        calls.append((row["code"], fresh))
        if row["code"] == "000001" and not fresh:
            raise DataError("源尚未更新")
        if row["code"] == "000002":
            raise DataError("周/月线不足（至少60周、24个完整月份）")
        if row["code"] == "000003":
            raise DataError("接口限流")
        return {"code": row["code"]}, None
    result = list(technical_results(rows, inspect, workers=2))
    assert len(result) == len({r[0]["code"] for r in result}) == 4
    assert ("000001", True) in calls and ("000003", True) in calls
    assert ("000000", True) not in calls and ("000002", True) not in calls
    recovered = next(r for r in result if r[0]["code"] == "000001")
    assert recovered[1] and recovered[3] == ["源尚未更新"]
    assert next(r for r in result if r[0]["code"] == "000003")[1] is None


def test_requested_full_market_is_not_automatically_complete_ranking():
    report = {"coverage": {"universe": 5572, "technical_requested": 5572, "technical_completed": 5571,
        "financial_data_available": 5572, "unscanned": 0, "leading_evidence_missing": 0,
        "score_inputs_completed": 20, "monthly_pool_count": 20}, "errors": []}
    assert not ranking_completeness(report)[0]

    report["coverage"]["technical_completed"] = 5572
    assert ranking_completeness(report)[0]
    report["coverage"]["leading_evidence_missing"] = 1
    assert not ranking_completeness(report)[0]


@pytest.mark.parametrize("price", [float("nan"), float("inf"), 0, -1])
def test_missing_quote_price_cannot_bypass_complete_session_price_validation(price):
    class NoRequests:
        def fetch(self, *args, **kwargs):
            pytest.fail("unknown quotes cannot be reconciled by another daily response")
    with pytest.raises(DataError, match="报价价格"):
        daily_history(NoRequests(), "000001", None, "20261009", expected_close=price)
