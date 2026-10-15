"""Daily TOP10 appearances from published closing decisions, never model reviews."""
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .scoring import finite, ranked_top20, ranking_eligible

ZONE = ZoneInfo("Asia/Shanghai")
TRACKING_THRESHOLD = 3
PREFERENCES_PATH = "operations/watchlist-preferences.json"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_preferences(value):
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1 or
            not isinstance(value.get("ignored_codes"), list) or len(value["ignored_codes"]) > 6000 or
            any(not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) for code in value["ignored_codes"]) or
            len(set(value["ignored_codes"])) != len(value["ignored_codes"])):
        raise ValueError("忽略名单须为schema=1及不重复的六位字符串ignored_codes，股票代码须保留前导0")
    return {"schema": 1, "ignored_codes": sorted(value["ignored_codes"])}


def load_preferences(path=PREFERENCES_PATH):
    file = Path(path)
    return validate_preferences(json.loads(file.read_text(encoding="utf-8"))) if file.exists() else {"schema": 1, "ignored_codes": []}


def apply_preferences(view, preferences):
    """Filter only the watch view; retain original rows, rankings and appearance ledger."""
    preferences = validate_preferences(preferences)
    original = view.get("all_rows", view.get("rows", []))
    if (not isinstance(original, list) or any(not isinstance(row, dict) or not re.fullmatch(r"[0-9]{6}", row.get("code", "")) for row in original) or
            len({r["code"] for r in original}) != len(original)):
        raise ValueError("观察表原始行格式错误，不能猜测被忽略的历史股票")
    ignored = set(preferences["ignored_codes"])
    visible = [row for row in original if row["code"] not in ignored]
    return {**view, "all_rows": original, "rows": visible, "preferences": preferences,
            "preferences_sha256": digest(preferences), "preferences_status": "ok",
            "suppressed_count": len(original) - len(visible)}


def closing_record(summary, rows=None):
    """One finished trading session, including honestly published partial scans."""
    try:
        if (summary.get("scan_phase") != "closing" or summary.get("scan_type") != "after_close" or
                summary.get("model_review") or summary.get("status") not in ("complete", "partial") or
                summary.get("scan_complete") is not (summary["status"] == "complete") or
                summary.get("selection_rule") != "dual_route_monthly_recovery" or
                not summary.get("scoring_version")):
            return None
        day = summary["session"]
        started, finished = (datetime.fromisoformat(summary[k]) for k in ("generated_at", "finished_at"))
        if started.tzinfo is None or finished.tzinfo is None:
            return None
        cutoff = datetime.fromisoformat(summary["minute15_cutoff"])
        cutoff = cutoff.replace(tzinfo=ZONE) if cutoff.tzinfo is None else cutoff.astimezone(ZONE)
        if (started.astimezone(ZONE).date().isoformat() != day or
                finished.astimezone(ZONE).date().isoformat() != day or
                cutoff.date().isoformat() != day or cutoff.hour < 15 or not cutoff <= started <= finished):
            return None
        rows = summary.get("top_candidates") if rows is None else rows
        if not isinstance(rows, list) or len(rows) > 20:
            return None
        codes = [row.get("code") for row in rows]
        if len(set(codes)) != len(codes) or any(not re.fullmatch(r"\d{6}", code or "") for code in codes):
            return None
        if any(finite(row.get("score")) is None for row in rows):
            return None
        record = {key: summary.get(key) for key in ("session", "generated_at", "finished_at", "scan_phase",
            "scan_type", "status", "scan_complete", "selection_rule", "scoring_version", "workflow_run_url",
            "ranking_complete", "minute15_cutoff")}
        record["top10"] = [{"rank": i, "code": row["code"], "name": row.get("name"),
                            "score": finite(row["score"])} for i, row in enumerate(rows[:10], 1)]
        record["decision_sha256"] = digest(record)
        return record
    except (ValueError, TypeError, KeyError):
        return None


def validate_history(history):
    if history.get("schema") != 1 or history.get("threshold") != TRACKING_THRESHOLD:
        raise ValueError("前10累计记录版本不匹配")
    sessions = history.get("sessions")
    if not isinstance(sessions, list) or len({s.get("session") for s in sessions}) != len(sessions):
        raise ValueError("前10累计记录重复交易日或格式错误")
    for record in sessions:
        if closing_record(record, record.get("top10")) != record:
            raise ValueError("前10累计记录日期、名单或哈希不符")
    return history


