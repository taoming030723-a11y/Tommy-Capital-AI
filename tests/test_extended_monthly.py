"""Offline protocol fixtures protect monthly history; never used as live market inputs."""
from datetime import date
import numpy as np
import pandas as pd
import pytest
from tommy_capital import live
from tommy_capital.data import DataError
from tommy_capital.technical import aggregate, extended_monthly_history, strategic_technical


def daily_fixture():
    dates = pd.bdate_range("2018-01-01", "2026-09-30")
    price = np.linspace(10, 35, len(dates))
    return pd.DataFrame({"open": price, "close": price + .1, "high": price + 1,
        "low": price - 1, "volume": np.ones(len(dates)) * 100, "amount": np.ones(len(dates)) * 5e7},
        index=pd.DatetimeIndex(dates, name="date"))


def raw_months(frame):
    return frame.reset_index().rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
        "high": "最高", "low": "最低", "volume": "成交量"})


class MonthlyFixture:
    def __init__(self, frame):
        self.frame, self.calls = frame, []
    def fetch(self, function, **kwargs):
        self.calls.append((function, kwargs))
        assert function == "monthly_tx_qfq"
        return raw_months(self.frame)


def test_long_monthly_history_keeps_history_and_unchanged_matching_prices():
    full = daily_fixture()
    native = aggregate(full, "ME", date(2026, 10, 8))
    provider = MonthlyFixture(native)
    result = extended_monthly_history(provider, "600001", full.tail(640), date(2026, 10, 8), date(2026, 10, 8))
    assert len(result) > 35
    assert result.index[-1] == pd.Timestamp("2026-09-30")
    assert result.attrs["price_validation"]["matched_complete_months"] >= 24
    assert provider.calls[0][1]["anchor_date"] == "2026-10-08"
    technical = strategic_technical(full.tail(640), date(2026, 9, 30), date(2026, 10, 7), monthly_history=result)
    assert technical["monthly_history_source"] == "validated_native_qfqmonth"
    assert technical["timeframes"]["monthly"]["divergence_status"] == "ok"
    assert technical["monthly_history_validation"]["forming_period_excluded"]


@pytest.mark.parametrize("column", ["open", "close", "high", "low"])
def test_mismatched_qfq_ohlc_is_rejected_in_older_complete_overlap(column):
    full = daily_fixture()
    native = aggregate(full, "ME", date(2026, 10, 8))
    native.loc[pd.Timestamp("2025-09-30"), column] += .10
    # Keep the fixture's OHLC internally valid so the pairing guard is exercised.
    if column == "close":
        native.loc[pd.Timestamp("2025-09-30"), "high"] += .10
    with pytest.raises(DataError, match="不一致"):
        extended_monthly_history(MonthlyFixture(native), "600001", full.tail(640),
            date(2026, 10, 8), date(2026, 10, 8))


def test_missing_recent_complete_month_is_rejected():
    full = daily_fixture()
    native = aggregate(full, "ME", date(2026, 10, 8)).drop(pd.Timestamp("2026-08-31"))
    with pytest.raises(DataError, match="缺少"):
        extended_monthly_history(MonthlyFixture(native), "600001", full.tail(640),
            date(2026, 10, 8), date(2026, 10, 8))


def test_accepted_long_source_does_not_import_forming_month():
    full = daily_fixture()
    native = aggregate(full, "ME", date(2026, 10, 8))
    native.loc[pd.Timestamp("2026-10-31")] = native.iloc[-1]
    result = extended_monthly_history(MonthlyFixture(native), "600001", full.tail(640),
        date(2026, 10, 8), date(2026, 10, 8))
    assert result.index[-1] == pd.Timestamp("2026-09-30")


def test_monthly_client_normalizes_completed_period_and_preserves_historical_anchor(monkeypatch):
    calls = []
    def source(url, params):
        calls.append(params["param"])
        records = [["2000-09-29", "10", "11", "12", "9", "100"],
                   ["2000-10-08", "11", "12", "13", "10", "200"]]
        return {"data": {"sh600001": {"qfqmonth": records}}}
    monkeypatch.setattr(live, "get_json", source)
    frame = live.monthly_tx_qfq("600001", "2000-10-08", "2000-10-08")
    assert calls == ["sh600001,month,,2000-10-08,120,qfq"]
    assert frame["日期"].dt.strftime("%Y-%m-%d").tolist() == ["2000-09-30"]
    assert frame["收盘"].tolist() == ["11"]


def test_monthly_client_rejects_raw_fallback(monkeypatch):
    monkeypatch.setattr(live, "get_json", lambda *a, **k:
        {"data": {"sh600001": {"month": [["2000-09-29", "10", "11", "12", "9", "100"]]}}})
    with pytest.raises(ValueError, match="qfqmonth"):
        live.monthly_tx_qfq("600001", "2000-10-08", "2000-10-08")
