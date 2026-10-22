"""Offline boundary cases; fixture prices never enter a market scan."""
import copy
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from tommy_capital.data import SHANGHAI
from tommy_capital.patterns import weekly_ma55_observation, WEEKLY_MA55_VERSION
from tommy_capital.reporting import candidate_reason
from tommy_capital.review import review_report
from tommy_capital.scoring import balanced_score, weekly_ma55_parts
from tommy_capital.technical import indicators, aggregate, timeframe_observation
from test_balanced_top20 import full_technical, frames, report_fixture
from test_core import good_row


def weekly_calculated(count=70):
    close = np.linspace(9., 11., count)
    frame = pd.DataFrame({"open": close, "close": close, "high": close + .2,
                          "low": close - .2, "volume": 100.},
                         index=pd.date_range("2025-01-03", periods=count, freq="W-FRI"))
    return indicators(frame)


def reference(**changes):
    result = copy.deepcopy(full_technical()["timeframes"]["weekly"]["macro_profile"]["weekly_ma55"])
    result.update(changes)
    return result


def test_recent_positions_use_each_week_own_ma55_and_true_completed_history():
    calculated = weekly_calculated()
    lines = np.arange(8.5, 11.5, .5)
    calculated.loc[calculated.index[-6:], "ma55"] = lines
    calculated.loc[calculated.index[-6:], "close"] = lines + .1
    actual = weekly_ma55_observation(calculated)
    assert actual["version"] == WEEKLY_MA55_VERSION and actual["status"] == "ok"
    assert actual["above_count"] == actual["consecutive_above"] == 6
    assert actual["history"][0]["close_qfq"] < actual["ma55_qfq"]
    assert actual["direction"] == "rising"
    future = pd.concat([calculated, calculated.iloc[-1:].set_axis([calculated.index[-1] + pd.Timedelta(weeks=1)])])
    assert weekly_ma55_observation(future.iloc[:-1]) == actual


@pytest.mark.parametrize("direction,points", [("rising", 4.), ("flat", 2.), ("falling", 0.)])
def test_line_direction_is_separate_from_current_price_and_reclaim(direction, points):
    parts, valid = weekly_ma55_parts(reference(direction=direction, retest=None), 11.)
    assert valid and parts["weekly_ma55_direction"] == points
    assert parts["weekly_ma55_current_above"] == 5


def test_one_week_cross_cannot_earn_sustained_hold_or_unconfirmed_reclaim_points():
    one, valid = weekly_ma55_parts(reference(above_count=1, consecutive_above=1, retest=None), 11.)
    stable, _ = weekly_ma55_parts(reference(above_count=4, consecutive_above=3, retest=None), 11.)
    assert valid and one["weekly_ma55_recent_hold"] == one["weekly_ma55_retest_or_reclaim"] == 0
    assert stable["weekly_ma55_recent_hold"] == 4
    assert sum(stable.values()) - sum(one.values()) == 4


def test_ma55_retest_requires_actual_touch_and_rebound_and_never_uses_later_atr():
    calculated = weekly_calculated()
    calculated["ma55"] = 10.
    calculated.loc[calculated.index[-6:], "close"] = [10.5] * 5 + [11.]
    calculated.loc[calculated.index[-6:], "low"] = 10.4
    calculated.loc[calculated.index[-6:], "high"] = 11.2
    calculated.loc[calculated.index[-1], "low"] = 10.
    actual = weekly_ma55_observation(calculated)
    assert actual["retest"]["confirmed_at"] == calculated.index[-1].date().isoformat()
    untouched = calculated.copy()
    untouched.loc[untouched.index[-1], "low"] = 10.01
    assert weekly_ma55_observation(untouched)["retest"] is None
    deep = calculated.copy()
    deep.loc[deep.index[-1], "low"] = 9.
    assert weekly_ma55_observation(deep)["retest"] is None
    later = pd.concat([calculated, calculated.iloc[-1:].set_axis([calculated.index[-1] + pd.Timedelta(weeks=1)])])
    later.loc[later.index[-1], ["low", "atr14"]] = [10.5, 30.]
    assert weekly_ma55_observation(later)["retest"] == actual["retest"]


