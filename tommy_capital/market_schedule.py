"""Gate recurring Actions scans with the real A-share trading calendar."""
import argparse
import os
import sys
from datetime import date, datetime

import pandas as pd

from .data import DataError, Provider, SHANGHAI, completed_session

OPENING_CRON = "50 1 8 10 *"
CLOSING_CRON = "5 7 * * 1-5"


def calendar_contains_today(calendar, now):
    # Retain the existing stale-year check and also reject a calendar that
    # ends before today within the current year.
    completed_session(calendar, now)
    dates = pd.to_datetime(calendar.trade_date, errors="coerce").dropna().dt.date
    if max(dates) < now.date():
        raise DataError("交易日历没有覆盖今天，不能把未知日期当作休市")
    return now.date() in set(dates)


def decision(event, schedule, phase, now, provider):
    if event != "schedule":
        return True, phase, "手动启动；行情完整性由扫描器继续核验"
    if schedule == OPENING_CRON:
        if now.date() != date(2026, 10, 8):
            return False, "opening", "单次开盘日期已过，跳过"
        phase = "opening"
    elif schedule == CLOSING_CRON:
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
    return trading, phase, "今日为A股交易日" if trading else "今日A股休市，保留上一交易日报告"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"))
    parser.add_argument("--schedule", default=os.environ.get("SCHEDULE_CRON", ""))
    parser.add_argument("--phase", choices=["auto", "opening", "closing"], default="auto")
    args = parser.parse_args()
    now = datetime.now(SHANGHAI)
    code = 0
    try:
        run, phase, reason = decision(args.event, args.schedule, args.phase, now, Provider())
    except DataError as exc:
        run, phase, reason, code = False, args.phase, f"无法核验交易日，本次未扫描：{exc}", 2
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"run={'true' if run else 'false'}\nphase={phase}\n")
    if not run and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(f"### A股定时扫描\n\n{now.date()}：{reason}。没有发布新的行情结论。\n")
    print(f"{now.isoformat()} · {phase} · {reason}")
    return code


if __name__ == "__main__":
    sys.exit(main())
