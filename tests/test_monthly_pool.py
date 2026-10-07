"""Synthetic formula/control-flow fixtures; never published as live results."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tommy_capital.cli import DEFAULTS, validate_config
from tommy_capital.data import DataError
from tommy_capital.reporting import minute_label, render_markdown
from tommy_capital.strategy import evaluate
from tommy_capital.technical import (aggregate, daily_volume_observation,
                                    weekly_base_observation, tactical)
from test_core import good_row, good_technical


@pytest.mark.parametrize("j,expected", [(-5, True), (19.999, True), (20, False),
    (75, False), (None, False), (np.nan, False), (np.inf, False)])
def test_monthly_j_is_strict_entrance_even_with_all_trends_confirmed(j, expected):
    technical = {**good_technical(), "monthly_j": j}
    technical.pop("monthly_recovery")
    result = evaluate(good_row(), technical, DEFAULTS, date(2026, 10, 7))
    assert result["eligible"] is expected
    assert result["monthly_low_j_watch"] is expected
    assert not result["strategic_eligible"]  # no confirmed J recovery history


def test_low_j_observation_needs_neither_profit_acceleration_nor_ma_uptrend():
    row = {**good_row(), "fundamental_improving": False}
    technical = {**good_technical(), "daily_trend": False, "weekly_trend": False,
                 "monthly_trend": False}
    result = evaluate(row, technical, DEFAULTS, date(2026, 10, 7))
    assert result["eligible"] and not result["t_trend_confirmed"]
    class NoRequests:
        def fetch(self, *args, **kwargs):
            pytest.fail("observation alone must not generate held-stock T checks")
    assert tactical(NoRequests(), "000001", result["t_trend_confirmed"], True,
                    date(2026, 9, 30))["status"] == "strategy_not_passed"
    row["pe_ttm"] = 61
    assert not evaluate(row, technical, DEFAULTS, date(2026, 10, 7))["eligible"]


def volume_frame():
    # Distant spikes, a 20-day baseline of 1..20, then exactly twice its mean.
    volumes = [10000] * 20 + list(range(1, 21)) + [21]
    return pd.DataFrame({"volume": volumes}, index=pd.bdate_range(end="2026-09-30", periods=41))


def test_abnormal_volume_uses_exact_previous20_trading_days_excluding_detection_day():
    frame = volume_frame()
    result = daily_volume_observation(frame)
    assert result["ratio20"] == 2 and result["abnormal"]
    assert result["previous20_mean_volume"] == 10.5
    assert result["baseline_bars"] == 20
    assert result["baseline_start"] == frame.index[-21].date().isoformat()
    assert result["baseline_end"] == frame.index[-2].date().isoformat()
    assert result["confirmed_at"] == "2026-09-30"
    assert not daily_volume_observation(frame, threshold=2.1)["abnormal"]


@pytest.mark.parametrize("problem", ["short", "zero", "missing", "current_missing"])
def test_missing_volume_baseline_never_becomes_no_abnormal_volume(problem):
    frame = volume_frame()
    if problem == "short":
        frame = frame.tail(20)
    elif problem == "zero":
        frame.iloc[-21:-1, 0] = 0
    else:
        frame.iloc[-2 if problem == "missing" else -1, 0] = np.nan
    with pytest.raises(DataError):
        daily_volume_observation(frame)


def base_frames():
    # Final completed holiday week has only three sessions, all at volume 100.
    index = pd.bdate_range("2026-08-24", "2026-09-30")
    daily = pd.DataFrame({"open": 100., "close": 100., "high": 101., "low": 99.,
                          "volume": 100.}, index=index)
    return daily, aggregate(daily, "W-FRI", date(2026, 10, 7))


def test_weekly_base_uses_completed_weeks_and_normalizes_holiday_trading_days():
    daily, weekly = base_frames()
    result = weekly_base_observation(weekly, daily)
    assert result["detected"]
    assert result["daily_average_volume_ratio"] == 1
    assert result["period_end"] == "2026-10-02"
    assert result["last_trading_day"] == "2026-09-30"


@pytest.mark.parametrize("condition", ["range_compressed", "lows_stable", "close_holds_base", "volume_quiet"])
def test_weekly_base_requires_every_price_and_volume_condition(condition):
    daily, weekly = base_frames()
    if condition == "range_compressed":
        weekly.iloc[-1, weekly.columns.get_loc("high")] = 125
    elif condition == "lows_stable":
        weekly.iloc[-1, weekly.columns.get_loc("low")] = 95
    elif condition == "close_holds_base":
        weekly.iloc[-1, weekly.columns.get_loc("close")] = 99
    else:
        weekly.iloc[-3:, weekly.columns.get_loc("volume")] *= 2
    result = weekly_base_observation(weekly, daily)
    assert not result["detected"] and not result["conditions"][condition]


def test_report_primary_list_and_divergences_follow_monthly_pool():
    daily, weekly = base_frames()
    technical = {**good_technical(), "weekly_base": weekly_base_observation(weekly, daily),
                 "daily_volume": daily_volume_observation(volume_frame()), "volume_ratio20": 2}
    card = {**good_row(), "eligible": True, "strategic_eligible": False, "technical": technical,
            "score": 60, "monthly_low_j_watch": True, "minute15": {"status": "unavailable", "signals": []}}
    signals = [{"code": "999999", "name": "池外不应展示", "timeframe": "daily", "direction": "bullish",
                "monthly_pool_eligible": False},
               {"code": "000001", "name": "测试公司", "timeframe": "15m", "direction": "bearish",
                "monthly_pool_eligible": True, "indicator": "MACD_DIF", "confirmed_at": "2026-10-08T09:45:00"}]
    report = {"selection_rule": "monthly_j_lt_20", "config": DEFAULTS, "status": "partial",
              "scan_complete": False, "generated_at": "2026-10-08T09:50:00+08:00", "scan_type": "intraday",
              "session": "2026-09-30", "rankings": [card], "observations": [], "divergences": signals,
              "errors": [], "coverage": {"monthly_pool_minute15_completed": 0}}
    markdown = render_markdown(report)
    assert "月 J＜20 观察候选 TOP 10" in markdown and "日线量比20" in markdown
    assert "≥2.00倍" in markdown and "此前20个交易日" in markdown
    assert "池外不应展示" not in markdown and "2026-10-08 09:45:00" in markdown
    assert "数据缺失" in markdown and "scan_complete=false" in markdown
    assert minute_label({"status": "insufficient_bars", "signals": []}) == "K线不足，未能判断"


def test_volume_threshold_and_weekly_tolerance_validation():
    validate_config(DEFAULTS)
    with pytest.raises(ValueError):
        validate_config({**DEFAULTS, "daily_volume_abnormal_ratio": 1})
    with pytest.raises(ValueError):
        validate_config({**DEFAULTS, "weekly_base_low_tolerance_pct": -1})
