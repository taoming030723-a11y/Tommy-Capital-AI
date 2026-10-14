"""Offline source fixtures verify long-week chronology/adjustment; no live prices generated."""
from datetime import date

import pandas as pd
import pytest

from tommy_capital import live
from tommy_capital.data import DataError
from tommy_capital.technical import aggregate, extended_weekly_history, strategic_technical
from test_extended_monthly import daily_fixture, raw_months


class WeeklyFixture:
    def __init__(self, frame):
        self.frame, self.calls = frame, []
    def fetch(self, function, **kwargs):
        assert function == "weekly_tx_qfq"
        self.calls.append(kwargs)
        return raw_months(self.frame)


def test_native_long_week_preserves_every_complete_overlapping_qfq_period():
    daily = daily_fixture()
    native = aggregate(daily, "W-FRI", date(2026, 10, 13))
    provider = WeeklyFixture(native)
    weekly = extended_weekly_history(provider, "600001", daily.tail(640), date(2026, 10, 13), date(2026, 10, 14))
    assert len(weekly) >= 233
    assert weekly.index[-1] == native.index[-1] <= pd.Timestamp("2026-10-13")
    assert weekly.attrs["price_validation"]["matched_complete_weeks"] > 100
    assert provider.calls[0]["max_periods"] == 300


def test_inconsistent_adjusted_week_is_rejected_without_using_raw_prices():
    daily = daily_fixture()
    native = aggregate(daily, "W-FRI", date(2026, 10, 13))
    native.iloc[-1, native.columns.get_loc("close")] += .02
    with pytest.raises(DataError, match="价格不一致"):
        extended_weekly_history(WeeklyFixture(native), "600001", daily.tail(640), date(2026, 10, 13), date(2026, 10, 14))


def test_week_end_normalization_does_not_advance_a_friday_or_include_forming_week(monkeypatch):
    calls = []
    def get(url, params):
        calls.append(params["param"])
        return {"data": {"sh600001": {"qfqweek": [["2000-10-06", "10", "11", "12", "9", "100"],
            ["2000-10-12", "11", "12", "13", "10", "200"]]}}}
    monkeypatch.setattr(live, "get_json", get)
    weekly = live.weekly_tx_qfq("600001", "2000-10-12", "2000-10-12")
    assert calls == ["sh600001,week,,2000-10-12,300,qfq"]
    assert weekly["日期"].dt.strftime("%Y-%m-%d").tolist() == ["2000-10-06"]


def test_native_week_and_month_clients_refuse_raw_fallback_and_invalid_counts(monkeypatch):
    monkeypatch.setattr(live, "get_json", lambda *a, **k: {"data": {"sh600001": {"week": []}}})
    with pytest.raises(ValueError, match="qfqweek"):
        live.weekly_tx_qfq("600001", "2000-10-12", "2000-10-12")
    with pytest.raises(ValueError, match="数量"):
        live.weekly_tx_qfq("600001", "2000-10-12", "2000-10-12", True)
    with pytest.raises(ValueError, match="数量"):
        live.monthly_tx_qfq("600001", "2000-10-12", "2000-10-12", 641)
