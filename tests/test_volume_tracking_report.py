"""Offline protocol/score fixtures, never injected into a market scan."""
import copy
import io
import json
from datetime import datetime

import pandas as pd
import pytest

from tommy_capital.cli import make_summary, write_outputs
from tommy_capital.data import SHANGHAI
from tommy_capital.market_schedule import published_closing_report
from tommy_capital.publish import publish
from tommy_capital.reporting import render_markdown, render_html
from tommy_capital.scoring import balanced_score, TECHNICAL_WEIGHTS, ranking_eligible
from tommy_capital.technical import aggregate, period_volume_observation
from tommy_capital.tracking import (closing_record, load_history, history_with_report,
    tracking_view, attach_tracking, audit_tracking, validate_history)
from test_balanced_top20 import full_technical, frames, report_fixture
from test_core import good_row


@pytest.fixture(autouse=True)
def no_fixture_market_publication(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


@pytest.mark.parametrize("ratio,points", [(.5, 0), (1, 0), (1.5, 2), (2, 4), (8, 4)])
def test_entire_daily_volume_allocation_moves_to_week_and_month(ratio, points):
    technical = full_technical()
    for period in ("weekly", "monthly"):
        technical["timeframes"][period]["structure"]["ratio20"] = ratio
    score = balanced_score(good_row(), technical, "A", minute_frames=frames())
    assert sum(TECHNICAL_WEIGHTS.values()) == 50
    assert [TECHNICAL_WEIGHTS[p] for p in ("monthly", "weekly", "daily")] == [18, 18, 10]
    parts = score["score_breakdown"]["technical"]
    assert parts["monthly"]["parts"]["volume_above_previous20_mean"] == points
    assert parts["weekly"]["parts"]["volume_above_previous20_mean"] == points
    assert not any("volume" in key or "breakout" in key for key in parts["daily"]["parts"])


def test_daily_volume_has_no_direct_points_and_missing_week_volume_cannot_be_rescaled():
    technical = full_technical()
    first = balanced_score(good_row(), technical, "A", minute_frames=frames())
    # Hold entry confirmation constant; ranking has no direct daily volume bonus.
    technical["daily_volume"].update(ratio20=100, abnormal=False)
    second = balanced_score(good_row(), technical, "A", minute_frames=frames())
    assert first == second
    technical["timeframes"]["weekly"]["structure"]["ratio20"] = None
    missing = balanced_score(good_row(), technical, "A", minute_frames=frames())
    assert missing["fundamental_score"] == first["fundamental_score"]
    assert not missing["score_inputs_complete"]
    assert not ranking_eligible({"eligible": True, **missing})
    assert "technical.weekly.volume_ma20" in missing["score_missing_inputs"]


def test_volume_baseline_excludes_tested_period_and_forming_week_month():
    index = pd.bdate_range("2022-01-03", "2026-10-14")
    frame = pd.DataFrame({"open": 10., "close": 10., "high": 11., "low": 9., "volume": 100.}, index=index)
    before = {frequency: period_volume_observation(aggregate(frame, frequency, index[-1].date()))
              for frequency in ("W-FRI", "ME")}
    frame.loc["2026-10-12":, "volume"] = 99999999.
    after = {frequency: period_volume_observation(aggregate(frame, frequency, index[-1].date()))
             for frequency in ("W-FRI", "ME")}
    assert before == after
    assert after["W-FRI"]["confirmed_at"] == "2026-10-09"
    assert after["ME"]["confirmed_at"] == "2026-09-30"
    weeks = aggregate(frame, "W-FRI", index[-1].date()).tail(22).copy()
    weeks.volume = [999999.] + [100.] * 20 + [300.]
    observation = period_volume_observation(weeks)
    assert observation["ratio20"] == 3 and observation["previous20_mean_volume"] == 100
    assert observation["baseline_bars"] == 20
    assert observation["baseline_end"] < observation["confirmed_at"]
    weeks.volume = 0.
    assert period_volume_observation(weeks)["status"] == "unavailable"


def closing(day, count=1):
    report = report_fixture(count)
    report.update(session=day, generated_at=day + "T15:05:00+08:00",
                  finished_at=day + "T15:50:00+08:00", minute15_cutoff=day + "T15:00:00")
    for row in report["rankings"]:
        row["technical"]["daily_volume"] = {"status": "ok", "volume": 200., "previous20_mean_volume": 100.,
            "ratio20": 2., "threshold": 2., "abnormal": True, "baseline_bars": 20,
            "baseline_start": "2026-09-01", "baseline_end": "2026-10-07", "confirmed_at": day}
    report["coverage"].update(universe=count, technical_requested=count, technical_completed=count)
    return report


def accumulated():
    history = {"schema": 1, "threshold": 3, "sessions": []}
    for day in ("2026-10-08", "2026-10-09", "2026-10-12"):
        report = closing(day)
        if day == "2026-10-09":
            report["scoring_version"] = "prior_version"
        history = history_with_report(history, report)
    return history


def test_same_day_final_rerun_replaces_list_without_double_counting():
    history = accumulated()
    report = closing("2026-10-12")
    report["finished_at"] = "2026-10-12T16:00:00+08:00"
    assert len(history_with_report(history, report)["sessions"]) == 3
    view = tracking_view(report, history_with_report(history, report))
    assert view["rows"][0]["appearance_count"] == 3
    report["rankings"][0]["code"] = "600099"
    revised = history_with_report(history, report)
    assert revised["sessions"][-1]["top10"][0]["code"] == "600099"
    assert not tracking_view(report, revised)["rows"]  # Old stock now has only two actual daily appearances.


@pytest.mark.parametrize("changes", [
    {"scan_phase": "pre_run"}, {"scan_phase": "opening", "scan_type": "intraday"},
    {"scan_phase": "model_review", "scan_type": "model_review"}, {"model_review": {"source": "old"}},
    {"status": "failed"}, {"scan_complete": True}, {"minute15_cutoff": "2026-10-12T14:45:00"},
    {"finished_at": "2026-10-13T01:00:00+08:00"},
])
def test_only_finished_real_same_day_closing_reports_add_appearances(changes):
    report = closing("2026-10-12")
    report.update(changes)
    assert closing_record(report, report["rankings"]) is None
    assert history_with_report(accumulated(), report) == accumulated()


def test_tracking_continues_outside_current_pool_and_never_reuses_old_daily_volume():
    report = closing("2026-10-13")
    history = accumulated()
    row = report["rankings"].pop()
    row.update(eligible=False, score_inputs_complete=False)
    report["observations"] = [row]
    watch = tracking_view(report, history)["rows"][0]
    assert watch["appearance_count"] == 3 and watch["current_rank"] is None
    assert not watch["currently_qualified"] and watch["daily_volume"]["confirmed_at"] == "2026-10-13"
    row["technical"]["daily_volume"]["confirmed_at"] = "2026-10-12"
    assert tracking_view(report, history)["rows"][0]["daily_volume"]["status"] == "unavailable"
    assert not report["rankings"]  # Tracking never inserts a stock back into the main pool.


def test_archives_backfill_once_ignore_future_reviews_and_detect_ledger_tampering(tmp_path):
    for day in ("2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"):
        target = tmp_path / day
        target.mkdir()
        (target / "closing-summary.json").write_text(json.dumps(make_summary(closing(day))))
        (target / "model_review-summary.json").write_text(json.dumps(make_summary(closing(day))))
    history, issues = load_history(tmp_path, "2026-10-12")
    assert not issues and len(history["sessions"]) == 3
    (tmp_path / "top10-history.json").write_text(json.dumps(history))
    assert load_history(tmp_path, "2026-10-12") == (history, [])
    tampered = copy.deepcopy(history)
    tampered["sessions"][0]["top10"][0]["score"] += 1
    with pytest.raises(ValueError, match="哈希"):
        validate_history(tampered)
    repeated = {**history, "sessions": history["sessions"] * 2}
    with pytest.raises(ValueError, match="重复"):
        validate_history(repeated)


def test_compact_tables_reasons_html_and_partial_duplicate_guard_remain_consistent(tmp_path):
    report = closing("2026-10-13", 25)
    report["top10_history"] = accumulated()
    report["top10_tracking"] = {**tracking_view(report, accumulated()), "history_status": "ok", "history_issues": []}
    report["rankings"][-1]["name"] = "<script>误标测试</script>"
    markdown = render_markdown(report)
    assert sum(line.startswith("| ---") for line in markdown.splitlines()) == 3
    assert "综合前20实际展示 | 20只" in markdown
    assert markdown.index("## 前20入选理由") > markdown.index("## 综合前10累计至少3次")
    assert "模型验证状态" not in markdown and "数据缺失与复核" not in markdown and "<details>" not in markdown
    assert "scan_complete=false" in markdown and "ranking_complete=false" in markdown
    html = render_html(report)
    assert html.count("<table>") == 3 and html.count("<section>") == 20
    assert "<script>" not in html
    summary = make_summary(report)
    target = tmp_path / "2026-10-13"
    target.mkdir()
    for path in (tmp_path / "latest-summary.json", target / "closing-summary.json"):
        path.write_text(json.dumps(summary))
    for path in (tmp_path / "latest-report.md", target / "closing-report.md"):
        path.write_text(markdown)
    assert published_closing_report(tmp_path, datetime(2026, 10, 13, 16, tzinfo=SHANGHAI))


def test_closing_publication_commits_cumulative_history_with_matching_both_reports(tmp_path, monkeypatch):
    report = closing("2026-10-12")
    attach_tracking(report, tmp_path / "empty-history")
    audit_tracking(report)
    write_outputs(tmp_path, report)
    (tmp_path / "summary.json").write_text(json.dumps(make_summary(report)))
    entries = []
    def respond(request, **kwargs):
        body = json.loads(request.data) if request.data else None
        if request.method == "GET" and "/git/ref/" in request.full_url:
            data = {"object": {"sha": "head"}}
        elif request.method == "GET":
            data = {"tree": {"sha": "base"}}
        elif request.method == "POST" and "/git/trees" in request.full_url:
            entries.extend(body["tree"]); data = {"sha": "tree"}
        else:
            data = {"sha": "commit"}
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    publish(tmp_path, "test-token", "owner/repo")
    files = {entry["path"]: entry["content"] for entry in entries}
    assert json.loads(files["results/top10-history.json"]) == report["top10_history"]
    assert files["results/latest-report.md"] == files["results/2026-10-12/closing-report.md"]
    assert files["results/latest-summary.json"] == files["results/2026-10-12/closing-summary.json"]
