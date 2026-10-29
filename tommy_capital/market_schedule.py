"""Gate recurring Actions scans with the real A-share trading calendar."""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .data import DataError, Provider, SHANGHAI, completed_session
from .scoring import SCORING_VERSION

OPENING_CRON = "50 1 8 10 *"
CLOSING_CRON = "5 7 * * 1-5"
CLOSING_FALLBACK_CRONS = ("35 7 * * 1-5", "5 8 * * 1-5")


def published_closing_report(results_directory, now):
    """A published partial result is final; failed/running/stale files are not."""
    day = now.astimezone(SHANGHAI).date().isoformat()
    directory = Path(results_directory) / day
    try:
        summary = json.loads((directory / "closing-summary.json").read_text(encoding="utf-8"))
        markdown = (directory / "closing-report.md").read_text(encoding="utf-8")
        latest = Path(results_directory)
        if (summary != json.loads((latest / "latest-summary.json").read_text(encoding="utf-8")) or
                markdown != (latest / "latest-report.md").read_text(encoding="utf-8")):
            return False
        if not isinstance(summary, dict):
            return False
        status = summary.get("status")
        count = summary.get("candidate_count")
        coverage = summary.get("coverage")
        if (status not in {"complete", "partial"} or
                summary.get("scan_complete") is not (status == "complete") or
                summary.get("scan_phase") != "closing" or
                summary.get("scan_type") != "after_close" or
                summary.get("session") != day or
                summary.get("selection_rule") != "dual_route_monthly_recovery" or
                summary.get("scoring_version") != SCORING_VERSION or
                summary.get("display_limit") != 20 or
                not isinstance(count, int) or isinstance(count, bool) or count < 0 or
                not isinstance(summary.get("top_candidates"), list) or
                not isinstance(summary.get("ranking_eligible_count"), int) or
                not 0 <= summary["ranking_eligible_count"] <= count or
                len(summary["top_candidates"]) != min(20, summary["ranking_eligible_count"]) or
                not isinstance(coverage, dict) or coverage.get("universe", 0) <= 0):
            return False
        started = datetime.fromisoformat(summary["generated_at"])
        finished = datetime.fromisoformat(summary["finished_at"])
        cutoff = datetime.fromisoformat(summary["minute15_cutoff"])
        if started.tzinfo is None or finished.tzinfo is None:
            return False
        started, finished = started.astimezone(SHANGHAI), finished.astimezone(SHANGHAI)
        # Minute-bar timestamps are exchange-local in the existing report schema.
        cutoff = (cutoff.replace(tzinfo=SHANGHAI) if cutoff.tzinfo is None else cutoff.astimezone(SHANGHAI))
        if (started.date().isoformat() != day or finished.date().isoformat() != day or
                cutoff.date().isoformat() != day or cutoff.hour < 15 or
                not (cutoff <= started <= finished <= now)):
            return False
        return (f"扫描启动：{started:%Y-%m-%d %H:%M:%S}" in markdown and
                f"扫描完成：{finished:%Y-%m-%d %H:%M:%S}" in markdown)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def calendar_contains_today(calendar, now):
    # Retain the existing stale-year check and also reject a calendar that
    # ends before today within the current year.
    completed_session(calendar, now)
    dates = pd.to_datetime(calendar.trade_date, errors="coerce").dropna().dt.date
    if max(dates) < now.date():
        raise DataError("交易日历没有覆盖今天，不能把未知日期当作休市")
    return now.date() in set(dates)


