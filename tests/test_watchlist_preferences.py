"""User-view controls operate on offline fixtures, never real scan inputs."""
import base64
import copy
import io
import json
import urllib.error
from datetime import datetime

import pytest

from tommy_capital.cli import make_summary, write_outputs
from tommy_capital.data import SHANGHAI
from tommy_capital.reporting import render_markdown
from tommy_capital.scoring import FUNDAMENTAL_WEIGHTS, TECHNICAL_WEIGHTS, balanced_score
from tommy_capital.tracking import (PREFERENCES_PATH, apply_preferences, attach_tracking, audit_tracking,
    digest, load_preferences, tracking_view, validate_preferences)
from tommy_capital.watchlist import changed_preferences, manage, refreshed_entries
from tommy_capital.publish import publish
from test_balanced_top20 import full_technical, frames
from test_core import good_row
from test_volume_tracking_report import accumulated, closing


def preferences(*codes):
    return {"schema": 1, "ignored_codes": list(codes)}


def report_with_watch():
    report = closing("2026-10-13", 2)
    report["top10_history"] = accumulated()
    report["top10_tracking"] = {**tracking_view(report, accumulated()), "history_status": "ok", "history_issues": []}
    return report


def test_ma55_is_twelve_of_thirty_and_total_remains_exact_half():
    assert sum(FUNDAMENTAL_WEIGHTS.values()) == sum(TECHNICAL_WEIGHTS.values()) == 50
    assert TECHNICAL_WEIGHTS=={"monthly":20,"weekly":30}
    result = balanced_score(good_row(), full_technical(), "A", minute_frames=frames())
    parts = result["score_breakdown"]["technical"]["weekly"]["parts"]
    assert sum(v for k, v in parts.items() if k.startswith("weekly_ma55_") and not k.endswith("penalty")) == 12
    assert result["technical_score"] == 50


@pytest.mark.parametrize("invalid", [
    {"schema": True, "ignored_codes": []}, {"schema": 2, "ignored_codes": []},
    {"schema": 1, "ignored_codes": [2780]}, preferences("2780"), preferences("002780", "002780"),
    preferences("１２３４５６"), preferences("600001;echo bad"), {"schema": 1},
])
def test_preferences_reject_ambiguous_or_command_like_codes(invalid):
    with pytest.raises(ValueError):
        validate_preferences(invalid)


def test_ignore_is_durable_idempotent_and_restore_keeps_original_appearance_count():
    report, history = report_with_watch(), accumulated()
    original = tracking_view(report, history)
    code = original["rows"][0]["code"]
    saved = changed_preferences(preferences(), "忽略", code)
    assert changed_preferences(saved, "ignore", code) == saved
    view = tracking_view(report, history, saved)
    assert not view["rows"] and view["suppressed_count"] == 1
    assert view["all_rows"] == original["rows"] and report["top10_history"] == history
    restored = apply_preferences(view, changed_preferences(saved, "恢复", code))
    assert restored["rows"] == original["rows"] and restored["rows"][0]["appearance_count"] == 3
    assert report["rankings"]  # Hiding does not delete the main research candidate.


def test_future_reports_still_hide_selected_code_without_erasing_ledger(tmp_path):
    config = tmp_path / "preferences.json"
    code = accumulated()["sessions"][0]["top10"][0]["code"]
    config.write_text(json.dumps(preferences(code)))
    (tmp_path / "top10-history.json").write_text(json.dumps(accumulated()))
    report = closing("2026-10-13")
    attach_tracking(report, tmp_path, config)
    audit_tracking(report)
    assert not report["top10_tracking"]["rows"] and report["top10_tracking"]["all_rows"][0]["appearance_count"] == 4
    assert len(report["top10_history"]["sessions"]) == 4
    config.write_text('{"schema":1,"ignored_codes":[2780]}')
    attach_tracking(report, tmp_path, config)
    audit_tracking(report)
    assert report["top10_tracking"]["preferences_status"] == "invalid" and not report["top10_tracking"]["rows"]
    assert load_preferences(tmp_path / "missing.json") == preferences()


def test_preference_refresh_changes_only_watch_view_and_preserves_market_fields_top20_and_body():
    report = report_with_watch()
    summary, markdown = make_summary(report), render_markdown(report)
    original = copy.deepcopy(summary)
    code = summary["top10_tracking"]["rows"][0]["code"]
    now = datetime(2026, 10, 15, 18, 30, tzinfo=SHANGHAI)
    entries = refreshed_entries(summary, markdown, summary, markdown, preferences(code), now, "owner")
    files = {e["path"]: e["content"] for e in entries}
    changed = json.loads(files["results/latest-summary.json"])
    assert all(changed[k] == v for k, v in original.items() if k != "top10_tracking")
    assert summary == original
    assert not changed["top10_tracking"]["rows"] and changed["top10_tracking"]["all_rows"] == original["top10_tracking"]["rows"]
    assert changed["top10_tracking"]["preferences_updated_at"] == now.isoformat()
    assert files["results/latest-summary.json"] == files["results/2026-10-13/closing-summary.json"]
    updated_md = files["results/latest-report.md"]
    assert updated_md == files["results/2026-10-13/closing-report.md"]
    assert markdown.split("## 综合前10")[0] == updated_md.split("## 综合前10")[0]
    assert markdown.split("## 前20入选理由")[1] == updated_md.split("## 前20入选理由")[1]
    assert "观察股票均已忽略" in updated_md and "/actions/workflows/watchlist.yml" in updated_md
    assert sum(line.startswith("| ---") for line in updated_md.splitlines()) == 3
    assert not refreshed_entries(changed, updated_md, changed, updated_md, preferences(code), now, "owner")
    restored = refreshed_entries(changed, updated_md, changed, updated_md, preferences(), now, "owner")
    restored_summary = json.loads(restored[0]["content"])
    assert restored_summary["top10_tracking"]["rows"] == original["top10_tracking"]["rows"]


