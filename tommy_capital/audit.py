"""Verify a finished real scan before publication; no market inputs are generated here."""
import csv
import json
from pathlib import Path

from .cli import make_summary
from .reporting import render_markdown, github_run_url
from .scoring import balanced_score, ranked_top20, ranking_eligible, SCORING_VERSION
from .strategy import base_checks


def audit(directory):
    root = Path(directory)
    report = json.loads((root / "report.json").read_text())
    summary = json.loads((root / "summary.json").read_text())
    assert report["status"] in ("complete", "partial", "failed")
    if report["status"] == "failed":
        assert report["scan_complete"] is False and not report["rankings"]
        assert summary == make_summary(report)
        assert report["errors"]
        proof = {"status": "failed", "generated_at": report["generated_at"], "errors": report["errors"]}
        (root / "verification.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2))
        print("TOP20_AUDIT=" + json.dumps(proof, ensure_ascii=False))
        return proof
    assert report["scoring_version"] == SCORING_VERSION
    assert report["selection_rule"] == "dual_route_monthly_recovery"
    closing = report["scan_phase"] == "closing"
    day = report["generated_at"][:10]
    if closing:
        assert report["scan_type"] == "after_close" and report["session"] == day
        assert report["minute15_cutoff"][:10] == day and report["minute15_cutoff"][11:16] >= "15:00"
    elif report["scan_phase"] in ("opening", "intraday"):
        assert report["scan_type"] == "intraday" and report["session"] < day
        assert report["minute15_cutoff"][:10] == day and report["minute15_cutoff"][11:16] >= "09:45"
    else:
        assert report["scan_phase"] == "pre_run" and report["scan_type"] in ("closed_session", "daily_baseline")
    assert summary == make_summary(report)
    assert (root / "report.md").read_text() == render_markdown(report, github_run_url())
    coverage, pool = report["coverage"], report["rankings"]
    assert coverage["technical_scope"] == "all_quoted_a_shares"
    assert coverage["technical_requested"] == coverage["technical_attempted"] == coverage["universe"]
    assert coverage["unscanned"] == 0
    assert coverage["technical_completed"] + coverage["technical_failed"] == coverage["technical_attempted"]
    completed = pool + report["observations"]
    missing = [r for r in report["data_gaps"] if not r["resolved"]]
    assert len({r["code"] for r in completed}) == len(completed) == coverage["technical_completed"]
    assert len({r["code"] for r in missing}) == len(missing) == coverage["technical_failed"]
    assert not ({r["code"] for r in completed} & {r["code"] for r in missing})
    assert {r["code"] for r in completed + missing} == set(report["quoted_codes"])
    assert len(report["quoted_codes"]) == len(set(report["quoted_codes"])) == coverage["universe"]
    assert len(list(csv.DictReader((root / "rankings.csv").open()))) == len(pool)
    assert len(list(csv.DictReader((root / "observations.csv").open()))) == len(report["observations"])
    top = ranked_top20(pool)
    eligible = sum(ranking_eligible(row) for row in pool)
    assert len(top) == min(20, eligible) == min(20, summary["ranking_eligible_count"])
    for row in completed:
        tech = row["technical"]
        assert tech["daily_bar_date"] == report["session"]
        if closing:
            assert abs(tech["daily_close_qfq"] - row["price"]) <= .011
        volume = tech["daily_volume"]
        assert volume["baseline_end"] < volume["confirmed_at"] == report["session"]
        assert volume["baseline_bars"] == 20
        for frame in tech["timeframes"].values():
            assert all(s.get("quality_passed") is True for s in frame["signals"] if s["indicator"] == "MACD_DIF")
        if tech["monthly_history_source"] == "validated_native_qfqmonth":
            assert tech["monthly_history_validation"]["forming_period_excluded"]
            assert tech["monthly_history_validation"]["latest_complete_period"] == tech["monthly_bar_date"]
    from datetime import date
    for row in pool:
        actual = balanced_score(row, row["technical"], row["route"], row["leading"], row["minute_frames"])
        for key in ("score", "fundamental_score", "technical_score", "score_breakdown", "fundamental_quality", "score_inputs_complete"):
            assert row[key] == actual[key], (row["code"], key)
        assert 0 <= row["fundamental_score"] <= 50 and 0 <= row["technical_score"] <= 50
        assert row["ranking_eligible"] is ranking_eligible(row)
        path = row["technical"]["monthly_recovery"]
        if row["route"] == "A":
            assert not base_checks(row, report["config"], date.fromisoformat(day))
            assert path["current_j"] < 20 or path["min_j_3_months"] < 20 and path["rising"]
        else:
            assert row["financial_routes"]["route_b_passed"] and path["route_b_path"]
        for frame in row["minute_frames"].values():
            if frame["status"] == "ok":
                if closing:
                    assert frame["last_bar"][:10] == day and frame["last_bar"][11:16] >= "15:00"
                else:
                    assert frame["last_bar"] <= report.get("minute15_cutoff", frame["last_bar"])
                assert all(s.get("quality_passed") is True for s in frame["signals"] if s["indicator"] == "MACD_DIF")
        assert not row["confirmed_holding"]  # Hosted scans have no confirmed current holdings file.
    proof = {"generated_at": report["generated_at"], "session": report["session"],
        "status": report["status"], "selection_rule": report["selection_rule"], "scoring_version": SCORING_VERSION,
        "scan_complete": report["scan_complete"], "ranking_complete": report["ranking_complete"],
        "ranking_incomplete_reasons": report["ranking_incomplete_reasons"], "coverage": coverage,
        "displayed": len(top), "top20": [{k: row[k] for k in
            ("code", "name", "score", "fundamental_score", "technical_score", "fundamental_quality")} for row in top]}
    (root / "verification.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2))
    print("TOP20_AUDIT=" + json.dumps(proof, ensure_ascii=False))
    return proof


if __name__ == "__main__":
    audit("reports/latest")
