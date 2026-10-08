"""Calendar gates, the 15:05 boundary, and failure retry behavior."""
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from tommy_capital import cli
from tommy_capital.data import DataError, SHANGHAI, completed_session
from tommy_capital.market_schedule import CLOSING_CRON, OPENING_CRON, decision


CALENDAR = pd.DataFrame({'trade_date': ['2026-09-30', '2026-10-08', '2026-10-09', '2026-12-31']})


class CalendarSource:
    def __init__(self, calendar=CALENDAR):
        self.calendar = calendar
        self.ttls = []

    def fetch(self, function, **kwargs):
        assert function == 'tool_trade_date_hist_sina'
        self.ttls.append(kwargs['ttl'])
        return self.calendar


@pytest.mark.parametrize('day,expected', [(7, False), (8, True), (9, True), (10, False)])
def test_recurring_scan_uses_real_calendar_not_just_weekdays(day, expected):
    source = CalendarSource()
    now = datetime(2026, 10, day, 15, 5, tzinfo=SHANGHAI)
    run, phase, reason = decision('schedule', CLOSING_CRON, 'auto', now, source)
    assert run is expected and phase == 'closing' and reason
    assert source.ttls == [86400]


def test_daily_close_is_not_limited_to_the_original_one_time_date():
    calendar = pd.DataFrame({'trade_date': ['2027-01-04', '2027-12-31']})
    now = datetime(2027, 1, 4, 15, 5, tzinfo=SHANGHAI)
    assert decision('schedule', CLOSING_CRON, 'auto', now, CalendarSource(calendar))[:2] == (True, 'closing')
    assert decision('schedule', OPENING_CRON, 'auto', now, CalendarSource(calendar))[:2] == (False, 'opening')


def test_unknown_calendar_day_is_failure_not_assumed_holiday():
    stale = CalendarSource(pd.DataFrame({'trade_date': ['2026-09-30']}))
    with pytest.raises(DataError, match='没有覆盖今天'):
        decision('schedule', CLOSING_CRON, 'auto', datetime(2026, 10, 8, 15, 5, tzinfo=SHANGHAI), stale)
    assert stale.ttls == [86400, 0]


def test_old_calendar_refreshes_from_real_source():
    class Refreshing(CalendarSource):
        def fetch(self, function, **kwargs):
            self.ttls.append(kwargs['ttl'])
            return pd.DataFrame({'trade_date': ['2025-12-31']}) if kwargs['ttl'] else CALENDAR
    source = Refreshing()
    now = datetime(2026, 10, 9, 15, 5, tzinfo=SHANGHAI)
    assert decision('schedule', CLOSING_CRON, 'auto', now, source)[:2] == (True, 'closing')
    assert source.ttls == [86400, 0]


def test_manual_pre_run_remains_available_on_holidays():
    assert decision('workflow_dispatch', '', 'auto', datetime(2026, 10, 7, 15, 5, tzinfo=SHANGHAI), None)[:2] == (True, 'auto')


@pytest.mark.parametrize('minute,second,expected', [(4, 59, date(2026, 9, 30)), (5, 0, date(2026, 10, 8))])
def test_complete_daily_session_boundary_is_1505(minute, second, expected):
    assert completed_session(CALENDAR, datetime(2026, 10, 8, 15, minute, second, tzinfo=SHANGHAI)) == expected


def test_1505_closing_reaches_quotes_but_earlier_time_is_rejected(tmp_path):
    class QuoteSentinel(CalendarSource):
        lineage = []
        quote_calls = 0
        def fetch(self, function, **kwargs):
            if function == 'tool_trade_date_hist_sina':
                return CALENDAR
            assert function == 'spot_sina_full'
            self.quote_calls += 1
            raise DataError('actual quote stage reached')
    args = SimpleNamespace(config=None, holdings=None, refresh=False, limit=0, output=str(tmp_path), scan_phase='closing')
    earlier = QuoteSentinel()
    assert cli.run(args, provider=earlier, now=datetime(2026, 10, 8, 15, 4, 59, tzinfo=SHANGHAI)) == 2
    assert earlier.quote_calls == 0
    ready = QuoteSentinel()
    assert cli.run(args, provider=ready, now=datetime(2026, 10, 8, 15, 5, tzinfo=SHANGHAI)) == 2
    assert ready.quote_calls == 1


def test_closing_global_failure_is_retried_and_diagnostics_are_preserved(tmp_path, monkeypatch):
    result = iter([2, 0])
    def attempt(args):
        (tmp_path / 'report.json').write_text('first failure' if not (tmp_path / 'report.json').exists() else 'final real result')
        return next(result)
    delays = []
    monkeypatch.setattr(cli, 'run', attempt)
    monkeypatch.setattr(cli.time, 'sleep', delays.append)
    args = SimpleNamespace(scan_phase='closing', closing_retries=2, output=str(tmp_path))
    assert cli.run_with_closing_retries(args) == 0
    assert delays == [60]
    assert (tmp_path / 'attempts/01/report.json').read_text() == 'first failure'
    assert (tmp_path / 'report.json').read_text() == 'final real result'


@pytest.mark.parametrize('code,phase,expected_calls', [(1, 'closing', 1), (2, 'opening', 1), (2, 'closing', 3)])
def test_partial_is_not_retried_and_global_retries_are_bounded(tmp_path, monkeypatch, code, phase, expected_calls):
    calls, delays = [], []
    monkeypatch.setattr(cli, 'run', lambda args: calls.append(args) or code)
    monkeypatch.setattr(cli.time, 'sleep', delays.append)
    args = SimpleNamespace(scan_phase=phase, closing_retries=2, output=str(tmp_path))
    assert cli.run_with_closing_retries(args) == code
    assert len(calls) == expected_calls and delays == [60] * (expected_calls - 1)
