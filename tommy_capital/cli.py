import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import logging
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .data import (Provider, DataError, SHANGHAI, completed_session, report_periods,
                   normalize_spot, normalize_finance, latest_finance, daily_history)
from .strategy import prepare_universe, base_checks, evaluate
from .technical import bars, strategic_technical, tactical, closed_minute_cutoff, minute_observation

LOG = logging.getLogger(__name__)
DEFAULTS = {"min_turnover_cny": 30000000, "min_revenue_yoy_pct": 0, "min_profit_yoy_pct": 0,
            "max_pe_ttm": 60, "max_pb": 8, "max_industry_pe_ratio": 1.2,
            "min_industry_pe_samples": 5, "max_finance_age_days": 225,
            "daily_history_years": 8, "request_timeout_seconds": 180, "request_attempts": 2}


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if value is pd.NaT or value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def validate_config(config):
    if set(config) != set(DEFAULTS):
        raise ValueError("配置存在未知字段")
    for key, value in config.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"配置 {key} 需为有限数值")
        if key not in ["min_revenue_yoy_pct", "min_profit_yoy_pct"] and value <= 0:
            raise ValueError(f"配置 {key} 需大于0")
    for key in ["daily_history_years", "request_attempts", "min_industry_pe_samples", "max_finance_age_days"]:
        if not isinstance(config[key], int):
            raise ValueError(f"配置 {key} 需为整数")


def load_holdings(path):
    if path is None:
        return set()
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or not isinstance(raw.get("holdings"), list):
        raise ValueError('持仓格式应为 {"holdings": ["六位代码"]}')
    values = raw["holdings"]
    if any(not isinstance(c, str) or len(c) != 6 or not c.isdigit() for c in values):
        raise ValueError("持仓代码必须是六位数字字符串")
    return set(values)