def test_reclaim_needs_following_completed_week_to_hold_and_is_invalidated_by_later_loss():
    calculated = weekly_calculated()
    calculated["ma55"] = 10.
    calculated.loc[calculated.index[-6:], "close"] = [9.8] * 5 + [10.5]
    calculated.loc[calculated.index[-6:], "low"] = 9.7
    calculated.loc[calculated.index[-6:], "high"] = 10.6
    assert weekly_ma55_observation(calculated)["reclaim"] is None
    later = pd.concat([calculated, calculated.iloc[-1:].set_axis([calculated.index[-1] + pd.Timedelta(weeks=1)])])
    actual = weekly_ma55_observation(later)
    assert actual["reclaim"] == {"event_at": calculated.index[-1].date().isoformat(),
                                  "confirmed_at": later.index[-1].date().isoformat()}
    later.loc[later.index[-1], "close"] = 9.9
    assert weekly_ma55_observation(later)["reclaim"] is None


def test_latest_daily_loss_removes_sixteen_points_and_keeps_two_point_penalty_and_fundamentals():
    technical = full_technical()
    original = balanced_score(good_row(), technical, "A", minute_frames=frames())
    technical["daily_close_qfq"] = 7.9
    below = balanced_score(good_row(), technical, "A", minute_frames=frames())
    assert below["score_inputs_complete"] and below["fundamental_score"] == original["fundamental_score"]
    assert original["technical_score"] - below["technical_score"] == 18
    parts = below["score_breakdown"]["technical"]["weekly"]["parts"]
    assert parts["weekly_ma55_lost_support_penalty"] == -2
    assert all(v == 0 for k, v in parts.items() if k.startswith("weekly_ma55_") and not k.endswith("penalty"))


def test_insufficient_ma55_history_or_missing_daily_reference_is_explicit_not_reweighted():
    actual = weekly_ma55_observation(weekly_calculated(59))
    assert actual["status"] == "unavailable"
    technical = full_technical()
    technical["timeframes"]["weekly"]["macro_profile"]["weekly_ma55"] = actual
    result = balanced_score(good_row(), technical, "A", minute_frames=frames())
    assert not result["score_inputs_complete"]
    assert "technical.weekly.ma55_reference" in result["score_missing_inputs"]
    parts, valid = weekly_ma55_parts(reference(), None)
    assert not valid and sum(parts.values()) == 0
    source = report_fixture(2)
    del source["rankings"][0]["technical"]["timeframes"]["weekly"]["macro_profile"]["weekly_ma55"]
    with pytest.raises(ValueError, match="逐周MA55.*不能补造"):
        review_report(source, datetime(2026, 10, 15, 13, tzinfo=SHANGHAI), "a" * 64)


def test_forming_week_cannot_change_completed_ma55_state():
    index = pd.bdate_range("2023-01-02", "2026-10-15")
    close = np.linspace(9., 12., len(index))
    daily = pd.DataFrame({"open": close, "close": close, "high": close + .2,
                          "low": close - .2, "volume": 100.}, index=index)
    before = timeframe_observation(aggregate(daily, "W-FRI", index[-1].date()), period="weekly")["macro_profile"]["weekly_ma55"]
    daily.loc["2026-10-12":, ["close", "high", "low"]] = [100., 110., 90.]
    after = timeframe_observation(aggregate(daily, "W-FRI", index[-1].date()), period="weekly")["macro_profile"]["weekly_ma55"]
    assert before == after and before["last_bar"][:10] == "2026-10-09"


def test_report_reason_contains_ma55_direction_six_week_position_and_current_daily_distance():
    technical = full_technical()
    row = {**good_row(), "technical": technical, "route": "A", "minute_frames": frames()}
    row.update(balanced_score(row, technical, "A", minute_frames=frames()))
    reason = candidate_reason(1, row)
    assert "周MA55 8.00（上行）" in reason and "近6个完整周6/6收于线上" in reason
    assert "连续6周" in reason and "最新日收盘距线37.50%" in reason

