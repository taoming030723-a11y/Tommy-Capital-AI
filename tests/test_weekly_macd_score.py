"""Offline weight/causality regressions; fixtures never enter real reports."""
import copy
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tommy_capital.cli import DEFAULTS
from tommy_capital.reporting import candidate_reason
from tommy_capital.scoring import (FUNDAMENTAL_WEIGHTS, TECHNICAL_WEIGHTS,
    WEEKLY_MA55_WEIGHTS, WEEKLY_WEIGHTS, balanced_score, ranking_eligible,
    weekly_macd_state)
from tommy_capital.strategy import evaluate
from tommy_capital.technical import aggregate, timeframe_observation
from test_balanced_top20 import full_technical
from test_core import good_row


def histogram_observation(current, previous, atr=1.):
    # DIF/DEA and the displayed 2x histogram always use the same convention.
    return {"status": "ok", "last_bar": "2026-10-16T00:00:00", "signals": [],
            "macd": {"dif": current / 2, "dea": 0., "histogram": current,
                     "previous_histogram": previous, "atr14": atr}}


def scored_histogram(current, previous):
    technical = full_technical()
    weekly = technical["timeframes"]["weekly"]
    weekly.update(histogram_observation(current, previous))
    return technical, balanced_score(good_row(), technical, "A")


def test_explicit_allocations_make_ma55_and_weekly_macd_dominant_without_changing_halves():
    assert sum(WEEKLY_MA55_WEIGHTS.values()) == 16
    assert WEEKLY_WEIGHTS["macd_histogram_state"] + WEEKLY_WEIGHTS["macd_momentum"] == 6
    assert sum(WEEKLY_WEIGHTS.values()) == TECHNICAL_WEIGHTS["weekly"] == 30
    assert TECHNICAL_WEIGHTS["monthly"] == 20
    assert sum(FUNDAMENTAL_WEIGHTS.values()) == sum(TECHNICAL_WEIGHTS.values()) == 50
    result = balanced_score(good_row(), full_technical(), "A")
    weekly = result["score_breakdown"]["technical"]["weekly"]
    assert weekly["component_maxima"] == WEEKLY_WEIGHTS
    assert sum(v for v in weekly["parts"].values() if v > 0) == weekly["score"] == 30
    assert result["technical_score"] == 50


@pytest.mark.parametrize("current,previous,state,points", [
    (.6, .4, "red_expanding", 5.), (.4, .6, "red_contracting", 1.5),
    (.4, .4, "red_flat", 3.), (-.4, -.6, "green_contracting", 3.),
    (-.6, -.4, "green_expanding", 0.), (-.4, -.4, "green_flat", 0.),
    (.2, -.2, "turning_red", 5.), (-.2, .2, "turning_green", 0.),
    (.001, -.001, "near_zero", 0.), (-.001, .001, "near_zero", 0.),
])
def test_red_green_states_distinguish_expansion_contraction_and_near_zero_noise(current, previous, state, points):
    observation = histogram_observation(current, previous)
    original = copy.deepcopy(observation)
    result = weekly_macd_state(observation)
    assert result["status"] == "ok" and result["state"] == state
    assert result["parts"]["macd_histogram_state"] == points
    assert observation == original  # Scoring cannot invent or rewrite a signal.
    assert "signals" not in result and "2×(DIF−DEA)" in result["basis"]


def test_declining_red_does_not_outrank_recovering_green_just_because_it_is_red():
    scores = {}
    for name, pair in {"red_growth": (.6, .4), "red_fade": (.4, .6),
                       "green_recovery": (-.4, -.6), "green_decline": (-.6, -.4)}.items():
        technical, result = scored_histogram(*pair)
        scores[name] = result["technical_score"]
        assert result["fundamental_score"] == balanced_score(good_row(), full_technical(), "A")["fundamental_score"]
        assert result["score_breakdown"]["technical"]["monthly"]["score"] == 20
        assert result["score_breakdown"]["technical"]["weekly"]["parts"]["confirmed_macd_bottom"] == 0
        assert evaluate(good_row(), technical, DEFAULTS, date(2026, 10, 16))["eligible"]
    assert scores["red_growth"] > scores["green_recovery"] > scores["red_fade"] > scores["green_decline"]


