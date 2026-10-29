"""An explicit financial repair must not become an unconditional daily rerun."""
import hashlib
import json

import pytest

from tommy_capital.data import DataError
from tommy_capital.market_schedule import CLOSING_CRON, decision
from test_market_schedule import CalendarSource, write_closing_archive
from test_scan_start_guard import NOW, request


RUN_URL = "https://github.com/taoming030723-a11y/Tommy-Capital-AI/actions/runs/123"
CHECK_TIME = NOW.replace(hour=16)


def repair_request(root, **changes):
    body = request(purpose="financial_recovery", requested_at=CHECK_TIME.isoformat(),
                   expected_report_sha256=hashlib.sha256((root / "latest-summary.json").read_bytes()).hexdigest(),
                   expected_report_run_url=RUN_URL)
    body.update(changes)
    return body


def broken_report(root, **changes):
    return write_closing_archive(root, workflow_run_url=RUN_URL,
                                 errors=["财务质量 cashflow 2026-09-30: 实际行数与源声明不符"], **changes)


def test_explicit_exact_financial_repair_runs_but_cron_and_normal_push_still_skip(tmp_path):
    broken_report(tmp_path)
    source = CalendarSource()
    run, phase, reason = decision("push", "", "auto", CHECK_TIME, source, tmp_path, repair_request(tmp_path))
    assert run and phase == "closing" and "SHA256" in reason
    assert source.ttls == [86400]
    assert not decision("schedule", CLOSING_CRON, "auto", CHECK_TIME, source, tmp_path)[0]
    assert not decision("push", "", "auto", CHECK_TIME, source, tmp_path,
                        request(requested_at=CHECK_TIME.isoformat()))[0]


def test_new_report_invalidates_the_previous_repair_request(tmp_path):
    directory = broken_report(tmp_path)
    old_request = repair_request(tmp_path)
    for path in (directory / "closing-summary.json", tmp_path / "latest-summary.json"):
        value = json.loads(path.read_text())
        value["workflow_run_url"] = RUN_URL + "4"
        path.write_text(json.dumps(value), encoding="utf-8")
    assert not decision("push", "", "auto", CHECK_TIME, CalendarSource(), tmp_path, old_request)[0]


@pytest.mark.parametrize("mode", ["wrong_hash", "wrong_run", "missing_archive", "missing_latest", "invalid_json", "mismatched_markdown", "no_directory"])
def test_report_identity_and_valid_publication_are_both_required(tmp_path, mode):
    directory = broken_report(tmp_path)
    body = repair_request(tmp_path)
    root = tmp_path
    if mode == "wrong_hash": body["expected_report_sha256"] = "f" * 64
    if mode == "wrong_run": body["expected_report_run_url"] = RUN_URL + "1"
    if mode == "missing_archive": (directory / "closing-summary.json").unlink()
    if mode == "missing_latest": (tmp_path / "latest-summary.json").unlink()
    if mode == "invalid_json": (tmp_path / "latest-summary.json").write_text("{")
    if mode == "mismatched_markdown": (tmp_path / "latest-report.md").write_text("扫描中")
    if mode == "no_directory": root = None
    assert not decision("push", "", "auto", CHECK_TIME, CalendarSource(), root, body)[0]


@pytest.mark.parametrize("errors", [[], ["000001: 周/月线不足"], ["某股票描述中提及财务质量，不是源错误"]])
def test_real_history_shortfalls_or_healthy_reports_cannot_force_financial_repair(tmp_path, errors):
    write_closing_archive(tmp_path, workflow_run_url=RUN_URL, errors=errors)
    assert not decision("push", "", "auto", CHECK_TIME, CalendarSource(), tmp_path, repair_request(tmp_path))[0]


@pytest.mark.parametrize("changes", [
    {"purpose": "force_all"}, {"expected_report_sha256": "x" * 64},
    {"expected_report_sha256": None}, {"expected_report_sha256": "0" * 63},
    {"expected_report_run_url": "https://example.com/actions/runs/123"},
    {"expected_report_run_url": RUN_URL + "/jobs/1"}, {"expected_report_run_url": None},
    {"attempt": 3}, {"requested_for": "2026-10-08"},
])
def test_repair_request_keeps_existing_bounds_and_requires_explicit_identity(tmp_path, changes):
    broken_report(tmp_path)
    with pytest.raises(DataError):
        decision("push", "", "auto", CHECK_TIME, CalendarSource(), tmp_path, repair_request(tmp_path, **changes))


def test_financial_repair_cannot_run_after_20_or_on_a_holiday(tmp_path):
    broken_report(tmp_path)
    with pytest.raises(DataError, match="过期"):
        decision("push", "", "auto", CHECK_TIME.replace(hour=20), CalendarSource(), tmp_path,
                 repair_request(tmp_path, requested_at=CHECK_TIME.replace(hour=19).isoformat()))
    holiday = CHECK_TIME.replace(day=10)
    body = repair_request(tmp_path, requested_for="2026-10-10", requested_at=holiday.isoformat())
    assert not decision("push", "", "auto", holiday, CalendarSource(), tmp_path, body)[0]
