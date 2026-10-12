"""Recovery cannot bypass market gates, duplicate final reports or fake completion."""
from datetime import datetime

import pytest

from tommy_capital.data import DataError, SHANGHAI
from tommy_capital.market_schedule import decision
from tommy_capital.scan_status import updated_state, status_entries, strip_banner, BEGIN
from test_market_schedule import CalendarSource, write_closing_archive


NOW = datetime(2026, 10, 9, 15, 10, tzinfo=SHANGHAI)
REPO = 'taoming030723-a11y/Tommy-Capital-AI'


def request(**changes):
    body = {'schema': 1, 'scan_phase': 'closing', 'requested_for': '2026-10-09',
            'requested_at': '2026-10-09T15:10:00+08:00', 'attempt': 1}
    body.update(changes)
    return body


def test_independent_push_uses_actual_calendar_and_closing_phase():
    source = CalendarSource()
    assert decision('push', '', 'auto', NOW, source, request=request())[:2] == (True, 'closing')
    assert source.ttls == [86400]
    holiday = NOW.replace(day=10)
    assert not decision('push', '', 'auto', holiday, CalendarSource(), request=request(
        requested_for='2026-10-10', requested_at='2026-10-10T15:10:00+08:00'))[0]


@pytest.mark.parametrize('change', [
    {'schema': True}, {'scan_phase': 'opening'}, {'requested_for': '2026-10-08'},
    {'requested_at': '2026-10-09T15:11:00+08:00'}, {'requested_at': '2026-10-09T15:10:00'},
    {'requested_at': '2026-10-09T15:04:00+08:00'}, {'requested_at': 'invalid'},
    {'attempt': 0}, {'attempt': 3}, {'attempt': True},
])
def test_push_cannot_bypass_date_time_phase_or_bounded_retry(change):
    with pytest.raises(DataError):
        decision('push', '', 'auto', NOW, CalendarSource(), request=request(**change))


def test_expired_request_rejected_and_published_partial_skips_recovery(tmp_path):
    with pytest.raises(DataError, match='过期'):
        decision('push', '', 'auto', NOW.replace(hour=18), CalendarSource(), request=request())
    write_closing_archive(tmp_path)
    assert not decision('push', '', 'auto', NOW.replace(minute=35), CalendarSource(), tmp_path, request())[0]


def test_status_changes_old_page_visibility_without_modifying_old_report():
    state = updated_state('start', None, '123', REPO, 'closing', NOW, {'session':'2026-10-08'})
    old = '# 旧报告\n\n扫描完成：2026-10-08 16:00:00\n'
    entries = status_entries(state, old)
    assert {e['path'] for e in entries} == {'results/scan-status.json', 'results/scan-status.md', 'results/latest-report.md'}
    banner = entries[-1]['content']
    assert banner.startswith(BEGIN) and '2026-10-09' in banner and '2026-10-08' in banner
    assert strip_banner(banner) == old
    assert status_entries(state, banner)[-1]['content'] == banner
    assert 'results/latest-summary.json' not in {e['path'] for e in entries}


def test_only_current_run_and_matching_published_report_can_claim_completion():
    state = updated_state('start', None, '123', REPO, 'closing', NOW, {})
    report = {'status':'partial', 'scan_complete':False, 'session':'2026-10-09',
              'generated_at':NOW.isoformat(), 'finished_at':NOW.replace(minute=40).isoformat()}
    published = {**report, 'workflow_run_url':state['run_url']}
    assert updated_state('finish', state, '122', REPO, 'closing', NOW, published, report, 'success') is None
    wrong = {**published, 'workflow_run_url':state['run_url']+'0'}
    assert updated_state('finish', state, '123', REPO, 'closing', NOW, wrong, report, 'success')['status'] == 'publish_failed'
    done = updated_state('finish', state, '123', REPO, 'closing', NOW, published, report, 'success')
    assert done['status'] == 'partial' and done['scan_complete'] is False
    assert done['report_session'] == '2026-10-09'
    assert status_entries(done, '# 最终报告\n')[-1]['content'] == '# 最终报告\n'


def test_publish_failure_stays_visible_on_latest_page():
    state = updated_state('start', None, '123', REPO, 'closing', NOW, {'session':'2026-10-08'})
    failed = updated_state('finish', state, '123', REPO, 'closing', NOW, {}, None, 'failure')
    assert '报告发布未完成' in status_entries(failed, '# 昨日报告\n')[-1]['content']