def test_no_valid_closing_report_never_becomes_a_new_market_report():
    report = report_with_watch()
    summary, markdown = make_summary(report), render_markdown(report)
    invalid = {**summary, "scan_phase": "model_review"}
    now = datetime(2026, 10, 15, 18, tzinfo=SHANGHAI)
    assert refreshed_entries(invalid, markdown, invalid, markdown, preferences(), now, "owner") == []
    assert refreshed_entries(summary, markdown, {**summary, "status": "failed"}, markdown, preferences(), now, "owner") == []


def test_management_rebases_on_concurrent_main_and_rechecks_current_settings(monkeypatch):
    code = accumulated()["sessions"][0]["top10"][0]["code"]
    report = report_with_watch()
    summary, markdown = make_summary(report), render_markdown(report)
    state = {"head": "head-one", "patches": 0, "trees": []}
    def encoded(value):
        return {"content": base64.b64encode(value.encode()).decode()}
    def respond(request, **kwargs):
        body = json.loads(request.data) if request.data else None
        url = request.full_url
        if request.method == "GET" and "/git/ref/" in url:
            data = {"object": {"sha": state["head"]}}
        elif request.method == "GET" and "/git/commits/" in url:
            data = {"tree": {"sha": "tree-" + state["head"]}}
        elif request.method == "GET":
            if PREFERENCES_PATH in url:
                # A concurrent, different ignore selection must survive the retry.
                data = encoded(json.dumps(preferences("002780") if state["head"] == "head-two" else preferences()))
            elif url.split("?")[0].endswith(".json"):
                data = encoded(json.dumps(summary))
            else:
                data = encoded(markdown)
        elif request.method == "POST" and "/git/trees" in url:
            state["trees"].append(body); data = {"sha": "new-tree"}
        elif request.method == "POST":
            data = {"sha": "commit-" + state["head"]}
        else:
            state["patches"] += 1
            if state["patches"] == 1:
                state["head"] = "head-two"
                raise urllib.error.HTTPError(url, 422, "changed head", {}, None)
            data = {"object": {"sha": body["sha"]}}
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    result = manage("owner/repo", "offline-token", "ignore", code, datetime(2026, 10, 15, 18, tzinfo=SHANGHAI), "owner")
    assert result["refreshed"] and result["preferences"] == preferences(*sorted({"002780", code}))
    assert state["trees"][-1]["base_tree"] == "tree-head-two"
    assert {e["path"] for e in state["trees"][-1]["tree"]} == {
        PREFERENCES_PATH, "results/latest-summary.json", "results/latest-report.md",
        "results/2026-10-13/closing-summary.json", "results/2026-10-13/closing-report.md"}


def test_scan_publisher_honors_new_ignore_selection_made_after_scan_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    (tmp_path / "top10-history.json").write_text(json.dumps(accumulated()))
    report = closing("2026-10-13")
    attach_tracking(report, tmp_path, tmp_path / "not-yet-created.json")
    code = report["top10_tracking"]["rows"][0]["code"]
    write_outputs(tmp_path, report)
    original_rankings = json.loads((tmp_path / "report.json").read_text())["rankings"]
    (tmp_path / "summary.json").write_text(json.dumps(make_summary(report)))
    entries = []
    def respond(request, **kwargs):
        body = json.loads(request.data) if request.data else None
        url = request.full_url
        if request.method == "GET" and "/git/ref/" in url:
            data = {"object": {"sha": "new-user-preference-head"}}
        elif request.method == "GET" and "/contents/" in url:
            data = {"content": base64.b64encode(json.dumps(preferences(code)).encode()).decode()}
        elif request.method == "GET":
            data = {"tree": {"sha": "base"}}
        elif request.method == "POST" and "/git/trees" in url:
            entries.extend(body["tree"]); data = {"sha": "tree"}
        else:
            data = {"sha": "commit"}
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    publish(tmp_path, "offline-token", "owner/repo")
    files = {e["path"]: e["content"] for e in entries}
    current = json.loads(files["results/latest-summary.json"])
    assert not current["top10_tracking"]["rows"] and current["top10_tracking"]["all_rows"]
    assert current["top10_tracking"]["preferences_source_commit"] == "new-user-preference-head"
    assert json.loads((tmp_path / "report.json").read_text())["rankings"] == original_rankings
    assert "观察股票均已忽略" in files["results/latest-report.md"]
    assert files["results/latest-report.md"] == files["results/2026-10-13/closing-report.md"]
