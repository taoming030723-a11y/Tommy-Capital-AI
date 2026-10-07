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


def render_dual_report(report, run_url=None):
    coverage, config = report.get("coverage", {}), report.get("config", {})
    pool, observations = report.get("rankings", []), report.get("observations", [])
    confirmed = [r for r in pool if r.get("strategic_eligible")]
    status = {"complete": "完成", "partial": "部分完成", "failed": "失败", "running": "扫描中"}.get(report.get("status"), "未确定")
    mode = {"intraday": "盘中", "after_close": "收盘后", "closed_session": "最近完整交易日", "daily_baseline": "日线预检"}.get(report.get("scan_type"), "待确定")
    phase = {"opening": "开盘报告", "closing": "收盘报告", "intraday": "盘中报告", "pre_run": "预跑报告"}.get(report.get("scan_phase"), "扫描报告")
    lines = ["# Tommy Capital A股扫描报告", "", f"**{phase} · {status} · {mode} · 双路线模型2.0**", ""]
    if str(report.get("generated_at", "")).startswith("2026-10-07"):
        lines += ["> 这是2026年10月7日预跑，不是10月8日开盘或收盘结果。", ""]
    if not report.get("scan_complete", False):
        lines += ["> **完整性：scan_complete=false。报价目录扫描与逐股技术、领先证据的完整覆盖不同，不能把本报告称为全部市场检查完成。**", ""]
    lines += [f"扫描启动：{date_label(report.get('generated_at'))}  ", f"扫描完成：{date_label(report.get('finished_at'))}  ",
              f"最新完整日线：{date_label(report.get('session'), True)}  ", f"15分钟完整K线：{date_label(report.get('minute15_cutoff'))}  ",
              "时间为北京时间／新加坡时间。完整月线／周线不包含未结束周期；日线信号在对应交易日收盘后确认。", ""]
    def count(key):
        return coverage.get(key, "未取得")
    lines += table(["检查项目", "实际结果"], [
        ["真实沪深北A股报价目录", f"{count('universe')}只；源列表{count('source_universe')}只"],
        ["基本面两路规则评估／有已公告财报", f"{count('financially_evaluated')} / {count('financial_data_available')}只"],
        ["A已兑现型基本面、估值通过", f"{count('route_a_fundamental_passed')}只"],
        ["B营收及数据预检／领先证据与估值通过", f"{count('route_b_prequalified')} / {count('route_b_fundamental_passed')}只"],
        ["B领先指标、催化或估值待齐", f"{count('leading_evidence_missing')}只；缺失不通过"],
        ["日／周／月线成功／请求", f"{count('technical_completed')} / {count('technical_requested')}只"],
        ["15分钟成功／请求", f"{count('minute15_completed')} / {count('minute15_requested')}只"],
        ["未请求分时／初筛后未请求日线", f"{count('minute15_not_required')} / {count('unscanned')}只"],
        ["观察池总数（candidate_count）", f"{len(pool)}只；TOP名单不是总数"],
        ["A池／B池（主路线计数）", f"{count('route_a_pool_count')} / {count('route_b_pool_count')}只"],
        ["当前月J<20／已向上脱离20", f"{count('monthly_current_low_count')} / {count('monthly_recovery_count')}只"],
        ["周线筑底／扩展周线结构", f"{count('weekly_base_count')} / {count('weekly_structure_count')}只"],
        ["日线普通异常量／有效首次突破", f"{count('daily_abnormal_volume_count')} / {count('daily_first_breakout_count')}只"],
        ["战略量价确认／再通过原日周月均线", f"{count('strategic_confirmed_count')} / {count('t_trend_confirmed_count')}只"],
        ["数据源与历史问题", f"{len(report.get('errors', []))}条，另有B证据覆盖缺口"]])
    lines += ["## 两条独立入口", "",
        "**A 已兑现型**：保留盈利、TTM PE、PB、行业相对估值与流动性规则；现金流须为正且不低于上年同期。完整月J当前<20，或最近3个完整月曾<20且当前J高于上月。", "",
        "**B 领先拐点型**：营收同比>10%，近6个完整月曾J<20且当前向上、近126个交易日曾发生价格压缩；销量／订单>30%增长或实际规模交付、核心业务改善、未来6个月催化及三情景估值均需证据。亏损允许入此路线，标签为“盈利尚未兑现”；不豁免估值。", "",
        f"B估值约束：Base安全边际≥{number(config.get('min_forward_margin_pct', 20))}%，Bear潜在跌幅≤{number(config.get('max_bear_downside_pct', 30))}%；正常化远期PE≤{number(config.get('max_forward_pe', 30))}，EV-Sales倍数≤{number(config.get('max_forward_ev_sales', 4))}。这些是可调整研究阈值，盈利／SOTP情景是假设，不是已实现业绩。", "",
        "入观察池不要求均线已全部转多。战略量价确认另须月J向上、周线结构成立与日线量价确认；B须有效首次平台放量突破。原日／周／月均线确认继续单列，做T采用更严格的全部确认。15分钟无背离不会淘汰观察或战略候选。", ""]
    def pool_rows(rows):
        output = []
        for row in rows:
            technical = row.get("technical", {})
            recovery = technical.get("monthly_recovery", {})
            weekly = technical.get("weekly_structure", {})
            labels = {"base": "筑底", "higher_low": "低点抬高", "volatility_contraction": "收敛", "platform_breakout": "平台突破"}
            week_label = "／".join(labels[k] for k, v in weekly.get("conditions", {}).items() if v) or "待确认"
            first = technical.get("first_volume_breakout", {}).get("first_event") or {}
            output.append([row.get("name"), row.get("code"), row.get("route"), number(technical.get("monthly_j")),
                "当前超卖" if row.get("monthly_low_j_watch") else "恢复路径",
                number(recovery.get("min_j_3_months" if row.get("route") == "A" else "min_j_6_months")), week_label,
                number(technical.get("volume_ratio20")), first.get("confirmed_at", "未检测"),
                "确认" if row.get("strategic_eligible") else "等待", minute_execution_status(row.get("minute15", {})), number(row.get("score"))])
        return output
    headers = ["股票", "代码", "路线", "当前月J", "月J状态", "近3/6月最低J", "周线结构", "最新RVOL20", "首次突破日", "战略量价", "15分钟观察状态", "评分"]
    lines += ["15分钟观察状态显示背离、金叉、回踩反弹、过热或无信号，均不决定选股资格。回踩反弹要求上一根收于MA20上、本根触及MA20后收回其上且收盘高于上一根；正式执行观察及持仓做T资格在后面的独立栏目核对。", ""]
    lines += ["## ⭐ 战略量价确认候选 TOP 10", "", f"共{len(confirmed)}只；只是研究候选，仍需原均线与执行确认。", ""]
    lines += expandable(headers, pool_rows(confirmed), 10, "其余战略量价确认候选") if confirmed else ["本次尚无全部满足战略量价确认的候选；观察池见下方。", ""]
    lines += ["## 👀 双路线观察池 TOP 10", "", f"全部{len(pool)}只均已通过各自基本面／估值／流动性与月J路径。按研究评分排序，评分不代表收益概率。", ""]
    lines += expandable(headers, pool_rows(pool), 10, "展开其余观察候选") if pool else ["本次未列出合格观察候选；检查失败不能等同全市场没有候选。", ""]
    lines += ["## 周线结构与日线首次异常放量突破", "",
        "周线使用原6周量价筑底代理，并增加低点抬高、波动收敛与突破此前6周高点。使用完整周且按实际交易日数校正周量；这些条件不证明主力吸筹。", "",
        f"最新完整日线普通异常量：RVOL20≥{number(config.get('daily_volume_abnormal_ratio', 2))}。首次突破：RVOL20≥{number(config.get('daily_breakout_rvol', 1.5))}、收盘超过此前20日最高价、(收盘−最低)/(最高−最低)≥{number(config.get('daily_breakout_close_location', .65))}，此前20日平台振幅≤{number(config.get('daily_breakout_platform_range_pct', 20))}%。", "",
        "两个量比的分母均为检测日**之前**20个完整交易日均量，不含检测当日；不把早盘未完成成交量与全天均量比较。首次事件按当时已知数据检测，保留事件日期；后续未守住原平台则取消有效突破确认。", ""]
    breakout_rows = []
    for row in pool:
        breakout = row.get("technical", {}).get("first_volume_breakout", {})
        event = breakout.get("first_event")
        if event:
            breakout_rows.append([row["name"], row["code"], event["confirmed_at"] + "（收盘后）", number(event["ratio20"]),
                number(event["close_location"]), number(event["platform_high_qfq"]), "仍守住平台" if breakout.get("active") else "已失守"])
    lines += expandable(["股票", "代码", "首次确认", "事件RVOL20", "收盘位置", "平台价（前复权）", "当前状态"], breakout_rows, 10, "其余首次突破") if breakout_rows else ["观察池内本次未检测到有效窗口中的首次平台突破。", ""]
    lines += ["## 领先拐点证据覆盖与待核实", ""]
    evidence = report.get("leading_evidence_coverage", {})
    lines += [f"官方证据登记{evidence.get('companies_registered', 0)}家公司；请求{evidence.get('sources_requested', 0)}个来源，来源抓取成功{evidence.get('sources_verified', 0)}个。登记覆盖不等于全市场覆盖。B预检合格但证据／估值待齐{count('leading_evidence_missing')}只，不计入主候选。", ""]
    pending = [r for r in observations if r.get("financial_routes", {}).get("route_b_prequalified") and
               r.get("technical", {}).get("monthly_recovery", {}).get("route_b_path") and
               r.get("technical", {}).get("price_compression", {}).get("experienced")]
    pending.sort(key=lambda r: (not r.get("leading", {}).get("leading_indicator_confirmed"),
                              not r.get("technical", {}).get("first_volume_breakout", {}).get("active"),
                              not r.get("technical", {}).get("weekly_structure", {}).get("confirmed"), r.get("code", "")))
    if pending:
        lines += [f"其中已检查技术且满足B月J恢复与价格压缩的待核实股{len(pending)}只，以下仅列前10，优先列已核对领先指标、其次有首次突破与周结构者，均未通关：", ""]
        lines += table(["股票", "代码", "月J", "首次突破", "未通过原因"], [[r.get("name"), r.get("code"),
            number(r["technical"].get("monthly_j")), (r["technical"].get("first_volume_breakout", {}).get("first_event") or {}).get("confirmed_at", "未检测"),
            "；".join(r.get("leading", {}).get("reasons", []))] for r in pending[:10]])
    leading_pool = [r for r in pool if r.get("route") == "B"]
    for row in leading_pool:
        lines += [f"### {cell(row['name'])} {cell(row['code'])} · {cell(row.get('leading', {}).get('profit_label'))}", ""]
        for record in row.get("leading", {}).get("records", []):
            if record.get("verification") == "verified":
                lines += [f"- {cell(record.get('field'))}：{cell(record.get('value'))}；[官方来源]({record['url']})，发布{cell(record.get('published_at'))}。"]
        lines += [""]
    signal_report = {**report, "divergences": [s for s in report.get("divergences", []) if s.get("strategy_pool_eligible", s.get("monthly_pool_eligible"))]}
    lines += ["## 📈 日线背离（仅主观察池）", ""]
    lines += signal_section(signal_report, "日线底背离", "daily", "bullish", len(pool))
    lines += signal_section(signal_report, "日线顶背离", "daily", "bearish", len(pool))
    lines += ["## ⏱ 15分钟背离（仅主观察池）", ""]
    lines += signal_section(signal_report, "15分钟底背离", "15m", "bullish", coverage.get("monthly_pool_minute15_completed", 0))
    lines += signal_section(signal_report, "15分钟顶背离", "15m", "bearish", coverage.get("monthly_pool_minute15_completed", 0))
    execution_rows = [r for r in pool if r.get("t_trend_confirmed") and r.get("minute15", {}).get("signals")]
    lines += ["## 🎯 基本面、大周期通过的15分钟执行／做T观察", "",
        "只有基本面／估值、月J路径、周日量价和原日／周／月均线全部确认的股票才列入本节。小周期背离仅有执行观察权，不能独立选股或成为新开仓理由。", "",
        f"底背离还须确认支撑附近反弹、价格结构收复、15分钟量比≥{number(config.get('execution_volume_ratio', 1.2))}、当日已完成60分钟结构在MA20上及30分钟无顶背离冲突；支撑距离≤{number(config.get('execution_support_distance_pct', 3))}%。15分钟J>100且收盘超过MA20的1.03倍时标过热，等待降温确认。开盘时当日60分钟尚未完成，会标等待。顶背离列为减仓／过热风险观察。", ""]
    if execution_rows:
        rows = []
        for row in execution_rows:
            execution = row.get("execution", {})
            confirmed_at = "；".join(date_label(s.get("confirmed_at")) for s in row["minute15"]["signals"])
            labels = {"bottom_divergence": "底背离", "no_15m_top_conflict": "无15分钟顶背离冲突", "not_overheated": "过热降温",
                "near_confirmed_support_and_rebound": "支撑附近反弹", "price_structure_reclaimed": "价格结构收复",
                "volume_confirmation": "量能确认", "same_session_60m_structure": "当日60分钟结构",
                "30m_without_top_conflict": "30分钟无顶背离冲突"}
            waiting = "／".join(labels.get(k, k) for k, v in execution.get("conditions", {}).items() if not v)
            rows.append([row["name"], row["code"], minute_label(row["minute15"]), confirmed_at,
                "量价/结构已确认" if execution.get("entry_watch") else "等待：" + (waiting or execution.get("status", "未检查")),
                "已确认持仓" if row.get("confirmed_holding") else "持仓未确认，仅执行观察",
                "持仓做T观察合格" if row.get("tactical", {}).get("entry_eligible") else "不具备已确认做T进场资格"])
        lines += table(["股票", "代码", "15分钟", "背离确认时间", "执行确认", "持仓状态", "做T资格"], rows)
    else:
        lines += ["本次没有同时通过上述基本面、大周期规则且出现已确认15分钟背离的股票。", ""]
    t_rows = [r for r in execution_rows if r.get("confirmed_holding") and r.get("tactical", {}).get("entry_eligible")]
    lines += [f"**已确认持仓且做T底背离进场观察合格：{len(t_rows)}只。** " + ("、".join(r["name"] + " " + r["code"] for r in t_rows) if t_rows else "当前合格名单为空；系统不把历史聊天中的持仓当作当前确认持仓。"), "",
              "A股当日新买股份不可当日卖出；本系统不下单，观察确认也不保证获利。", ""]
    lines += ["## ⚠️ 数据缺失与未扫描", ""]
    names = {r.get("code"): r.get("name") for r in pool + observations + report.get("excluded", [])}
    errors = []
    for message in report.get("errors", []):
        match = re.match(r"^(\d{6})(?:\s+([^：:]+))?[:：]\s*(.*)", message, re.DOTALL)
        if match:
            code, stage, reason = match.groups()
            errors.append([names.get(code, "名称未取得"), code, stage or "日线/大周期", failure_reason(reason)])
        else:
            errors.append(["全局/数据源", "—", "扫描", failure_reason(message)])
    lines += table(["股票", "代码", "环节", "原因"], errors) if errors else ["本次未记录接口/历史校验错误；领先证据缺口仍须单独查看。", ""]
    lines += [f"B证据未齐{count('leading_evidence_missing')}只；初筛后未请求深度检查{count('unscanned')}只。完整JSON/CSV保留池外技术记录、财务排除与逐股证据缺失，池外背离不混入主名单。", "",
              "## 阅读与复核", "", "先看基本面、估值与大周期，再看执行信号。月J低位、筑底、放量和背离均不能单独成为买点。保留真实数据、历史长度、复权与最新交易日校验；缺失不填成行情、不改为完整。", ""]
    if run_url:
        lines += [f"[本次 Actions、完整附件与日志]({run_url})", ""]
    if os.environ.get("REPORT_ARTIFACT_URL"):
        lines += [f"[下载本次完整报告附件]({os.environ['REPORT_ARTIFACT_URL']})", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="把完整扫描JSON转换为中文可读报告，不重新抓取行情")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-url")
    args = parser.parse_args()
    report = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if "rankings" not in report or "observations" not in report:
        parser.error("必须使用完整report.json，不能把前15名摘要当成全部结果")
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(report, args.run_url), encoding="utf-8")


if __name__ == "__main__":
    main()
