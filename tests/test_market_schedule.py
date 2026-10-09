"""Calendar gates, the 15:05 boundary, and failure retry behavior."""
import json
import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from tommy_capital import cli
from tommy_capital.data import DataError, SHANGHAI, completed_session
from tommy_capital.market_schedule import CLOSING_CRON, CLOSING_FALLBACK_CRONS, OPENING_CRON, decision, published_closing_report


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


def write_closing_archive(root, **changes):
    summary = {
        'generated_at': '2026-10-09T15:05:00+08:00',
        'finished_at': '2026-10-09T15:28:00+08:00',
        'status': 'partial', 'scan_complete': False,
        'session': '2026-10-09', 'scan_type': 'after_close', 'scan_phase': 'closing',
        'minute15_cutoff': '2026-10-09T15:00:00',
        'selection_rule': 'dual_route_monthly_recovery',
        'scoring_version': 'balanced_50_50_v1', 'display_limit': 20,
        'candidate_count': 261, 'top_candidates': [{'code': str(i).zfill(6)} for i in range(20)],
        'coverage': {'universe': 5571},
    }
    summary.update(changes)
    directory = root / '2026-10-09'
    directory.mkdir(exist_ok=True)
    (directory / 'closing-summary.json').write_text(json.dumps(summary), encoding='utf-8')
    (directory / 'closing-report.md').write_text(
        '扫描启动：2026-10-09 15:05:00\n扫描完成：2026-10-09 15:28:00\n', encoding='utf-8')
    (root / 'latest-summary.json').write_text((directory / 'closing-summary.json').read_text(), encoding='utf-8')
    (root / 'latest-report.md').write_text((directory / 'closing-report.md').read_text(), encoding='utf-8')
    return directory


@pytest.mark.parametrize('cron', (CLOSING_CRON, *CLOSING_FALLBACK_CRONS))
def test_primary_and_fallbacks_skip_an_already_published_partial(tmp_path, cron):
    write_closing_archive(tmp_path)
    run, phase, reason = decision('schedule', cron, 'auto', datetime(2026, 10, 9, 16, 5, tzinfo=SHANGHAI), CalendarSource(), tmp_path)
    assert not run and phase == 'closing' and '已发布' in reason
    assert json.loads((tmp_path / '2026-10-09/closing-summary.json').read_text())['scan_complete'] is False


@pytest.mark.parametrize('cron', CLOSING_FALLBACK_CRONS)
def test_fallback_scans_if_primary_never_published(tmp_path, cron):
    now = datetime(2026, 10, 9, 15, 35, tzinfo=SHANGHAI)
    assert decision('schedule', cron, 'auto', now, CalendarSource(), tmp_path)[:2] == (True, 'closing')
    assert not decision('schedule', cron, 'auto', now.replace(day=10), CalendarSource(), tmp_path)[0]


@pytest.mark.parametrize('changes', [
    {'status': 'failed', 'candidate_count': 0, 'top_candidates': []},
    {'status': 'running'},
    {'scan_type': 'intraday', 'scan_phase': 'opening'},
    {'session': '2026-09-30'},
    {'generated_at': '2026-10-08T23:00:00+08:00'},
    {'finished_at': '2026-10-09T16:00:00+08:00'},
    {'minute15_cutoff': '2026-10-09T14:45:00'},
    {'minute15_cutoff': '2026-10-08T15:00:00'},
    {'coverage': None},
    {'coverage': {'universe': 0}},
    {'top_candidates': []},
    {'selection_rule': 'monthly_j_lt_20'},
])
def test_invalid_or_unfinished_closing_files_cannot_suppress_recovery(tmp_path, changes):
    write_closing_archive(tmp_path, **changes)
    now = datetime(2026, 10, 9, 15, 35, tzinfo=SHANGHAI)
    assert decision('schedule', CLOSING_FALLBACK_CRONS[0], 'auto', now, CalendarSource(), tmp_path)[:2] == (True, 'closing')


def test_recovery_requires_matching_human_report_and_readable_json(tmp_path):
    directory = write_closing_archive(tmp_path)
    now = datetime(2026, 10, 9, 15, 35, tzinfo=SHANGHAI)
    (directory / 'closing-report.md').write_text('扫描启动：2026-10-08 23:00:00', encoding='utf-8')
    assert not published_closing_report(tmp_path, now)
    (directory / 'closing-report.md').unlink()
    assert not published_closing_report(tmp_path, now)
    write_closing_archive(tmp_path)
    (directory / 'closing-summary.json').write_text('{unfinished', encoding='utf-8')
    assert not published_closing_report(tmp_path, now)


def test_complete_and_true_zero_candidates_are_also_final(tmp_path):
    write_closing_archive(tmp_path, status='complete', scan_complete=True, candidate_count=0, top_candidates=[])
    now = datetime(2026, 10, 9, 15, 35, tzinfo=SHANGHAI)
    assert published_closing_report(tmp_path, now)
    # An explicit manual rerun remains available even with today's valid archive.
    assert decision('workflow_dispatch', '', 'closing', now, None, tmp_path)[:2] == (True, 'closing')


def test_current_archive_with_stale_latest_page_does_not_suppress_repair(tmp_path):
    write_closing_archive(tmp_path)
    (tmp_path / 'latest-report.md').write_text('昨晚的报告', encoding='utf-8')
    now = datetime(2026, 10, 9, 15, 35, tzinfo=SHANGHAI)
    assert decision('schedule', CLOSING_FALLBACK_CRONS[0], 'auto', now, CalendarSource(), tmp_path)[0]


def test_workflow_and_calendar_gate_keep_the_same_primary_and_recovery_crons():
    workflow = (Path(__file__).parents[1] / '.github/workflows/screen.yml').read_text()
    assert set(re.findall(r"cron: '([^']+)'", workflow)) == {OPENING_CRON, CLOSING_CRON, *CLOSING_FALLBACK_CRONS}
    assert 'ref: main' in workflow and 'cancel-in-progress: false' in workflow
