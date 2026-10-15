"""Verify a finished real scan before publication; no market inputs are generated here."""
import csv
import hashlib
import json
from pathlib import Path

from .cli import make_summary
from .reporting import render_markdown, github_run_url
from .scoring import balanced_score, ranked_top20, ranking_eligible, SCORING_VERSION
from .strategy import base_checks
from .patterns import PROFILE_VERSION, WEEKLY_MA55_VERSION


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
    reviewing = report["scan_phase"] == "model_review"
    closing = report["scan_phase"] == "closing" or reviewing
    day = report["model_review"]["source_generated_at"][:10] if reviewing else report["generated_at"][:10]
    if reviewing:
        review = report["model_review"]
        original_bytes = (root / "source-report.json").read_bytes()
        assert hashlib.sha256(original_bytes).hexdigest() == review["source_report_sha256"]
        original = json.loads(original_bytes)
        assert original["scan_phase"] == "closing" and original["scan_type"] == "after_close"
        assert review["new_market_requests"] == review["new_evidence_requests"] == 0
        assert report["session"] == original["session"] == day <= report["generated_at"][:10]
        assert report["minute15_cutoff"] == original["minute15_cutoff"]
        assert report["errors"] == original["errors"] and report["data_gaps"] == original["data_gaps"]
        assert report["observations"] == original["observations"] and report["quoted_codes"] == original["quoted_codes"]
        mutable = {"score", "fundamental_score", "technical_score", "score_breakdown", "score_inputs_complete", "score_missing_inputs",
                   "fundamental_quality", "scoring_version", "ranking_eligible", "confirmation_state", "ranking_purpose", "profit_status"}
        source_rows = {r["code"]: r for r in original["rankings"]}
        assert {r["code"] for r in report["rankings"]} == set(source_rows)
        for row in report["rankings"]:
            assert all(row[k] == v for k, v in source_rows[row["code"]].items() if k not in mutable)
        for key, val in original["coverage"].items():
            if key not in {"ranking_eligible_count", "score_inputs_completed", "confirmed_ranking_count", "financial_sector_review_missing"}:
                assert report["coverage"][key] == val
    if closing:
        assert report["scan_type"] == ("model_review" if reviewing else "after_close") and report["session"] == day
        assert report["minute15_cutoff"][:10] == day and report["minute15_cutoff"][11:16] >= "15:00"
    elif report["scan_phase"] in ("opening", "intraday"):
        assert report["scan_type"] == "intraday" and report["session"] < day
        assert report["minute15_cutoff"][:10] == day and report["minute15_cutoff"][11:16] >= "09:45"
    else:
        assert report["scan_phase"] == "pre_run" and report["scan_type"] in ("closed_session", "daily_baseline")
    assert summary == make_summary(report)
    if report.get("macd_monitor") is not None:
        from .monitor import validate_public_monitor,monitor_scope
        monitor=report["macd_monitor"]
        state_file=root/"monitor-state.json"
        validate_public_monitor(monitor,json.loads(state_file.read_text()) if state_file.exists() else None)
        if monitor.get("status") not in ("unavailable","configuration_changed"):
            expected=monitor_scope(report=report,preferences=report["top10_tracking"]["preferences"])
            assert monitor["scope"]==expected
            assert {r["code"] for r in monitor["rows"]}=={r["code"] for r in expected}
            assert monitor["monitor_complete"] is all(r["data_complete"] for r in monitor["rows"])
        for row in monitor.get("rows",[]):
            assert set(row["frames"])=={"daily","120","60","30","15"}
            assert row["data_complete"] is all(f["status"]=="ok" for f in row["frames"].values())
            for frame in row["frames"].values():
                if frame["status"]=="ok":
                    assert frame["last_bar"]<=monitor["checked_at"][:19]
                    assert frame.get("source")
    assert (root / "report.md").read_text() == render_markdown(report, github_run_url())
    if report.get("top10_history") is not None:
        from .tracking import audit_tracking
        assert audit_tracking(report) == json.loads((root / "top10-history.json").read_text())
    if (root / "research-snapshot.json").exists():
        from .research import freeze_snapshot, validate_snapshot
        snapshot = json.loads((root / "research-snapshot.json").read_text())
        validate_snapshot(snapshot)
        assert snapshot == freeze_snapshot(report)
        evaluation = json.loads((root / "forward-evaluation.json").read_text())
        assert report["forward_evaluation"] == {k: v for k, v in evaluation.items() if k not in ("checks", "lineage")}
        assert all(c.get("exit_session", "") <= evaluation["as_of_session"] for c in evaluation["checks"] if c["status"] == "observed")
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
        for period in ("weekly", "monthly"):
            frame = tech["timeframes"][period]
            period_volume = frame.get("volume_ma20")
            if period_volume is not None and period_volume["status"] == "ok":
                assert period_volume["baseline_bars"] == 20
                assert period_volume["baseline_end"] < period_volume["confirmed_at"] == tech[period + "_bar_date"]
                assert abs(period_volume["ratio20"] - period_volume["volume"] / period_volume["previous20_mean_volume"]) < 1e-10
        for period in ("monthly", "weekly", "daily"):
            frame = tech["timeframes"][period]
            if period=="daily" and frame.get("status")=="not_requested":
                continue
            profile = frame["macro_profile"]
            assert profile["version"] == PROFILE_VERSION
            if profile["status"] == "ok":
                assert profile["last_bar"] == frame["last_bar"]
                assert profile["last_bar"][:10] == tech[period + "_bar_date"]
                assert profile["bar_count"] == frame["bar_count"]
                if period == "weekly":
                    reference = profile["weekly_ma55"]
                    assert reference["version"] == WEEKLY_MA55_VERSION
                    assert reference["last_bar"] == frame["last_bar"]
                    if reference["status"] == "ok":
                        history = reference["history"]
                        assert len(history) == reference["window"] == 6
                        assert [p["date"] for p in history] == sorted(set(p["date"] for p in history))
                        assert history[-1]["date"] == tech["weekly_bar_date"]
                        assert reference["ma55_qfq"] == history[-1]["ma55_qfq"] == profile["averages_qfq"]["55"]
                        assert all(p["above"] is (p["close_qfq"] > p["ma55_qfq"]) for p in history)
                        assert reference["above_count"] == sum(p["above"] for p in history)
                        consecutive = 0
                        for p in reversed(history):
                            if not p["above"]:
                                break
                            consecutive += 1
                        assert reference["consecutive_above"] == consecutive
                        assert reference["latest_week_above"] is history[-1]["above"]
                        for event in (reference["retest"], reference["reclaim"]):
                            if event:
                                assert event["event_at"] <= event["confirmed_at"] <= tech["weekly_bar_date"]
        for frame in tech["timeframes"].values():
            assert all(s.get("quality_passed") is True for s in frame["signals"] if s["indicator"] == "MACD_DIF")
        if tech["monthly_history_source"] == "validated_native_qfqmonth":
            assert tech["monthly_history_validation"]["forming_period_excluded"]
            assert tech["monthly_history_validation"]["latest_complete_period"] == tech["monthly_bar_date"]
        if tech.get("weekly_history_source") == "validated_native_qfqweek":
            assert tech["weekly_history_validation"]["forming_period_excluded"]
            assert tech["weekly_history_validation"]["latest_complete_period"] == tech["weekly_bar_date"]
    from datetime import date
    for row in pool:
        if report["config"].get("weekly_ma55_entry_gate"):
            from .strategy import evaluate
            assert evaluate(row,row["technical"],report["config"],date.fromisoformat(day))["eligible"]
        actual = balanced_score(row, row["technical"], row["route"], row["leading"], row["minute_frames"])
        for key in ("score", "fundamental_score", "technical_score", "score_breakdown", "fundamental_quality", "score_inputs_complete", "confirmation_state"):
            assert row[key] == actual[key], (row["code"], key)
        assert 0 <= row["fundamental_score"] <= 50 and 0 <= row["technical_score"] <= 50
        assert set(row["score_breakdown"]["technical"])=={"weekly","monthly"}
        assert row["ranking_eligible"] is ranking_eligible(row)
        assert row["execution"].get("entry_watch") is not True or row["t_trend_confirmed"]
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
