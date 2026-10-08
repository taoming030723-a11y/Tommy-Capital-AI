import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .data import (Provider, DataError, SHANGHAI, completed_session, report_periods,
                   normalize_spot, normalize_finance, latest_finance, daily_history)
from .strategy import prepare_universe, financial_routes, evaluate, numeric, SELECTION_RULE
from .technical import (bars, strategic_technical, tactical, closed_minute_cutoff,
                        minute_observation, execution_observation)
from .inflection import load_evidence
from .reporting import render_markdown, github_run_url

LOG = logging.getLogger(__name__)
DEFAULTS = {"min_turnover_cny": 30000000, "min_revenue_yoy_pct": 0, "min_profit_yoy_pct": 0,
            "max_pe_ttm": 60, "max_pb": 8, "max_industry_pe_ratio": 1.2,
            "min_industry_pe_samples": 5, "max_finance_age_days": 225,
            "daily_history_years": 8, "request_timeout_seconds": 180, "request_attempts": 2,
            "daily_volume_abnormal_ratio": 2.0, "weekly_base_max_range_pct": 20.0,
            "weekly_base_low_tolerance_pct": 3.0, "weekly_base_max_volume_ratio": 1.1,
            "daily_breakout_rvol": 1.5, "daily_breakout_close_location": 0.65,
            "daily_breakout_lookback": 20, "daily_breakout_platform_range_pct": 20.0,
            "leading_min_revenue_growth_pct": 10.0, "leading_min_business_growth_pct": 30.0,
            "leading_price_compression_pct": 20.0, "max_forward_pe": 30.0,
            "max_forward_ev_sales": 4.0, "min_forward_margin_pct": 20.0,
            "max_bear_downside_pct": 30.0, "execution_support_distance_pct": 3.0,
            "execution_volume_ratio": 1.2}


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
        if key not in ["min_revenue_yoy_pct", "min_profit_yoy_pct", "weekly_base_low_tolerance_pct"] and value <= 0:
            raise ValueError(f"配置 {key} 需大于0")
    if config["daily_volume_abnormal_ratio"] <= 1 or not 0 <= config["weekly_base_low_tolerance_pct"] < 100:
        raise ValueError("异常放量倍数需大于1；周线低点容差需在0到100之间")
    if config["daily_breakout_rvol"] <= 1 or not 0 < config["daily_breakout_close_location"] <= 1:
        raise ValueError("平台突破量比需大于1，收盘位置需在(0,1]之间")
    for key in ["min_forward_margin_pct", "max_bear_downside_pct", "leading_price_compression_pct"]:
        if not 0 < config[key] < 100:
            raise ValueError(f"配置 {key} 需在0到100之间")
    if config["execution_volume_ratio"] < 1:
        raise ValueError("执行量价确认量比不能小于1")
    for key in ["daily_history_years", "request_attempts", "min_industry_pe_samples", "max_finance_age_days", "daily_breakout_lookback"]:
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
    status_label = {'complete':'完成', 'partial':'部分完成', 'failed':'失败', 'running':'扫描中'}.get(report['status'], report['status'])
    mode_label = {'intraday':'盘中', 'after_close':'收盘后', 'closed_session':'最近完整交易日', 'daily_baseline':'日线预检'}.get(report.get('scan_type'), '待确定')
    title = f"Tommy Capital · {status_label}"
    intro = f"扫描启动：{report['generated_at']}；模式：{mode_label}；完整日线截至：{report.get('session', '未确定')}；15分钟截至：{report.get('minute15_cutoff', '未请求')}。"
    intro += " 规则筛选候选不代表买入建议；排名分数不代表收益概率。"
    coverage = report.get('coverage', {})
    coverage_text = (f"行情 {coverage.get('universe', 0)}只；基本面/估值通过 {coverage.get('financially_eligible', 0)}只；"
                     f"日线检查 {coverage.get('technical_completed', 0)}/{coverage.get('technical_requested', 0)}只；"
                     f"15分钟检查 {coverage.get('minute15_completed', 0)}/{coverage.get('minute15_requested', 0)}只；"
                     f"未扫初筛合格股 {coverage.get('unscanned', 0)}只")
    cards = []
    display_rows = report.get("rankings", [])
    for index, row in enumerate(display_rows, 1):
        category = f"{row.get('route', '—')}路线观察候选；" + ('战略量价确认' if row['strategic_eligible'] else '等待战略确认')
        card_title = f"{index}. {row['code']} {row['name']} · {row['score']}分 · {category}"
        details = [f"行业：{row.get('industry')}；PE TTM：{numeric(row.get('pe_ttm'))}；PB：{numeric(row.get('pb'))}",
                   f"报告期：{row.get('period')}；公告日期：{row.get('announced_at')}",
                   f"累计营收同比：{row.get('revenue_yoy')}%；累计净利润同比：{row.get('profit_yoy')}%",
                   "日线背离：" + signal_label(row['technical']['daily_divergences']),
                   "15分钟背离：" + signal_label(row.get('minute15', {})),
                   '周线筑底蓄力代理：' + ('满足' if row['technical']['weekly_base']['detected'] else '暂未满足') + f"；6周振幅：{row['technical']['weekly_base']['range_pct']:.2f}%",
                   f"日线量比：{row['technical']['volume_ratio20']:.2f}倍（此前20个交易日均量，不含检测当日）；异常放量门槛：{row['technical']['daily_volume']['threshold']:.2f}倍；确认日：{row['technical']['daily_bar_date']}",
                   '日/周/月趋势：' + '/'.join('确认' if row['technical'][k] else '未确认' for k in ['daily_trend','weekly_trend','monthly_trend']) + f"；月线J：{row['technical']['monthly_j']:.2f}",
                   '持仓战术：' + {'not_held':'未确认持仓，仅作行情观察', 'strategy_not_passed':'未通过战略条件', 'watch_only':'可查看持仓战术观察', 'not_requested':'本次未请求'}.get(row['tactical']['status'],row['tactical']['status']) + '；仍需支撑阻力、量价与价格结构确认',
                   "人工核对：" + "；".join(row['manual_review'])]
        cards.append("<section><h2>" + html.escape(card_title) + "</h2>" +
                     "".join("<p>" + html.escape(s) + "</p>" for s in details) + "</section>")
    if not cards:
        cards.append("<p>本次未列出通过双路线基本面/估值/流动性及月J路径的观察候选。请结合覆盖情况和证据缺失判断。</p>")
    (out / "report.md").write_text(render_markdown(report, github_run_url()), encoding="utf-8")
    error_html = "".join("<p class='error'>" + html.escape(e) + "</p>" for e in report.get("errors", []))
    (out / "report.html").write_text(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Tommy Capital</title><style>body{font:16px system-ui;background:#10202e;color:#e9f1f7;max-width:1100px;margin:40px auto;padding:20px}"
        "section{background:#193245;padding:20px;margin:20px 0;border-radius:12px}p{line-height:1.7;overflow-wrap:anywhere}.error{color:#ffba86}td,th{padding:8px;text-align:left;border-bottom:1px solid #486070}table{border-collapse:collapse}</style>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(intro)}</p><p>完整扫描：{report.get('scan_complete', False)}；覆盖：{html.escape(coverage_text)}</p>"
        + error_html + "".join(cards) + divergence_table(report.get('divergences', [])) + "</html>", encoding="utf-8")


def signal_label(observation):
    if observation.get('status') == 'unavailable':
        return '数据不可用：' + observation.get('error', '')
    if observation.get('status') == 'not_requested':
        return '未进入月J观察池，未请求分时' if observation.get('reason') == 'outside_monthly_pool' else '日线预检模式，未请求分时'
    if observation.get('status') == 'insufficient_bars':
        return 'K线不足，无法判断有无背离'
    label = '、'.join(('底背离' if s['direction'] == 'bullish' else '顶背离') + '/' + s['indicator'] +
                     '（确认：' + s['confirmed_at'] + '）' for s in observation.get('signals', [])) or '无已确认背离'
    return label + ('；完整K线：' + observation['last_bar'] if observation.get('last_bar') else '')


def divergence_table(rows):
    columns = ['code', 'name', 'timeframe', 'direction', 'indicator', 'confirmed_at', 'strategic_eligible']
    headers = ['代码', '名称', '周期', '方向', '指标', '确认时间', '战略资格']
    body = ''
    for row in rows:
        body += '<tr>' + ''.join('<td>' + html.escape(('底背离' if row[k] == 'bullish' else '顶背离')
                              if k == 'direction' else str(row[k])) + '</td>' for k in columns) + '</tr>'
    return '<h2>全部初筛合格股票的日线/15分钟背离</h2><div style="overflow:auto"><table><tr>' + ''.join('<th>' + h + '</th>' for h in headers) + '</tr>' + body + '</table></div>'


def quote_clock_check(spot, now, intraday, acquired=None):
    """Check provider update times, which may continue after the market closes.

    The source supplies a clock without a date. A valid clock alone does not
    confirm today's close; inspect_stock still requires today's qfq daily bar
    and its close to match the received spot price.
    """
    acquired = acquired or datetime.now(SHANGHAI)
    acquired = acquired if acquired.date() == now.date() and acquired >= now else now
    lower = pd.Timedelta(hours=9, minutes=30) if intraday else pd.Timedelta(hours=14, minutes=55)
    upper = pd.Timedelta(hours=acquired.hour, minutes=acquired.minute, seconds=acquired.second) + pd.Timedelta(minutes=1)
    clocks = pd.to_timedelta(spot.quote_clock_time, errors="coerce")
    valid = (clocks >= lower) & (clocks <= upper)
    diagnostics = {"source_clock_semantics": "provider_update_time_without_date",
                   "acquired_at": acquired.isoformat(), "window_start": str(lower), "window_end": str(upper),
                   "rejected": int((~valid).sum()),
                   "clock_counts": spot.quote_clock_time.fillna("missing").value_counts().head(20).to_dict()}
    return valid, diagnostics


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
    minute_scope = getattr(args, "minute_scope", "monthly")
    minute_scope = "monthly" if minute_scope == "strategic" else minute_scope
    provider = provider or Provider(timeout=config["request_timeout_seconds"], retries=config["request_attempts"], refresh=args.refresh)
    report = {"system": "Tommy Capital 2.0", "selection_rule": SELECTION_RULE, "mode": "live", "generated_at": now.isoformat(),
              "status": "failed", "scan_complete": False, "config": config, "errors": [], "warnings": [],
              "rankings": [], "observations": [], "excluded": [], "divergences": [], "lineage": [],
              "workflow_run_url": github_run_url(), "source_commit": os.environ.get("GITHUB_SHA"),
              "limitations": ["规则引擎，无订单执行；基本面改善为累计同比加速代理",
                              "领先订单/销量/催化及估值覆盖依赖可核对的官方证据，不存在全市场自动补齐的可靠免费接口",
                              "远期盈利/分部/EV-Sales三情景为研究假设，证据缺失不通过；扣非、负债、股本仍需复核",
                              "公开接口可能限流/滞后；抓取时间不是交易所逐股报价时间",
                              "15分钟与日线背离只作观察；做T提示仅用于确认持仓且通过战略规则的股票"]}
    try:
        calendar = provider.fetch("tool_trade_date_hist_sina", ttl=86400)
        session = completed_session(calendar, now)
        report["session"] = session.isoformat()
        trading_today = today in set(pd.to_datetime(calendar.trade_date).dt.date)
        intraday = trading_today and (9, 30) <= (now.hour, now.minute) < (15, 10)
        after_close = trading_today and (now.hour, now.minute) >= (15, 10)
        report["scan_type"] = "daily_baseline" if daily_only else "intraday" if intraday else "after_close" if after_close else "closed_session"
        phase = getattr(args, "scan_phase", "auto")
        if phase == "auto":
            phase = "pre_run" if daily_only else "closing" if after_close else "opening" if intraday and (now.hour, now.minute) < (10, 30) else "intraday" if intraday else "pre_run"
        report["scan_phase"] = phase
        cutoff = None if daily_only else closed_minute_cutoff(calendar, now, 15)
        if cutoff is not None:
            report["minute15_cutoff"] = cutoff.isoformat()
            report["minute15_current_session"] = cutoff.date() == today
        if intraday and cutoff is not None and cutoff.date() != today:
            raise DataError("今日首根15分钟K线尚未完成，请于09:45:30之后扫描")
        if phase == "opening" and (not intraday or cutoff is None or cutoff.date() != today or cutoff.time() < datetime.strptime("09:45", "%H:%M").time()):
            raise DataError("开盘任务未处于当天盘中或首根09:45完整15分钟K线未确认")
        if phase == "closing" and (not after_close or session != today or cutoff is None or cutoff.date() != today or cutoff.hour < 15):
            raise DataError("收盘任务缺少当天完整日线或15:00分时，不能将旧行情发布为收盘结果")
        spot_raw = provider.fetch("spot_sina_full", ttl=60)
        spot = normalize_spot(spot_raw)
        # Preserve actual quote coverage even if a later global guard stops us.
        report["coverage"] = {"source_universe": len(spot_raw), "universe": len(spot)}
        if intraday or after_close:
            spot["quote_clock_ok"], report["quote_clock_validation"] = quote_clock_check(spot, now, intraday)
            rejected = report["quote_clock_validation"]["rejected"]
            report["quote_clock_rejected"] = rejected
        if intraday:
            if rejected > len(spot) / 10:
                raise DataError(f"盘中行情时刻异常 {rejected}/{len(spot)}；数据可能未更新，停止排名")
            if rejected:
                report["errors"].append(f"{rejected}只股票报价时刻未确认当前盘中时段，已排除；源未提供完整报价日期")
        elif after_close:
            if rejected > len(spot) / 10:
                raise DataError(f"收盘报价时刻异常 {rejected}/{len(spot)}，不能发布为当日收盘扫描")
            if rejected:
                report["errors"].append(f"{rejected}只报价时刻未确认收盘时段，已排除；已检查股票另与当天完整日线收盘价核对")
        histories, financial_period_counts = [], {}
        def load_period(period):
            ttl = 900 if period == report_periods(today)[0] else 86400
            raw = provider.fetch("finance_em_named", ttl=ttl, allow_empty=True, date=period.strftime("%Y%m%d"))
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
        evidence, evidence_stats = load_evidence(getattr(args, "leading_evidence", None), today)
        report["leading_evidence_coverage"] = evidence_stats
        report["errors"] += evidence_stats["source_failures"]
        route_a_count = route_b_pre_count = route_b_count = missing_evidence = 0
        eligible_rows = []
        for row in universe.to_dict("records"):
            row["liquidity_deferred"] = True
            row["leading_evidence"] = evidence.get(row["code"], {})
            routes = financial_routes(row, config, today)
            row["financial_routes"] = routes
            route_a_count += int(routes["route_a_passed"])
            route_b_pre_count += int(routes["route_b_prequalified"])
            route_b_count += int(routes["route_b_passed"])
            missing_evidence += int(routes["route_b_prequalified"] and not routes["route_b_passed"])
            if not routes["route_a_passed"] and not routes["route_b_prequalified"]:
                reasons = ["A：" + r for r in routes["route_a_reasons"]] + ["B：" + r for r in routes["route_b_reasons"]]
                report["excluded"].append({"code": row["code"], "name": row["name"], "stage": "fundamental_valuation", "reasons": reasons})
                if row["code"] in holdings:
                    report["observations"].append({**row, "eligible": False, "reasons": reasons,
                        "tactical": tactical(provider, row["code"], False, True, session)})
            else:
                eligible_rows.append(row)
        eligible_rows.sort(key=lambda r: (not r["financial_routes"]["route_a_passed"], -(numeric(r.get("profit_yoy")) or 0), r["code"]))
        selected = eligible_rows[:] if args.limit == 0 else eligible_rows[:args.limit]
        selected_codes = {r["code"] for r in selected}
        selected += [r for r in eligible_rows if r["code"] in holdings and r["code"] not in selected_codes]
        report["coverage"] = {"source_universe": len(spot_raw), "universe": len(universe),
                              "financial_period_counts": financial_period_counts,
                              "financially_evaluated": len(universe), "financial_data_available": int(universe.period.notna().sum()),
                              "financially_eligible": sum(r["financial_routes"]["route_a_passed"] or r["financial_routes"]["route_b_passed"] for r in eligible_rows),
                              "route_a_fundamental_passed": route_a_count, "route_b_prequalified": route_b_pre_count,
                              "route_b_fundamental_passed": route_b_count, "leading_evidence_missing": missing_evidence,
                              "technical_prefilter_count": len(eligible_rows),
                              "technical_requested": len(selected), "technical_completed": 0,
                              "monthly_pool_count": 0, "weekly_base_count": 0, "daily_abnormal_volume_count": 0,
                              "strategic_confirmed_count": 0, "monthly_pool_minute15_completed": 0,
                              "route_a_pool_count": 0, "route_b_pool_count": 0, "monthly_current_low_count": 0,
                              "monthly_recovery_count": 0, "weekly_structure_count": 0,
                              "daily_first_breakout_count": 0, "legacy_ma_confirmed_count": 0,
                              "t_trend_confirmed_count": 0, "execution_divergence_count": 0,
                              "execution_entry_watch_count": 0, "confirmed_holding_t_entry_count": 0,
                              "minute15_scope": minute_scope, "minute15_requested": 0, "minute15_completed": 0, "minute15_not_required": 0, "tactical_failed_frames": 0,
                              "unscanned": len(eligible_rows) - len(selected),
                              "unknown_holdings": sorted(holdings - set(universe.code))}
        def inspect_stock(row):
            raw, warning = daily_history(provider, row["code"], f"{today.year - config['daily_history_years']}0101",
                                         session.strftime("%Y%m%d"), today.strftime("%Y%m%d"),
                                         required_session=today if after_close else None,
                                         expected_close=row["price"] if after_close else None)
            daily = bars(raw)
            daily = daily[daily.index.date <= session]
            if after_close and (daily.empty or daily.index[-1].date() != today or
                                abs(float(daily.close.iloc[-1]) - float(row["price"])) > 0.011):
                raise DataError("收盘现价与当天最新完整前复权日线收盘价不一致，行情可能滞后/复权锚点未确认")
            period_cutoff = today if (now.hour, now.minute) >= (15, 10) else today - timedelta(days=1)
            technical = strategic_technical(daily, session, period_cutoff, config)
            decision = evaluate(row, technical, config, today)
            need_minute = not daily_only and (minute_scope == "screened" or decision["eligible"])
            minute = minute_observation(provider, row["code"], cutoff, daily) if need_minute else {
                "status": "not_requested", "signals": [], "reason": "daily_baseline" if daily_only else "outside_monthly_pool"}
            execution = execution_observation(provider, row["code"], decision["t_trend_confirmed"], now, calendar, daily, minute, config) if not daily_only else {"status": "not_requested", "entry_watch": False}
            card = {**row, **decision, "technical": technical, "minute15": minute, "execution": execution,
                    "confirmed_holding": row["code"] in holdings,
                    "tactical": tactical(provider, row["code"], decision["t_trend_confirmed"], row["code"] in holdings, session,
                                         now=now, calendar=calendar, daily_qfq=daily, minute15=minute, execution=execution) if not daily_only else
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
                    if card["eligible"]:
                        report["coverage"]["monthly_pool_count"] += 1
                        report["coverage"]["weekly_base_count"] += int(card["technical"]["weekly_base"]["detected"])
                        report["coverage"]["daily_abnormal_volume_count"] += int(card["technical"]["daily_volume"]["abnormal"])
                        report["coverage"]["strategic_confirmed_count"] += int(card["strategic_eligible"])
                        for key, value in [("route_a_pool_count", card["route"] == "A"), ("route_b_pool_count", card["route"] == "B"),
                            ("monthly_current_low_count", card["monthly_low_j_watch"]), ("monthly_recovery_count", card["monthly_recovery_watch"]),
                            ("weekly_structure_count", card["technical"]["weekly_structure"]["confirmed"]),
                            ("daily_first_breakout_count", card["technical"]["first_volume_breakout"]["active"]),
                            ("legacy_ma_confirmed_count", card["legacy_ma_confirmed"]), ("t_trend_confirmed_count", card["t_trend_confirmed"]),
                            ("execution_divergence_count", card["t_trend_confirmed"] and bool(card["minute15"]["signals"])),
                            ("execution_entry_watch_count", card["execution"].get("entry_watch", False)),
                            ("confirmed_holding_t_entry_count", card["tactical"].get("entry_eligible", False))]:
                            report["coverage"][key] += int(value)
                    if card["minute15"]["status"] != "not_requested":
                        report["coverage"]["minute15_requested"] += 1
                        if card["minute15"]["status"] == "ok":
                            report["coverage"]["minute15_completed"] += 1
                            report["coverage"]["monthly_pool_minute15_completed"] += int(card["eligible"])
                        else:
                            report["errors"].append(f"{row['code']} 15分钟：{card['minute15'].get('error', card['minute15']['status'])}")
                    else:
                        report["coverage"]["minute15_not_required"] += 1
                    for period, frame in card["execution"].get("frames", {}).items():
                        if frame["status"] == "unavailable":
                            report["coverage"]["tactical_failed_frames"] += 1
                            report["errors"].append(f"{row['code']} 执行观察{period}分钟：{frame.get('error')}")
                    for timeframe, observation in [("daily", card["technical"]["daily_divergences"]), ("15m", card["minute15"])]:
                        for signal in observation["signals"]:
                            report["divergences"].append({"code": row["code"], "name": row["name"], "timeframe": timeframe,
                                "monthly_pool_eligible": card["eligible"], "strategy_pool_eligible": card["eligible"],
                                "route": card["route"], "strategic_eligible": card["strategic_eligible"], **signal})
                    report["rankings" if card["eligible"] else "observations"].append(card)
                except DataError as exc:
                    report["errors"].append(f"{row['code']}: {exc}")
                    report["excluded"].append({"code": row["code"], "name": row["name"], "stage": "technical_data", "reasons": [str(exc)]})
                if count % 25 == 0 or count == len(selected):
                    report["rankings"].sort(key=lambda r: (-r["score"], r["code"]))
                    LOG.info("技术进度 %s/%s；双路线观察候选 %s", count, len(selected), len(report["rankings"]))
                    write_outputs(Path(args.output), report)
        report["rankings"].sort(key=lambda r: (-r["score"], r["code"]))
        report["scan_complete"] = report["coverage"]["unscanned"] == 0 and not report["errors"] and missing_evidence == 0
        report["status"] = "complete" if report["scan_complete"] else "partial"
    except DataError as exc:
        report["errors"].append(str(exc))
    finally:
        report["finished_at"] = datetime.now(SHANGHAI).isoformat()
        report["lineage"] = provider.lineage + report.get("leading_evidence_coverage", {}).get("lineage", [])
        write_outputs(Path(args.output), report)
    LOG.info("状态 %s；候选 %s；报告 %s/report.html", report["status"], len(report["rankings"]), args.output)
    summary = {k: report.get(k) for k in ["system", "selection_rule", "status", "scan_complete", "scan_type", "scan_phase", "generated_at", "finished_at", "session", "minute15_cutoff", "coverage", "config", "leading_evidence_coverage", "workflow_run_url", "source_commit", "quote_clock_validation"]}
    summary["top_candidates"] = [{k: r.get(k) for k in ["code", "name", "route", "score", "strategic_eligible", "t_trend_confirmed", "technical", "minute15", "execution", "tactical", "confirmed_holding"]} for r in report["rankings"][:15]]
    summary["execution_candidates"] = [{k: r.get(k) for k in ["code", "name", "route", "minute15", "execution", "tactical", "confirmed_holding"]}
        for r in report["rankings"] if r.get("t_trend_confirmed") and r.get("minute15", {}).get("signals")]
    summary["candidate_count"] = len(report["rankings"])
    summary["observation_count"] = len(report["observations"])
    summary["divergences"] = report["divergences"]
    summary["errors"] = report["errors"]
    (Path(args.output) / "summary.json").write_text(json.dumps(clean(summary), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("TOMMY_RESULT_JSON=" + json.dumps(clean(summary), ensure_ascii=False, allow_nan=False))
    return 2 if report["status"] == "failed" else 1 if report["status"] == "partial" else 0


def main():
    parser = argparse.ArgumentParser(description="Tommy Capital 真实沪深北A股扫描；开盘后使用完整15分钟K线")
    parser.add_argument("--config", help="JSON阈值配置")
    parser.add_argument("--holdings", help="确认持仓JSON")
    parser.add_argument("--leading-evidence", default="evidence/leading-inflections.json", help="官方领先指标与已复核三情景估值证据文件")
    parser.add_argument("--scan-phase", choices=["auto", "opening", "intraday", "closing", "pre_run"], default="auto")
    parser.add_argument("--limit", type=int, default=0, help="技术扫描上限，默认0=全部初筛合格股票")
    parser.add_argument("--workers", type=int, default=4, help="股票请求并发数，1到8，默认4")
    parser.add_argument("--daily-only", action="store_true", help="仅预检完整日线/财报，不请求分时")
    parser.add_argument("--minute-scope", choices=["monthly", "strategic", "screened"], default="monthly", help="15分钟默认检查全部双路线月J路径观察池；screened扩展至全部初筛请求技术检查的股票")
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