def validate_close_request(request, now):
    """A bounded same-day recovery request cannot become a holiday/pre-open scan."""
    if not isinstance(request, dict) or type(request.get("schema")) is not int or request["schema"] != 1:
        raise DataError("补启动请求格式无效")
    if request.get("scan_phase") != "closing" or request.get("requested_for") != now.date().isoformat():
        raise DataError("补启动仅允许当天收盘扫描，旧日期请求不执行")
    if type(request.get("attempt")) is not int or not 1 <= request["attempt"] <= 2:
        raise DataError("独立补启动每日最多两次")
    purpose = request.get("purpose")
    if purpose not in (None, "financial_recovery"):
        raise DataError("无法识别补启动用途")
    if purpose == "financial_recovery":
        digest = request.get("expected_report_sha256")
        run_url = request.get("expected_report_run_url")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                not isinstance(run_url, str) or not re.fullmatch(
                    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+", run_url)):
            raise DataError("财报修复请求必须绑定原报告SHA256和实际运行链接")
    try:
        requested = datetime.fromisoformat(request["requested_at"])
    except (KeyError, ValueError, TypeError) as exc:
        raise DataError("补启动请求时间无效") from exc
    if requested.tzinfo is None:
        raise DataError("补启动请求时间必须有时区")
    requested = requested.astimezone(SHANGHAI)
    if (requested.date() != now.date() or not timedelta(0) <= now-requested <= timedelta(hours=2)
            or (requested.hour, requested.minute) < (15, 5) or not 15 <= now.hour < 20):
        raise DataError("补启动请求已过期、含未来时间或尚未收盘")


def financial_recovery_matches(results_directory, request):
    """One explicit repair may supersede an exact, financially broken partial.

    A new publication invalidates the old request. Ordinary partial reports,
    including genuine history shortfalls, still suppress duplicate scans.
    """
    if results_directory is None:
        return False
    try:
        raw = (Path(results_directory) / "latest-summary.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != request["expected_report_sha256"]:
            return False
        summary = json.loads(raw)
        return (summary.get("workflow_run_url") == request["expected_report_run_url"] and
                summary.get("status") == "partial" and summary.get("scan_complete") is False and
                any(isinstance(error, str) and error.startswith(("财报 ", "财务质量 "))
                    for error in summary.get("errors", [])))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def decision(event, schedule, phase, now, provider, results_directory=None, request=None):
    if event == "push":
        validate_close_request(request, now)
        phase = "closing"
    elif event != "schedule":
        return True, phase, "手动启动；行情完整性由扫描器继续核验"
    elif schedule == OPENING_CRON:
        if now.date() != date(2026, 10, 8):
            return False, "opening", "单次开盘日期已过，跳过"
        phase = "opening"
    elif schedule in (CLOSING_CRON, *CLOSING_FALLBACK_CRONS):
        phase = "closing"
    else:
        raise DataError("无法识别定时表达式，停止而不猜测扫描阶段")
    calendar = provider.fetch("tool_trade_date_hist_sina", ttl=86400)
    try:
        trading = calendar_contains_today(calendar, now)
    except DataError:
        # Refresh the real calendar once, without guessing weekdays/holidays.
        calendar = provider.fetch("tool_trade_date_hist_sina", ttl=0)
        trading = calendar_contains_today(calendar, now)
    if trading and phase == "closing":
        published = results_directory is not None and published_closing_report(results_directory, now)
        if event == "push" and request.get("purpose") == "financial_recovery":
            if published and financial_recovery_matches(results_directory, request):
                return True, phase, "已核对原报告财报故障及SHA256，执行明确请求的有界修复重扫"
            return False, phase, "财报修复对应的报告已变化、不完整或无财报故障，跳过旧请求"
        if published:
            return False, phase, "当天有效收盘报告已发布（含如实标记的部分结果），跳过重复扫描"
    return trading, phase, "今日为A股交易日" if trading else "今日A股休市，保留上一交易日报告"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"))
    parser.add_argument("--schedule", default=os.environ.get("SCHEDULE_CRON", ""))
    parser.add_argument("--phase", choices=["auto", "opening", "closing"], default="auto")
    parser.add_argument("--results-directory", default="results")
    parser.add_argument("--request-path", default="operations/scan-request.json")
    args = parser.parse_args()
    now = datetime.now(SHANGHAI)
    code = 0
    try:
        request = None
        if args.event == "push":
            raw = Path(args.request_path).read_text()
            if len(raw) > 4000:
                raise DataError("补启动请求过大")
            request = json.loads(raw)
        run, phase, reason = decision(args.event, args.schedule, args.phase, now, Provider(), args.results_directory, request)
    except (DataError, OSError, ValueError) as exc:
        run, phase, reason, code = False, args.phase, f"无法核验交易日，本次未扫描：{exc}", 2
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"run={'true' if run else 'false'}\nphase={phase}\n")
    if not run and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(f"### A股定时扫描\n\n{now.date()}：{reason}。本次未发布新的行情结论。\n")
    print(f"{now.isoformat()} · {phase} · {reason}")
    return code


if __name__ == "__main__":
    sys.exit(main())