def load_history(directory, as_of):
    """Backfill actual archives; a date is counted only once across reruns/versions."""
    root = Path(directory)
    records, issues = {}, []
    ledger = root / "top10-history.json"
    if ledger.exists():
        try:
            existing = validate_history(json.loads(ledger.read_text(encoding="utf-8")))
            records = {r["session"]: r for r in existing["sessions"] if r["session"] <= as_of}
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            issues.append("累计文件不可用，按正式收盘归档重建：" + str(exc))
    for path in sorted(root.glob("*/closing-summary.json")):
        if path.parent.name > as_of:
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            record = closing_record(summary)
            if record is None or record["session"] != path.parent.name:
                issues.append(str(path) + "未通过收盘日期/状态/名单校验，未计次数")
                continue
            previous = records.get(record["session"])
            if previous is None or datetime.fromisoformat(record["finished_at"]) >= datetime.fromisoformat(previous["finished_at"]):
                records[record["session"]] = record
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            issues.append(str(path) + "不可读取，未计次数：" + str(exc))
    return {"schema": 1, "threshold": TRACKING_THRESHOLD,
            "sessions": [records[day] for day in sorted(records)]}, issues


def history_with_report(history, report):
    result = {**history, "sessions": list(history["sessions"])}
    record = closing_record(report, ranked_top20(report.get("rankings", [])))
    if record is not None:
        # Replace the session's final list, never add a second appearance for a rerun.
        result["sessions"] = [r for r in result["sessions"] if r["session"] != record["session"]] + [record]
        result["sessions"].sort(key=lambda r: r["session"])
    return result


def tracking_view(report, history, preferences=None):
    visits = defaultdict(list)
    for session in history["sessions"]:
        for row in session["top10"]:
            visits[row["code"]].append({"session": session["session"], "rank": row["rank"],
                "name": row.get("name"), "scoring_version": session["scoring_version"],
                "decision_sha256": session["decision_sha256"]})
    current = {row["code"]: row for row in report.get("rankings", []) + report.get("observations", [])}
    ranks = {row["code"]: i for i, row in enumerate(sorted(
        (r for r in report.get("rankings", []) if ranking_eligible(r)),
        key=lambda r: (-float(r["score"]), r["code"])), 1)}
    day, rows = report.get("session"), []
    for code, appearances in visits.items():
        if len(appearances) < TRACKING_THRESHOLD:
            continue
        row = current.get(code, {})
        observation = row.get("technical", {}).get("daily_volume", {})
        ratio = finite(observation.get("ratio20"))
        mean, volume = finite(observation.get("previous20_mean_volume")), finite(observation.get("volume"))
        valid = (observation.get("status") == "ok" and observation.get("confirmed_at") == day and
                 observation.get("baseline_bars") == 20 and observation.get("baseline_end") and
                 str(observation["baseline_end"]) < day and ratio is not None and ratio >= 0 and
                 mean is not None and mean > 0 and volume is not None and volume >= 0 and
                 abs(ratio - volume / mean) < 1e-10)
        rows.append({"code": code, "name": row.get("name") or appearances[-1].get("name"),
            "appearance_count": len(appearances), "appearances": appearances,
            "first_session": appearances[0]["session"], "last_session": appearances[-1]["session"],
            "current_rank": ranks.get(code), "currently_qualified": code in ranks,
            "daily_volume": observation if valid else {"status": "unavailable", "ratio20": None,
                "confirmed_at": None, "reason": "本次完整日线或此前20日均量未核验；不沿用旧日量"}})
    rows.sort(key=lambda r: (-r["appearance_count"], -int(r["last_session"].replace("-", "")), r["code"]))
    view = {"schema": 1, "threshold": TRACKING_THRESHOLD, "as_of_session": day,
        "counted_sessions": len(history["sessions"]),
        "first_counted_session": history["sessions"][0]["session"] if history["sessions"] else None,
        "basis": "正式交易日收盘综合前10；跨版本保留实际原榜，同日重跑只计一次，模型复核/预跑/失败不计",
        "daily_volume_basis": "最新完整交易日量/此前20个完整交易日均量；排除当日；仅跟踪，不加排名分",
        "rows": rows}
    return apply_preferences(view, preferences or {"schema": 1, "ignored_codes": []})


def attach_tracking(report, results_directory="results", preferences_path=PREFERENCES_PATH):
    if not report.get("session"):
        return
    history, issues = load_history(results_directory, report["session"])
    history = history_with_report(history, report)
    report["top10_history"] = history
    try:
        preferences, preference_error = load_preferences(preferences_path), None
    except (OSError, ValueError, TypeError) as exc:
        preferences, preference_error = {"schema": 1, "ignored_codes": []}, str(exc)
    report["top10_tracking"] = {**tracking_view(report, history, preferences),
        "history_status": "partial" if issues else "ok", "history_issues": issues}
    if preference_error:
        report["top10_tracking"].update(preferences_status="invalid", preferences_error=preference_error, rows=[])


def audit_tracking(report):
    history = validate_history(report["top10_history"])
    current = closing_record(report, ranked_top20(report.get("rankings", [])))
    if current is not None:
        assert current in history["sessions"]
    assert all(r["session"] <= report["session"] for r in history["sessions"])
    expected = tracking_view(report, history, report["top10_tracking"].get("preferences"))
    if report["top10_tracking"].get("preferences_status") == "invalid":
        expected.update(preferences_status="invalid", rows=[])
    assert all(report["top10_tracking"][key] == value for key, value in expected.items())
    return history
