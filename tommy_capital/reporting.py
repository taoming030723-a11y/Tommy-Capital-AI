"""Chinese reports rendered directly from real scan results."""
import argparse
import html
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .technical import minute_execution_status
from .scoring import ranked_top20, TECHNICAL_WEIGHTS, SCORING_VERSION, ranking_eligible, confirmation_state


def cell(value):
    text = html.escape(str(value if value is not None else "未取得"))
    return re.sub(r"[\r\n]+", " ", text).replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")


def number(value, digits=2):
    try:
        value = float(value)
        return f"{value:.{digits}f}" if math.isfinite(value) else "未取得"
    except (TypeError, ValueError):
        return "未取得"


def date_label(value, daily=False):
    if not value:
        return "未取得"
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
        return parsed.strftime("%Y-%m-%d" if daily else "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def trend(value):
    return "✅" if value is True else "❌" if value is False else "未检查"


def table(headers, rows):
    return ["| " + " | ".join(map(cell, headers)) + " |",
            "| " + " | ".join(["---"] * len(headers)) + " |"] + [
        "| " + " | ".join(map(cell, row)) + " |" for row in rows] + [""]


def expandable(headers, rows, shown=10, label="查看其余记录"):
    if not rows:
        return []
    lines = table(headers, rows[:shown])
    if len(rows) > shown:
        lines += ["<details>", f"<summary>{cell(label)}（{len(rows) - shown}条）</summary>", ""]
        lines += table(headers, rows[shown:]) + ["</details>", ""]
    return lines


def github_run_url():
    repository, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if repository and run_id:
        return f"https://github.com/{repository}/actions/runs/{run_id}"
    return None


def source_failure(message):
    if "qfqday" in message or "hfqday" in message:
        return "未返回所需复权价格"
    if "复权因子" in message:
        return "复权因子不可用或校验未通过"
    if any(s in message.lower() for s in ["timeout", "timed out", "超时"]):
        return "请求超时"
    if any(s in message for s in ["ConnectionError", "RemoteDisconnected", "连接", "connection closed"]):
        return "连接中断"
    if "字段" in message or "格式" in message:
        return "源字段或数据格式不可用"
    final = message.strip().splitlines()[-1] if message.strip() else "数据不可用"
    return re.sub(r"^(?:\w+Error|\w+Exception):\s*", "", final)[:160]


def failure_reason(message):
    if "周/月线不足" in message:
        return "历史不足：完整周线少于60周或完整月线少于24个月。"
    if "日线不足250根" in message:
        return "日线少于250根或未覆盖最新完整交易日，可能为历史不足、停牌或数据滞后。"
    if "日线均不可用" in message:
        parts = re.split(r"；(腾讯|东财|新浪)：", message)
        reasons = [parts[i] + "：" + source_failure(parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
        return "日线来源均不可用；" + "；".join(reasons)
    if "分时最新完整K线不是" in message:
        return source_failure(message) + "；分时可能停牌或滞后。"
    return source_failure(message)


def minute_label(observation):
    status = observation.get("status")
    if status == "unavailable":
        return "数据缺失"
    if status == "insufficient_bars":
        return "K线不足，未能判断"
    if status == "not_requested":
        return "未请求"
    if status != "ok":
        return "未检查"
    directions = {s.get("direction") for s in observation.get("signals", [])}
    return "、".join(label for direction, label in [("bullish", "底背离"), ("bearish", "顶背离")]
                    if direction in directions) or "无已确认背离"


def ranking_rows(rows, monthly_pool=False):
    output = []
    for row in rows:
        technical = row.get("technical", {})
        if monthly_pool:
            output.append([row.get("name"), row.get("code"), number(technical.get("monthly_j")),
                           trend(technical.get("weekly_base", {}).get("detected")),
                           number(technical.get("volume_ratio20")) + "倍",
                           trend(technical.get("daily_volume", {}).get("abnormal")),
                           minute_label(row.get("minute15", {})), number(row.get("score"))])
            continue
        output.append([row.get("name"), row.get("code"), number(row.get("score")),
                       trend(technical.get("daily_trend")), trend(technical.get("weekly_trend")),
                       trend(technical.get("monthly_trend")), number(technical.get("monthly_j"))])
    return output


def signal_section(report, title, timeframe, direction, checked):
    signals = [s for s in report.get("divergences", []) if s.get("timeframe") == timeframe and s.get("direction") == direction]
    signals.sort(key=lambda s: (s.get("confirmed_at", ""), s.get("code", ""), s.get("indicator", "")), reverse=True)
    lines = [f"### {title}（{len(signals)}条 / {len({s['code'] for s in signals})}只股票）", ""]
    if not signals:
        return lines + [("已成功检查的股票中，未检测到已确认的对应背离。" if checked else
                         "本次没有完成对应周期检查，无法判断有无背离。"), ""]
    daily = timeframe == "daily"
    rows = [[s.get("name"), s.get("code"), {"MACD_DIF": "MACD（DIF）", "KDJ_J": "KDJ（J）"}.get(s.get("indicator"), s.get("indicator")),
             date_label(s.get("current_pivot"), daily),
             date_label(s.get("confirmed_at"), daily) + ("（收盘后）" if daily else ""),
             "通过" if s.get("strategic_eligible") else "未通过，仅观察"] for s in signals]
    return lines + expandable(["股票", "代码", "指标", "当前枢轴", "确认时间", "战略规则"], rows, 20, "展开其余已确认背离")


def render_markdown(report, run_url=None):
    if report.get("selection_rule") == "dual_route_monthly_recovery":
        return render_dual_report(report, run_url)
    coverage = report.get("coverage", {})
    status = {"complete": "完成", "partial": "部分完成", "failed": "失败", "running": "扫描中"}.get(report.get("status"), "未确定")
    mode = {"intraday": "盘中扫描", "closed_session": "最近完整交易日扫描", "daily_baseline": "日线预检"}.get(report.get("scan_type"), "尚未确定")
    candidates = report.get("rankings", [])
    observations = report.get("observations", [])
    monthly_pool = report.get("selection_rule") == "monthly_j_lt_20"
    lines = ["# Tommy Capital A股扫描报告", "", f"**状态：{status} · {mode}**", ""]
    if str(report.get("generated_at", "")).startswith("2026-10-07"):
        lines += ["> 这是10月7日预跑，行情截至下列实际日期，不是10月8日开盘结果。", ""]
    elif report.get("scan_type") != "intraday":
        lines += ["> 本次使用最近完整交易日数据，不能作为当天开盘扫描结果。", ""]
    if not report.get("scan_complete", False):
        lines += ["> **完整性：未完成全部检查（scan_complete=false）。** 请同时查看下方数据缺失与未扫描数量。", ""]
    lines += [f"扫描启动：{date_label(report.get('generated_at'))}  ",
              f"扫描完成：{date_label(report.get('finished_at'))}  ",
              f"完整日线截至：{date_label(report.get('session'), True)}  ",
              f"15分钟完整K线截至：{date_label(report.get('minute15_cutoff'))}  ",
              "时间均为北京时间／新加坡时间。日线确认时间表示该交易日收盘后；分时确认时间为完成确认的K线时间标签。", ""]
    def count(key):
        return coverage.get(key, "未取得")
    lines += table(["检查项目", "实际结果"], [
        ["市场行情覆盖", f"{count('universe')}只（源证券列表{count('source_universe')}只）"],
        ["基本面／估值初筛通过", f"{count('financially_eligible')}只"],
        ["日线／周线／月线检查成功", f"{count('technical_completed')} / {count('technical_requested')}只请求"],
        ["15分钟检查成功", f"{count('minute15_completed')} / {count('minute15_requested')}只请求"],
        ["未请求15分钟检查", f"{count('minute15_not_required')}只（按本次范围规则）"],
        ["初筛合格但未请求深度技术检查", f"{count('unscanned')}只"],
        ["月J＜20观察候选总数" if monthly_pool else "战略候选总数", f"{len(candidates)}只"],
        ["数据问题", f"{len(report.get('errors', []))}条（详见缺失清单）"]])
    if monthly_pool:
        lines += table(["月J观察池内检查", "实际结果"], [
            ["周线筑底蓄力代理满足", f"{count('weekly_base_count')} / {len(candidates)}只"],
            ["日线异常放量", f"{count('daily_abnormal_volume_count')} / {len(candidates)}只"],
            ["15分钟检查成功", f"{count('monthly_pool_minute15_completed')} / {len(candidates)}只"],
            ["日／周／月均线趋势另外确认", f"{count('strategic_confirmed_count')}只（不等于买点）"]])
    scope = "全部基本面／估值初筛合格股票" if coverage.get("minute15_scope") == "screened" else ("全部月J＜20观察池" if monthly_pool else "战略候选及月线低J观察股")
    lines += [f"15分钟范围：{scope}；覆盖数按实际成功检查计。以下背离按确认时间排序，同一股票可能同时出现MACD与KDJ记录，条数不等于股票数。", "",
              "## ⭐ 月 J＜20 观察候选 TOP 10" if monthly_pool else "## ⭐ 战略候选 TOP 10", "",
              (f"本次{len(candidates)}只均通过基本面／估值／流动性且完整月线J＜20。周线筑底、日线放量和15分钟背离逐项观察，未满足项直接标出；不要求均线已经转多才能进入观察池。" if monthly_pool else
               f"本次共有{len(candidates)}只通过战略规则的候选。") + "前10只按研究评分排列，评分不代表收益概率。", ""]
    if candidates:
        headers = (["股票", "代码", "月线J", "周线筑底", "日线量比20", "异常放量", "15分钟背离", "研究评分"] if monthly_pool else
                   ["股票", "代码", "综合评分", "日线趋势", "周线趋势", "月线趋势", "月线J"])
        lines += expandable(headers, ranking_rows(candidates, monthly_pool), 10, "查看其余观察候选" if monthly_pool else "查看其余战略候选")
    else:
        lines += ["本次未列出候选；结合检查状态和数据缺失判断，不能将检查失败理解为全市场没有候选。", ""]
    signal_report = report
    daily_checked, minute_checked = coverage.get("technical_completed", 0), coverage.get("minute15_completed", 0)
    if monthly_pool:
        config = report.get("config", {})
        threshold = config.get("daily_volume_abnormal_ratio", 2)
        lines += ["## 周线筑底蓄力观察", "",
                  f"使用最近6个完整周的量价代理：区间高低振幅≤{number(config.get('weekly_base_max_range_pct', 20))}%，"
                  f"后3周最低价较前3周最低价下探不超过{number(config.get('weekly_base_low_tolerance_pct', 3))}%，"
                  "最近周收盘≥6周收盘均值，"
                  f"后3周日均成交量／前3周日均成交量≤{number(config.get('weekly_base_max_volume_ratio', 1.1))}。四项同时满足才标✅；交易日数按真实日线计，休市周不会因天数少而被误判缩量。此标记不能证明主力吸筹。", ""]
        bases = [r for r in candidates if r.get("technical", {}).get("weekly_base", {}).get("detected")]
        if bases:
            rows = [[r.get("name"), r.get("code"), number(r["technical"]["weekly_base"]["range_pct"]) + "%",
                     number(r["technical"]["weekly_base"]["low_change_pct"]) + "%",
                     number(r["technical"]["weekly_base"]["daily_average_volume_ratio"]),
                     r["technical"]["weekly_base"]["period_end"], r["technical"]["weekly_base"]["last_trading_day"]] for r in bases]
            lines += expandable(["股票", "代码", "6周振幅", "低点变化", "日均量比", "完整周标签", "实际末交易日"], rows, 10, "查看其余筑底观察股")
        else:
            lines += ["月J观察池中暂未检测到同时满足上述四项的周线筑底代理。", ""]
        lines += ["## 日线异常放量（相对前20个交易日均量）", "",
                  f"量比＝最新完整交易日成交量÷此前20个交易日平均成交量，不含检测当日。当前异常放量门槛为≥{number(threshold)}倍，可在config.json调整。"
                  f"检测日为{date_label(report.get('session'), True)}；早盘不将当天未完成成交量与20天全天均量混比。", ""]
        volumes = sorted([r for r in candidates if r.get("technical", {}).get("daily_volume", {}).get("abnormal")],
                         key=lambda r: -r["technical"]["volume_ratio20"])
        if volumes:
            rows = [[r.get("name"), r.get("code"), number(r["technical"]["volume_ratio20"]) + "倍",
                     r["technical"]["daily_volume"]["baseline_start"] + " 至 " + r["technical"]["daily_volume"]["baseline_end"],
                     r["technical"]["daily_volume"]["confirmed_at"] + "（收盘后）"] for r in volumes]
            lines += expandable(["股票", "代码", "量比20", "此前20日基准区间", "确认日期"], rows, 10, "查看其余异常放量股")
        else:
            lines += ["月J观察池中最新完整日线未达到当前异常放量门槛。", ""]
        signal_report = {**report, "divergences": [s for s in report.get("divergences", []) if s.get("monthly_pool_eligible")]}
        daily_checked, minute_checked = len(candidates), coverage.get("monthly_pool_minute15_completed", 0)
        lines += ["以下日线／15分钟背离仅展示月J＜20观察池。池外已检查的日线背离保留在完整JSON／CSV，不混入主名单。", ""]
    lines += ["## 📈 日线背离", ""]
    lines += signal_section(signal_report, "日线底背离", "daily", "bullish", daily_checked)
    lines += signal_section(signal_report, "日线顶背离", "daily", "bearish", daily_checked)
    lines += ["## ⏱ 15分钟背离", ""]
    lines += signal_section(signal_report, "15分钟底背离", "15m", "bullish", minute_checked)
    lines += signal_section(signal_report, "15分钟顶背离", "15m", "bearish", minute_checked)
    watches = [r for r in candidates + observations if r.get("monthly_low_j_watch")]
    watches.sort(key=lambda r: (-(r.get("score") or 0), r.get("code", "")))
    if monthly_pool:
        watches = [r for r in candidates if r.get("fundamental_improving")]
    lines += ["## 👀 月线 J < 20 ＋ 基本面改善", "",
              (f"观察池内{len(watches)}只同时显示基本面改善代理。月J＜20已经是入池前提；改善用于观察加分，不是月J入口的替代条件。" if monthly_pool else
               f"共{len(watches)}只观察股。仅作观察加分，不能替代估值与日／周／月趋势规则。") + "改善采用已公告累计净利润同比增速较相邻报告期加快的代理，仍需核对扣非与经营证据。", ""]
    if watches:
        rows = [[r.get("name"), r.get("code"), number(r.get("technical", {}).get("monthly_j")),
                 number(r.get("previous_profit_yoy")) + "%", number(r.get("profit_yoy")) + "%",
                 "通过" if r.get("strategic_eligible" if monthly_pool else "eligible") else "未通过，仅观察"] for r in watches]
        lines += expandable(["股票", "代码", "月线J", "上一期利润同比", "最新利润同比", "战略规则"], rows, 10, "查看其余月线低J观察股")
    else:
        lines += ["已完成检查的股票中，本次没有同时满足两项条件的观察股。", ""]
    lines += ["## ⚠️ 数据缺失与未扫描", ""]
    names = {r.get("code"): r.get("name") for r in candidates + observations + report.get("excluded", [])}
    errors = []
    for message in report.get("errors", []):
        match = re.match(r"^(\d{6})(?:\s+([^：:]+))?[:：]\s*(.*)", message, re.DOTALL)
        if match:
            code, stage, reason = match.groups()
            errors.append([names.get(code, "名称未取得"), code, stage or "日线／大周期", failure_reason(reason)])
        else:
            errors.append(["全局／数据源", "—", "扫描", failure_reason(message)])
    if errors:
        lines += table(["股票", "代码", "检查环节", "缺失原因"], errors)
    else:
        lines += ["本次没有记录接口或数据校验错误。", ""]
    if coverage.get("unscanned", 0):
        lines += [f"另有{coverage['unscanned']}只初筛合格股票未请求深度技术检查，不能视为已扫描。", ""]
    if coverage.get("unknown_holdings"):
        lines += ["持仓名单中未在本次行情覆盖里确认的代码：" + "、".join(map(str, coverage["unknown_holdings"])) + "。", ""]
    lines += ["## 使用这些结果", "",
              ("先通过基本面／估值／流动性与月J＜20，再观察周线筑底、日线异常放量和15分钟顶底背离。入池只是观察资格，日／周／月均线趋势的确认另行记录。" if monthly_pool else
               "基本面、估值与日／周／月大周期决定方向；") + "日线和15分钟背离不单独成为买点。做T仅限已确认持仓且通过全部趋势确认条件，并结合60分钟结构、支撑阻力与量价；本系统没有下单授权。", "",
              "观察候选仍需人工核对扣非净利润、行业景气、订单、公告、负债及股本变化。数据缺失不会被补成行情或解释成没有背离。", ""]
    if run_url:
        lines += [f"[查看本次 Actions、完整报告附件与运行日志]({run_url})", "",
                  "附件保留完整JSON、CSV、HTML与中文Markdown；逐股原始错误和数据来源可在附件里复核。", ""]
    return "\n".join(lines)


def week_structure_label(technical):
    labels = {"base": "筑底", "higher_low": "低点抬高", "volatility_contraction": "波动收敛", "platform_breakout": "平台突破"}
    structure = technical.get("weekly_structure", {})
    return "／".join(labels[k] for k, v in structure.get("conditions", {}).items() if v and k in labels) or ("已确认" if structure.get("confirmed") else "待确认")


def volume_ratio(technical, period):
    observation = technical.get("timeframes", {}).get(period, {})
    volume = observation.get("volume_ma20")
    return volume.get("ratio20") if volume is not None else observation.get("structure", {}).get("ratio20")


def candidate_reason(index, row):
    """Explain recorded inputs, confirmation stage and real signals, without promotion."""
    tech = row.get("technical", {})
    quality = row.get("fundamental_quality", {})
    recovery = tech.get("monthly_recovery", {})
    history = recovery.get("history", [])[-(6 if row.get("route") == "B" else 3):]
    path = " → ".join(number(p.get("j")) for p in history) or number(recovery.get("previous_j")) + " → " + number(tech.get("monthly_j"))
    low = tech.get("monthly_j") is not None and float(tech["monthly_j"]) < 20
    entry = "当前完整月J<20" if low else "近6月超卖恢复路径" if row.get("route") == "B" else "近3月超卖恢复路径"
    finance = (f"{row.get('route')}路线，{row.get('profit_status', '盈利状态未取得')}；营收同比{number(row.get('revenue_yoy'))}%，")
    if row.get("route") == "B":
        valuation = row.get("leading", {}).get("valuation", {})
        finance += ("真实连续两期归母/扣非亏损、利润率及同比现金流同步改善，仍属TTM亏损；PS/PB限定研究路线。" if row.get("leading",{}).get("variant")=="loss_recovery" else
                    f"领先指标、核心改善与未来催化证据通过，审阅估值安全边际{number(valuation.get('base_margin_pct'))}%。")
    else:
        finance += (f"TTM扣非/归母利润{number(quality.get('core_profit_share_ttm'))}，"
                    f"经营现金流/归母利润{number(quality.get('cash_conversion_ttm'))}，"
                    f"PE/PB {number(row.get('pe_ttm'))}/{number(row.get('pb'))}。")
    if quality.get("low_base_flag"):
        finance += "低基数增速加分受限。"
    periods = {"monthly": "月", "weekly": "周"}
    breakdown = row.get("score_breakdown", {}).get("technical", {})
    scores = "、".join(label + number(breakdown.get(period, {}).get("score")) for period, label in periods.items())
    signals = []
    frames = {**tech.get("timeframes", {}), **row.get("minute_frames", {})}
    for period, label in periods.items():
        for signal in frames.get(period, {}).get("signals", []):
            if signal.get("indicator") != "MACD_DIF" or signal.get("quality_passed") is not True:
                continue
            direction = "底背离" if signal.get("direction") == "bullish" else "顶背离"
            signals.append(label + "MACD" + direction + "（" + date_label(signal.get("confirmed_at"), period in ("monthly", "weekly", "daily")) + "确认）")
    state = row.get("confirmation_state") or confirmation_state(tech, row.get("route"))
    execution = "；小周期分不计排名" if state.get("execution_score_enabled") is False else ""
    reason = (f"**{index}. {cell(row.get('name'))}（{cell(row.get('code'))}）**：{finance}"
              f"{entry}，月J {path}（截至{date_label(tech.get('monthly_bar_date'), True)}）；"
              f"周线{week_structure_label(tech)}，周/月量比{number(volume_ratio(tech, 'weekly'))}/{number(volume_ratio(tech, 'monthly'))}倍。"
              f"技术分：{scores}。" + ("；".join(signals) + "。" if signals else "未检出已确认MACD背离。") +
              f"当前{state['label']}{execution}；15分钟{minute_execution_status(row.get('minute15', {}))}。")
    structures = []
    for period, label in (("monthly", "月"), ("weekly", "周"), ("daily", "日")):
        profile = frames.get(period, {}).get("macro_profile", {})
        if profile.get("status") == "ok":
            ma = profile.get("averages_qfq", {})
            structures.append(f"{label}线{profile.get('label')}（MA5/13 {number(ma.get('5'))}/{number(ma.get('13'))}，J {number(profile.get('kdj', {}).get('previous_j'))}→{number(profile.get('kdj', {}).get('j'))}）")
    if structures:
        reason += "形态：" + "；".join(structures) + "。"
    ma55 = frames.get("weekly", {}).get("macro_profile", {}).get("weekly_ma55", {})
    if ma55.get("status") == "ok":
        line, close = ma55.get("ma55_qfq"), tech.get("daily_close_qfq")
        distance = (close / line - 1) * 100 if close is not None and line is not None and line > 0 else None
        event = ma55.get("retest") or ma55.get("reclaim")
        confirmation = ("；" + ("回踩收回" if ma55.get("retest") else "重站并守住") + event["confirmed_at"] + "确认") if event else ""
        reason += (f"周MA55 {number(line)}（{ma55.get('direction_label')}），近6个完整周{ma55.get('above_count')}/6收于线上，"
                   f"连续{ma55.get('consecutive_above')}周；最新日收盘距线{number(distance)}%{confirmation}。")
    return reason


def render_tracking_section(tracking, config=None):
    """The same watch table is used by scans and preference-only refreshes."""
    config = config or {}
    repository = os.environ.get("GITHUB_REPOSITORY", "taoming030723-a11y/Tommy-Capital-AI")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("观察名单管理仓库地址无效")
    control = f"https://github.com/{repository}/actions/workflows/watchlist.yml"
    hidden = f"；已忽略{tracking['suppressed_count']}只" if tracking.get("suppressed_count") else ""
    lines = [f"## 综合前10累计至少3次（日线量比仅跟踪{hidden}） [忽略/恢复]({control})", ""]
    watch_rows = []
    for row in tracking.get("rows", []):
        volume = row.get("daily_volume", {})
        valid = volume.get("status") == "ok"
        ratio = volume.get("ratio20")
        threshold = volume.get("threshold", config.get("daily_volume_abnormal_ratio", 2))
        label = ("异常放量≥" + number(threshold) + "倍" if valid and ratio >= threshold else
                 "高于均量" if valid and ratio > 1 else "等于均量" if valid and ratio == 1 else
                 "低于均量" if valid else "数据未齐")
        watch_rows.append([str(row.get("name")) + "（" + str(row.get("code")) + "）", row.get("appearance_count"),
            row.get("last_session"), row.get("current_rank") or "未入本次研究榜",
            date_label(volume.get("confirmed_at"), True) if valid else "未核验",
            number(ratio) + "倍" if valid else "未取得", label])
    empty = ("忽略设置无效，请在管理入口修复" if tracking.get("preferences_status") == "invalid" else
             "观察股票均已忽略" if tracking.get("suppressed_count") else
             "历史记录未齐" if tracking.get("history_status") == "partial" else "暂无累计至少3次的股票")
    lines += table(["股票（代码）", "不同收盘日次数", "最近入选前10", "本次综合名次", "日量日期", "日量/前20日均量", "日量状态"],
        watch_rows or [[empty, *["—"] * 6]])
    return lines


def render_dual_report(report, run_url=None):
    coverage = report.get("coverage", {})
    pool = report.get("rankings", [])
    failed = report.get("status") == "failed"
    displayed = [] if failed else ranked_top20(pool)
    status = {"complete": "完成", "partial": "部分完成", "failed": "失败", "running": "扫描中"}.get(report.get("status"), "未确定")
    phase = {"opening": "开盘", "closing": "收盘", "intraday": "盘中", "pre_run": "预跑", "model_review": "模型复核"}.get(report.get("scan_phase"), "扫描")
    lines = [f"# Tommy Capital · {date_label(report.get('session'), True)} · {phase}", "", "## 扫描概况", ""]
    rows = [
        ["扫描股票", f"{coverage.get('universe', '未取得')}只"],
        ["日周月有效核验 / 请求", f"{coverage.get('technical_completed', '未取得')} / {coverage.get('technical_requested', '未取得')}只"],
        ["符合入口 / 评分齐全", "未完成，候选数量无法判断" if failed else f"{len(pool)} / {sum(ranking_eligible(row) for row in pool)}只"],
        ["综合前20实际展示", "未完成，候选数量无法判断" if failed else f"{len(displayed)}只"],
        ["扫描时间", f"扫描启动：{date_label(report.get('generated_at'))}；扫描完成：{date_label(report.get('finished_at'))}"],
        ["数据状态", status + f"；scan_complete={str(report.get('scan_complete', False)).lower()}；ranking_complete={str(report.get('ranking_complete', False)).lower()}" +
         ("；已核验范围排名" if not report.get("ranking_complete", False) else "")]]
    if report.get("model_review"):
        rows.append(["报告用途", f"复核{report['model_review']['source_generated_at'][:10]}行情，未抓取新行情、不计前10次数"])
    elif report.get("scan_phase") == "pre_run":
        rows.append(["报告用途", "预跑，不计前10次数" + ("；不是10月8日开盘或收盘结果" if str(report.get("generated_at", "")).startswith("2026-10-07") else "")])
    if failed:
        rows.append(["目标日线日期（未完成逐股核验）", date_label(report.get("session"), True)])
        rows.append(["目标15分钟截止（未完成逐股核验）", date_label(report.get("minute15_cutoff"))])
        rows.append(["停止原因", "；".join(failure_reason(e) for e in report.get("errors", [])) or "详见日志"])
    links = []
    run_url = run_url or report.get("workflow_run_url")
    if run_url:
        links.append(f"[本次扫描]({run_url})")
    if os.environ.get("REPORT_ARTIFACT_URL"):
        links.append(f"[完整数据附件]({os.environ['REPORT_ARTIFACT_URL']})")
    if links:
        rows.append(["完整数据", " · ".join(links)])
    lines += table(["扫描", "结果"], rows)
    lines += ["## 综合前20（基本面50分＋技术面50分）", ""]
    top_rows = []
    for index, row in enumerate(displayed, 1):
        tech = row.get("technical", {})
        state = row.get("confirmation_state") or confirmation_state(tech, row.get("route"))
        top_rows.append([index, str(row.get("name")) + "（" + str(row.get("code")) + "）",
            number(row.get("score")), number(row.get("fundamental_score")), number(row.get("technical_score")),
            number(tech.get("monthly_j")), number(volume_ratio(tech, "weekly")) + "倍",
            number(volume_ratio(tech, "monthly")) + "倍", state["label"]])
    lines += table(["名次", "股票（代码）", "综合/100", "基本面/50", "技术/50", "完整月J", "周量/前20周均量", "月量/前20月均量", "确认阶段"],
        top_rows or [["—", "扫描未完成，无法判断" if failed else "暂无评分齐全的合格股票", *["—"] * 7]])
    tracking = report.get("top10_tracking", {})
    lines += render_tracking_section(tracking, report.get("config"))
    if displayed:
        lines += ["## 前20入选理由", ""]
        for index, row in enumerate(displayed, 1):
            lines += [candidate_reason(index, row), ""]
    if report.get("macd_monitor") is not None:
        from .monitor_reporting import render_monitor_section
        lines += render_monitor_section(report["macd_monitor"])
    from .report_controls import add_report_controls
    return add_report_controls("\n".join(lines))


def render_html(report, run_url=None):
    """Use the same compact tables and reasons as the Markdown/Actions report."""
    def inline(text):
        escaped = html.escape(html.unescape(text.replace("\\|", "|").replace("\\`", "`")))
        from .report_controls import REPORT_URL
        escaped = re.sub(r"\[([^\]]+)\]\((https://github\.com/[^\s)]+|"+re.escape(REPORT_URL)+r")\)", r'<a href="\2">\1</a>', escaped)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    lines = render_markdown(report, run_url).splitlines()
    output, index = [], 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("<!-- tommy-report-controls:"):
            index+=1
            continue
        if line.startswith("| "):
            group = []
            while index < len(lines) and lines[index].startswith("| "):
                group.append(lines[index]); index += 1
            output.append("<table>")
            for i, row in enumerate(group):
                if i == 1:
                    continue  # Markdown separator, not a data row.
                cells = re.split(r"(?<!\\)\|", row)[1:-1]
                tag = "th" if i == 0 else "td"
                output.append("<tr>" + "".join(f"<{tag}>" + inline(c.strip()) + f"</{tag}>" for c in cells) + "</tr>")
            output.append("</table>")
            continue
        if line.startswith("## "):
            output.append("<h2>" + inline(line[3:]) + "</h2>")
        elif line.startswith("# "):
            output.append("<h1>" + inline(line[2:]) + "</h1>")
        elif line:
            paragraph = "<p>" + inline(line) + "</p>"
            output.append("<section>" + paragraph + "</section>" if re.match(r"\*\*\d+\.", line) else paragraph)
        index += 1
    return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Tommy Capital</title><style>body{font:16px system-ui;max-width:1400px;margin:32px auto;padding:16px;color:#172a3a}"
            "table{border-collapse:collapse;width:100%;margin-bottom:28px}td,th{padding:8px;text-align:left;border:1px solid #ccd6df}"
            "th{background:#edf3f8}p{line-height:1.8;overflow-wrap:anywhere}section{margin:18px 0}</style>"
            + "".join(output) + "</html>")


def main():
    parser = argparse.ArgumentParser(description="把完整扫描JSON转换为中文可读报告，不重新抓取行情")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-url")
    args = parser.parse_args()
    report = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if "rankings" not in report or "observations" not in report:
        parser.error("必须使用完整report.json，不能把TOP20摘要当成全部结果")
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(report, args.run_url), encoding="utf-8")


if __name__ == "__main__":
    main()