def write_outputs(out, report):
    out.mkdir(parents=True, exist_ok=True)
    report = clean(report)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    for key, filename in [("rankings", "rankings.csv"), ("observations", "observations.csv"), ("excluded", "excluded.csv"), ("divergences", "divergences.csv")]:
        rows = report.get(key, [])
        frame = pd.DataFrame(rows)
        if frame.empty:
            frame = pd.DataFrame(columns=["code", "name", "score", "reasons"])
        for col in frame.columns:
            if frame[col].map(lambda v: isinstance(v, (dict, list))).any():
                frame[col] = frame[col].map(lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        # Prevent spreadsheet formula interpretation of upstream text values.
        for col in frame.select_dtypes("object").columns:
            frame[col] = frame[col].map(lambda v: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v)
        frame.to_csv(out / filename, index=False, encoding="utf-8-sig")
    title = f"Tommy Capital · {report['status']}"
    intro = f"扫描启动：{report['generated_at']}；模式：{report.get('scan_type', '未确定')}；完整日线截至：{report.get('session', '未确定')}；15分钟截至：{report.get('minute15_cutoff', '未请求')}。"
    intro += " 规则筛选候选不代表买入建议；排名分数不代表收益概率。"
    coverage_text = json.dumps(report.get('coverage', {}), ensure_ascii=False)
    lines = [f"# {title}", "", intro, "", f"完整扫描：{report.get('scan_complete', False)}；候选数量：{len(report.get('rankings', []))}", "", "覆盖：" + coverage_text, ""]
    for err in report.get("errors", []):
        lines.append(f"- 数据问题：{err}")
    cards = []
    for index, row in enumerate(report.get("rankings", []), 1):
        card_title = f"{index}. {row['code']} {row['name']} · {row['score']}分"
        details = [f"行业：{row.get('industry')}；PE TTM：{row.get('pe_ttm'):.2f}；PB：{row.get('pb'):.2f}",
                   f"报告期：{row.get('period')}；公告日期：{row.get('announced_at')}",
                   f"累计营收同比：{row.get('revenue_yoy')}%；累计净利润同比：{row.get('profit_yoy')}%",
                   f"分数分解：{json.dumps(row['score_breakdown'], ensure_ascii=False)}",
                   "日线背离：" + signal_label(row['technical']['daily_divergences']),
                   "15分钟背离：" + signal_label(row.get('minute15', {})),
                   f"日/周/月趋势：{row['technical']['daily_trend']}/{row['technical']['weekly_trend']}/{row['technical']['monthly_trend']}；月线J：{row['technical']['monthly_j']:.2f}",
                   f"战术观察：{row['tactical']['status']}；仍需支撑阻力、量价与价格结构确认",
                   "人工核对：" + "；".join(row['manual_review'])]
        lines += [f"## {card_title}", ""] + details + [""]
        cards.append("<section><h2>" + html.escape(card_title) + "</h2>" +
                     "".join("<p>" + html.escape(s) + "</p>" for s in details) + "</section>")
    if not cards:
        cards.append("<p>本次没有符合全部规则的候选。请查阅 report.json 中的覆盖情况和排除原因。</p>")
    lines += ["数据来源：新浪行情/分时、腾讯日线、东方财富财报、AKShare交易日历；哈希及缓存时点见report.json。", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    error_html = "".join("<p class='error'>" + html.escape(e) + "</p>" for e in report.get("errors", []))
    (out / "report.html").write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Tommy Capital</title><style>body{font:16px system-ui;background:#10202e;color:#e9f1f7;max-width:1100px;margin:40px auto;padding:20px}"
        "section{background:#193245;padding:20px;margin:20px 0;border-radius:12px}p{line-height:1.7;overflow-wrap:anywhere}.error{color:#ffba86}</style>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(intro)}</p><p>完整扫描：{report.get('scan_complete', False)}；覆盖：{html.escape(coverage_text)}</p>"
        + error_html + "".join(cards) + divergence_table(report.get('divergences', [])) + "</html>", encoding="utf-8")


def signal_label(observation):
    if observation.get('status') == 'unavailable':
        return '数据不可用：' + observation.get('error', '')
    if observation.get('status') == 'not_requested':
        return '日线预检模式，未请求分时'
    return '、'.join(('底背离' if s['direction'] == 'bullish' else '顶背离') + '/' + s['indicator']
                    for s in observation.get('signals', [])) or '无已确认背离'


def divergence_table(rows):
    columns = ['code', 'name', 'timeframe', 'direction', 'indicator', 'confirmed_at', 'strategic_eligible']
    headers = ['代码', '名称', '周期', '方向', '指标', '确认时间', '战略资格']
    body = ''
    for row in rows:
        body += '<tr>' + ''.join('<td>' + html.escape(str(row[k])) + '</td>' for k in columns) + '</tr>'
    return '<h2>全部初筛合格股票的日线/15分钟背离</h2><div style="overflow:auto"><table><tr>' + ''.join('<th>' + h + '</th>' for h in headers) + '</tr>' + body + '</table></div>'


def run(args, provider=None, now=None):
    now = now or datetime.now(SHANGHAI)
    today = now.date()
    config = dict(DEFAULTS)
    if args.config:
        config.update(json.loads(Path(args.config).read_text(encoding="utf-8-sig")))
    validate_config(config)
    holdings = load_holdings(args.holdings)
    workers = getattr(args, "workers", 4)
    daily_only = getattr(args, "daily_only", False)
    provider = provider or Provider(timeout=config["request_timeout_seconds"], retries=config["request_attempts"], refresh=args.refresh)
    report = {"system": "Tommy Capital 1.1", "mode": "live", "generated_at": now.isoformat(),
              "status": "failed", "scan_complete": False, "config": config, "errors": [], "warnings": [],
              "rankings": [], "observations": [], "excluded": [], "divergences": [], "lineage": [],
              "limitations": ["规则引擎，无订单执行；基本面改善为累计同比加速代理",
                              "扣非、负债、订单、公告事件与股东信息仍需人工复核",
                              "公开接口可能限流/滞后；抓取时间不是交易所逐股报价时间",
                              "15分钟与日线背离只作观察；做T提示仅用于确认持仓且通过战略规则的股票"]}
    try:
        calendar = provider.fetch("tool_trade_date_hist_sina", ttl=86400)
        session = completed_session(calendar, now)
        report["session"] = session.isoformat()
        trading_today = today in set(pd.to_datetime(calendar.trade_date).dt.date)
        intraday = trading_today and (9, 30) <= (now.hour, now.minute) < (15, 10)
        report["scan_type"] = "daily_baseline" if daily_only else "intraday" if intraday else "closed_session"
        cutoff = None if daily_only else closed_minute_cutoff(calendar, now, 15)
        if cutoff is not None:
            report["minute15_cutoff"] = cutoff.isoformat()
            report["minute15_current_session"] = cutoff.date() == today
        if intraday and cutoff is not None and cutoff.date() != today:
            raise DataError("今日首根15分钟K线尚未完成，请于09:45:30之后扫描")
        spot_raw = provider.fetch("spot_sina_full", ttl=60)
        spot = normalize_spot(spot_raw)
        histories, financial_period_counts = [], {}
        def load_period(period):
            raw = provider.fetch("finance_em_named", ttl=86400, allow_empty=True, date=period.strftime("%Y%m%d"))
            return normalize_finance(raw, period, today) if not raw.empty else raw
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(load_period, p): p for p in report_periods(today)}
            for future in as_completed(futures):
                period = futures[future]
                try:
                    frame = future.result()
                    financial_period_counts[period.isoformat()] = len(frame)
                    if not frame.empty:
                        histories.append(frame)
                except DataError as exc:
                    report["errors"].append(f"财报 {period}: {exc}")
        if not histories:
            raise DataError("没有可用已公告财报，停止排名")
        finance = latest_finance(pd.concat(histories, ignore_index=True))
        universe = prepare_universe(spot, finance, config)
        eligible_rows = []
        for row in universe.to_dict("records"):
            row["liquidity_deferred"] = True
            reasons = base_checks(row, config, today)
            if reasons:
                report["excluded"].append({"code": row["code"], "name": row["name"], "stage": "fundamental_valuation", "reasons": reasons})
                if row["code"] in holdings:
                    report["observations"].append({**row, "eligible": False, "reasons": reasons,
                        "tactical": tactical(provider, row["code"], False, True, session)})
            else:
                eligible_rows.append(row)
        eligible_rows.sort(key=lambda r: (-float(r["profit_yoy"]), r["code"]))
        selected = eligible_rows[:] if args.limit == 0 else eligible_rows[:args.limit]
        selected_codes = {r["code"] for r in selected}
        selected += [r for r in eligible_rows if r["code"] in holdings and r["code"] not in selected_codes]
        report["coverage"] = {"source_universe": len(spot_raw), "universe": len(universe),
                              "financial_period_counts": financial_period_counts, "financially_eligible": len(eligible_rows),
                              "technical_requested": len(selected), "technical_completed": 0,
                              "minute15_requested": 0, "minute15_completed": 0, "tactical_failed_frames": 0,
                              "unscanned": len(eligible_rows) - len(selected),
                              "unknown_holdings": sorted(holdings - set(universe.code))}
        def inspect_stock(row):
            raw, warning = daily_history(provider, row["code"], f"{today.year - config['daily_history_years']}0101",
                                         session.strftime("%Y%m%d"), today.strftime("%Y%m%d"))
            daily = bars(raw)
            daily = daily[daily.index.date <= session]
            technical = strategic_technical(daily, session)
            decision = evaluate(row, technical, config, today)
            minute = {"status": "not_requested", "signals": []} if daily_only else minute_observation(provider, row["code"], cutoff, daily)
            card = {**row, **decision, "technical": technical, "minute15": minute,
                    "tactical": tactical(provider, row["code"], decision["eligible"], row["code"] in holdings, session,
                                         now=now, calendar=calendar, daily_qfq=daily, minute15=minute) if not daily_only else
                                {"status": "not_requested", "signals": []}}
            return card, warning
        report["status"] = "running"
        write_outputs(Path(args.output), report)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(inspect_stock, r): r for r in selected}
            for count, future in enumerate(as_completed(futures), 1):
                row = futures[future]
                try:
                    card, warning = future.result()
                    if warning:
                        report["warnings"].append(warning)
                    report["coverage"]["technical_completed"] += 1
                    if not daily_only:
                        report["coverage"]["minute15_requested"] += 1
                        if card["minute15"]["status"] == "ok":
                            report["coverage"]["minute15_completed"] += 1
                        else:
                            report["errors"].append(f"{row['code']} 15分钟：{card['minute15'].get('error', card['minute15']['status'])}")
                    for period, frame in card["tactical"].get("frames", {}).items():
                        if frame["status"] == "unavailable":
                            report["coverage"]["tactical_failed_frames"] += 1
                            report["errors"].append(f"{row['code']} 持仓{period}分钟：{frame.get('error')}")
                    for timeframe, observation in [("daily", card["technical"]["daily_divergences"]), ("15m", card["minute15"])]:
                        for signal in observation["signals"]:
                            report["divergences"].append({"code": row["code"], "name": row["name"], "timeframe": timeframe,
                                "strategic_eligible": card["eligible"], **signal})
                    report["rankings" if card["eligible"] else "observations"].append(card)
                except DataError as exc:
                    report["errors"].append(f"{row['code']}: {exc}")
                    report["excluded"].append({"code": row["code"], "name": row["name"], "stage": "technical_data", "reasons": [str(exc)]})
                if count % 25 == 0 or count == len(selected):
                    report["rankings"].sort(key=lambda r: (-r["score"], r["code"]))
                    LOG.info("技术进度 %s/%s；战略候选 %s", count, len(selected), len(report["rankings"]))
                    write_outputs(Path(args.output), report)
        report["rankings"].sort(key=lambda r: (-r["score"], r["code"]))
        report["scan_complete"] = report["coverage"]["unscanned"] == 0 and not report["errors"]
        report["status"] = "complete" if report["scan_complete"] else "partial"
    except DataError as exc:
        report["errors"].append(str(exc))
    finally:
        report["finished_at"] = datetime.now(SHANGHAI).isoformat()
        report["lineage"] = provider.lineage
        write_outputs(Path(args.output), report)
    LOG.info("状态 %s；候选 %s；报告 %s/report.html", report["status"], len(report["rankings"]), args.output)
    summary = {k: report.get(k) for k in ["status", "scan_complete", "scan_type", "generated_at", "session", "minute15_cutoff", "coverage"]}
    summary["top_candidates"] = [{k: r.get(k) for k in ["code", "name", "score", "technical", "minute15"]} for r in report["rankings"][:15]]
    summary["divergences"] = report["divergences"]
    summary["errors"] = report["errors"]
    (Path(args.output) / "summary.json").write_text(json.dumps(clean(summary), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("TOMMY_RESULT_JSON=" + json.dumps(clean(summary), ensure_ascii=False, allow_nan=False))
    return 2 if report["status"] == "failed" else 1 if report["errors"] else 0


def main():
    parser = argparse.ArgumentParser(description="Tommy Capital 真实沪深北A股扫描；开盘后使用完整15分钟K线")
    parser.add_argument("--config", help="JSON阈值配置")
    parser.add_argument("--holdings", help="确认持仓JSON")
    parser.add_argument("--limit", type=int, default=0, help="技术扫描上限，默认0=全部初筛合格股票")
    parser.add_argument("--workers", type=int, default=4, help="股票请求并发数，1到8，默认4")
    parser.add_argument("--daily-only", action="store_true", help="仅预检完整日线/财报，不请求分时")
    parser.add_argument("--output", default="reports/latest")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，重新抓取")
    args = parser.parse_args()
    if args.limit < 0 or not 1 <= args.workers <= 8:
        parser.error("--limit 必须非负；--workers 为1到8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        return run(args)
    except (ValueError, OSError) as exc:
        LOG.error("配置/文件错误：%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
