"""Synthetic closed-bar fixtures are solely for offline regression, never market input."""
import copy
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from tommy_capital.data import SHANGHAI
from tommy_capital.patterns import macro_profile, PROFILE_VERSION
from tommy_capital.review import review_report
from tommy_capital.scoring import balanced_score, TECHNICAL_WEIGHTS, ranking_eligible
from tommy_capital.technical import indicators, timeframe_observation, aggregate
from test_balanced_top20 import full_technical, frames, report_fixture
from test_core import good_row


def prices(count=300):
    close = np.linspace(9., 12., count)
    return pd.DataFrame({"open": close - .03, "close": close, "high": close + .15,
                         "low": close - .15, "volume": 100.},
                        index=pd.bdate_range("2025-01-02", periods=count))


def test_macro_structure_dominates_and_perfect_minute_frames_never_restore_failed_gates():
    assert sum(TECHNICAL_WEIGHTS[p] for p in ("monthly", "weekly", "daily")) == 46
    assert sum(TECHNICAL_WEIGHTS[p] for p in ("60", "30", "15")) == 4
    technical = full_technical()
    technical["weekly_trend"] = False
    strongest = balanced_score(good_row(), technical, "A", minute_frames=frames())
    weak = frames()
    for frame in weak.values():
        frame.update(above_ma20=False, signals=[])
        frame["structure"]["ma20_rising"] = False
        frame["macd"].update(dif=0, dea=0, histogram=0, previous_histogram=0)
    assert strongest["score"] == balanced_score(good_row(), technical, "A", minute_frames=weak)["score"]
    assert not strongest["confirmation_state"]["execution_score_enabled"]


def test_causal_platform_price_launch_does_not_add_daily_volume_points():
    frame = prices()
    for field in ("open", "close"):
        frame.loc[frame.index[-21:], field] = 12.
    frame.loc[frame.index[-21:], "low"] = 11.9
    frame.loc[frame.index[-21:], "high"] = 12.1
    frame.loc[frame.index[-1], ["close", "high", "low"]] = [12.5, 12.6, 12.]
    result = macro_profile(indicators(frame), "daily")
    assert result["price_platform_breakout"] and not result["chase_risk"]
    assert result["platform_high_qfq"] == 12.1
    alternative = frame.copy()
    alternative.iloc[-1, alternative.columns.get_loc("volume")] = 10000000.
    assert macro_profile(indicators(alternative), "daily") == result
    # Adding future bars changes the future observation, never the stored earlier one.
    future = pd.concat([frame, frame.iloc[-1:].set_axis([frame.index[-1] + pd.offsets.BDay()])])
    assert macro_profile(indicators(future.iloc[:-1]), "daily") == result


def test_rising_support_requires_an_actual_touch_and_reclaim_not_close_above_line_only():
    frame = prices()
    ma = indicators(frame).ma13.iloc[-1]
    frame.iloc[-1, frame.columns.get_loc("low")] = ma
    result = macro_profile(indicators(frame), "daily")
    assert result["pullback_reclaimed"] and 13 in result["reclaimed_ma_periods"]
    untouched = frame.copy()
    untouched.iloc[-1, untouched.columns.get_loc("low")] = untouched.close.iloc[-1] - .01
    assert not macro_profile(indicators(untouched), "daily")["pullback_reclaimed"]
    failed = frame.copy()
    failed.iloc[-1, failed.columns.get_loc("close")] = ma - .3
    assert not macro_profile(indicators(failed), "daily")["pullback_reclaimed"]


def test_short_history_does_not_invent_long_monthly_ma_or_reallocate_points():
    frame = prices(40)
    frame.index = pd.date_range("2023-01-31", periods=40, freq="ME")
    observation = timeframe_observation(frame, period="monthly")
    profile = observation["macro_profile"]
    assert profile["status"] == "ok"
    assert profile["averages_qfq"]["144"] is None and profile["averages_qfq"]["233"] is None
    assert profile["optional_long_ma_missing"] == [55, 144, 233]
    technical = full_technical()
    technical["timeframes"]["monthly"] = observation
    score = balanced_score(good_row(), technical, "A", minute_frames=frames())
    assert score["score_inputs_complete"]
    assert score["score_breakdown"]["technical"]["monthly"]["max"] == 18


def test_missing_core_ma_kdj_records_are_missing_not_substituted_from_old_ma20():
    technical = full_technical()
    del technical["timeframes"]["weekly"]["macro_profile"]
    score = balanced_score(good_row(), technical, "A", minute_frames=frames())
    assert not score["score_inputs_complete"]
    assert "technical.weekly.macro_profile" in score["score_missing_inputs"]
    assert not ranking_eligible({"eligible": True, **score})
    source = report_fixture(2)
    del source["rankings"][0]["technical"]["timeframes"]["monthly"]["macro_profile"]
    with pytest.raises(ValueError, match="不能补造"):
        review_report(source, datetime(2026, 10, 14, 13, tzinfo=SHANGHAI), "a" * 64)


def test_risk_penalty_preserves_healthy_expansion_but_reduces_fading_or_broken_support():
    technical = full_technical()
    healthy = balanced_score(good_row(), technical, "A", minute_frames=frames())
    for period in ("monthly", "weekly", "daily"):
        technical["timeframes"][period]["macro_profile"].update(extended=True, chase_risk=False)
    assert balanced_score(good_row(), technical, "A", minute_frames=frames())["score"] == healthy["score"]
    technical["timeframes"]["daily"]["macro_profile"]["chase_risk"] = True
    risky = balanced_score(good_row(), technical, "A", minute_frames=frames())
    assert risky["fundamental_score"] == healthy["fundamental_score"]
    assert risky["technical_score"] == healthy["technical_score"] - 5  # Lost setup3 + risk2.
    assert risky["score_breakdown"]["technical"]["daily"]["parts"]["price_launch_or_support_reclaim"] == 0


def test_forming_month_is_excluded_and_histogram_recovery_is_never_called_bottom_divergence():
    frame = prices(550)
    cutoff = frame.index[-1].date()
    monthly = aggregate(frame, "ME", cutoff)
    result = timeframe_observation(monthly, period="monthly")
    assert result["last_bar"][:10] <= cutoff.isoformat()
    assert result["macro_profile"]["last_bar"] == result["last_bar"]
    technical = full_technical()
    technical["timeframes"]["daily"]["signals"] = []
    scored = balanced_score(good_row(), technical, "A", minute_frames=frames())
    assert scored["score_breakdown"]["technical"]["daily"]["parts"]["macd_momentum"] > 0
    assert scored["score_breakdown"]["technical"]["daily"]["parts"]["confirmed_macd_bottom"] == 0


def test_moving_average_cost_band_breakout_can_precede_all_long_ma_turning_up():
    calculated = indicators(prices())
    for length in (20, 55, 144, 233):
        calculated.loc[calculated.index[-2:], f"ma{length}"] = 11.9
    calculated.loc[calculated.index[-2], "close"] = 11.8
    calculated.loc[calculated.index[-1], "close"] = 12.1
    profile = macro_profile(calculated, "weekly")
    assert profile["ma_cluster_breakout"]
    assert profile["prior_long_ma_band_width_pct"] == 0


def test_ticker_and_name_do_not_modify_technical_or_fundamental_scores():
    base = good_row()
    result = balanced_score(base, full_technical(), "A", minute_frames=frames())
    for code, name in (("603127", "昭衍新药"), ("600089", "特变电工"), ("000001", "其他公司")):
        assert balanced_score({**base, "code": code, "name": name}, full_technical(), "A", minute_frames=frames()) == result