@pytest.mark.parametrize("multiplier", [.01, 100., 10000.])
def test_atr_normalization_preserves_scores_across_price_scales(multiplier):
    observation = histogram_observation(.6, .4)
    scaled = copy.deepcopy(observation)
    scaled["macd"] = {k: v * multiplier for k, v in observation["macd"].items()}
    assert weekly_macd_state(scaled)["parts"] == weekly_macd_state(observation)["parts"]
    # A microscopic absolute movement on a large-ATR share earns no expansion bonus.
    assert weekly_macd_state(histogram_observation(.6, .4, atr=100.))["state"] == "near_zero"


@pytest.mark.parametrize("field,bad", [("histogram", None), ("previous_histogram", float("nan")),
    ("dif", float("inf")), ("dea", None), ("atr14", 0.), ("atr14", -1.)])
def test_missing_or_invalid_weekly_macd_cannot_earn_points_or_complete_the_ranking(field, bad):
    technical = full_technical()
    observation = technical["timeframes"]["weekly"]
    observation["macd"][field] = bad
    state = weekly_macd_state(observation)
    assert state["status"] == "unavailable" and sum(state["parts"].values()) == 0
    score = balanced_score(good_row(), technical, "A")
    assert not score["score_inputs_complete"]
    assert "technical.weekly.macd" in score["score_missing_inputs"]
    assert not ranking_eligible({"eligible": True, **score})


def test_strict_top_penalty_and_ma55_gate_survive_strong_histogram_scores():
    technical, original = scored_histogram(.6, .4)
    technical["timeframes"]["weekly"]["signals"] = [
        {"indicator": "MACD_DIF", "direction": "bearish", "quality_passed": True}]
    conflict = balanced_score(good_row(), technical, "A")
    assert conflict["technical_score"] == original["technical_score"] - 1
    technical["timeframes"]["weekly"]["macro_profile"]["weekly_ma55"]["direction"] = "falling"
    assert not evaluate(good_row(), technical, DEFAULTS, date(2026, 10, 16))["eligible"]


def test_forming_week_cannot_change_completed_histogram_state_or_score():
    index = pd.bdate_range("2023-01-02", "2026-10-22")
    close = 10 + np.linspace(0., 3., len(index)) + np.sin(np.arange(len(index)) / 15.)
    daily = pd.DataFrame({"open": close, "close": close, "high": close + .2,
                          "low": close - .2, "volume": 100.}, index=index)
    before = timeframe_observation(aggregate(daily, "W-FRI", index[-1].date()), period="weekly")
    daily.loc["2026-10-19":, ["close", "high", "low", "volume"]] = [100., 110., 90., 999999.]
    after = timeframe_observation(aggregate(daily, "W-FRI", index[-1].date()), period="weekly")
    assert before == after and before["last_bar"][:10] == "2026-10-16"
    technical = full_technical()
    technical["timeframes"]["weekly"] = before
    first = balanced_score(good_row(), technical, "A")
    technical["timeframes"]["weekly"] = after
    assert balanced_score(good_row(), technical, "A") == first


def test_chinese_reason_explains_new_points_and_completed_week_without_claiming_divergence():
    technical, score = scored_histogram(-.4, -.6)
    row = {**good_row(), **score, "technical": technical, "route": "A"}
    reason = candidate_reason(1, row)
    assert "MA55专项16.00/16.00分" in reason
    assert "周MACD绿柱收敛，等待转强" in reason and "柱体3.00/5.00分" in reason
    assert "完整周2026-10-16" in reason
    assert "周MACD底背离" not in reason
