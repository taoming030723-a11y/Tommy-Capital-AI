"""Offline enrichment keeps research decisions and original appearance history intact."""
import copy
import json
from datetime import datetime

from tommy_capital.cli import clean, make_summary, write_outputs
from tommy_capital.data import SHANGHAI
from tommy_capital.reporting import render_markdown, github_run_url
from tommy_capital.valuation_view import refresh
from tommy_capital.research import freeze_snapshot
from tommy_capital.watchlist import refreshed_entries
from test_watchlist_preferences import report_with_watch, preferences


def valued_report():
    report = report_with_watch()
    code = report["top10_tracking"]["rows"][0]["code"]
    report["valuations"] = {code: {
        "code": code, "session": report["session"], "status": "complete", "label": "相对合理",
        "current_pe_ttm": 18.5, "current_pb": 2.1,
        "historical": {"median_pe_ttm": 19, "percentile": 45},
        "reasons": ["冻结测试估值，不是真实行情"],
    }}
    report["valuation_coverage"] = {"requested": 1, "completed": 1, "influences_ranking": False}
    return report


def test_valuation_refresh_preserves_rankings_history_and_matching_views(tmp_path):
    report = valued_report()
    original = copy.deepcopy(report)
    write_outputs(tmp_path, report)
    refresh(tmp_path)
    saved = json.loads((tmp_path / "report.json").read_text())
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert report == original
    assert saved["rankings"] == clean(original["rankings"])
    assert saved["top10_history"] == original["top10_history"]
    assert summary == clean(make_summary(report))
    assert json.loads((tmp_path / "top10-history.json").read_text()) == original["top10_history"]
    markdown = (tmp_path / "report.md").read_text()
    assert markdown == render_markdown(saved, github_run_url())
    assert "三年PE中位 / 当前分位" in markdown and "相对合理" in markdown
    assert "冻结测试估值，不是真实行情" in markdown


def test_enrichment_preserves_snapshot_hash_and_integral_float_config(tmp_path):
    report = valued_report()
    report["ranking_complete"] = False
    report["config"]["daily_volume_abnormal_ratio"] = 2.0
    snapshot = freeze_snapshot(report)
    write_outputs(tmp_path, report)
    (tmp_path / "enrichment.json").write_text(json.dumps({"valuations": {}, "technical_reviews": {"rows": []}}))
    refresh(tmp_path)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert type(saved["config"]["daily_volume_abnormal_ratio"]) is float
    assert freeze_snapshot(saved) == snapshot
    assert saved["valuations"] == {} and saved["technical_reviews"] == {"rows": []}


def test_enrichment_cannot_update_ranking_config_or_history(tmp_path):
    import pytest
    report = valued_report()
    write_outputs(tmp_path, report)
    original = (tmp_path / "report.json").read_bytes()
    (tmp_path / "enrichment.json").write_text('{"config": {}}')
    with pytest.raises(ValueError, match="禁止修改"):
        refresh(tmp_path)
    assert (tmp_path / "report.json").read_bytes() == original


def test_preference_refresh_retains_enriched_watch_values():
    report = valued_report()
    summary = clean(make_summary(report))
    markdown = render_markdown(report)
    # Force a different preference digest, while the valued stock remains visible.
    entries = refreshed_entries(summary, markdown, summary, markdown, preferences("999999"),
        datetime(2026, 10, 15, 18, tzinfo=SHANGHAI), "test")
    by_path = {e["path"]: e["content"] for e in entries}
    watch = by_path["results/latest-report.md"].split("## 综合前10")[1].split("\n## ")[0]
    assert "相对合理" in watch and "18.50" in watch
    assert json.loads(by_path["results/latest-summary.json"])["valuations"] == summary["valuations"]
