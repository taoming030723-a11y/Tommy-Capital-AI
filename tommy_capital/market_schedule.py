"""Gate recurring Actions scans with the real A-share trading calendar."""
import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .data import DataError, Provider, SHANGHAI, completed_session

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
                summary.get("scoring_version") != "balanced_50_50_v1" or
                summary.get("display_limit") != 20 or
                not isinstance(count, int) or isinstance(count, bool) or count < 0 or
                not isinstance(summary.get("top_candidates"), list) or
                len(summary["top_candidates"]) != min(20, count) or
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


def decision(event, schedule, phase, now, provider, results_directory=None):
    if event != "schedule":
        return True, phase, "手动启动；行情完整性由扫描器继续核验"
    if schedule == OPENING_CRON:
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
    if trading and phase == "closing" and results_directory is not None and published_closing_report(results_directory, now):
        return False, phase, "当天有效收盘报告已发布（含如实标记的部分结果），跳过重复扫描"
    return trading, phase, "今日为A股交易日" if trading else "今日A股休市，保留上一交易日报告"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"))
    parser.add_argument("--schedule", default=os.environ.get("SCHEDULE_CRON", ""))
    parser.add_argument("--phase", choices=["auto", "opening", "closing"], default="auto")
    parser.add_argument("--results-directory", default="results")
    args = parser.parse_args()
    now = datetime.now(SHANGHAI)
    code = 0
    try:
        run, phase, reason = decision(args.event, args.schedule, args.phase, now, Provider(), args.results_directory)
    except DataError as exc:
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
