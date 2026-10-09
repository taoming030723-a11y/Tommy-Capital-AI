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
from .scoring import ranked_top20, TECHNICAL_WEIGHTS, SCORING_VERSION, ranking_eligible


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
    displayed = ranked_top20(pool)
    status = {"complete": "完成", "partial": "部分完成", "failed": "失败", "running": "扫描中"}.get(report.get("status"), "未确定")
    mode = {"intraday": "盘中", "after_close": "收盘后", "closed_session": "最近完整交易日", "daily_baseline": "日线预检"}.get(report.get("scan_type"), "待确定")
    phase = {"opening": "开盘报告", "closing": "收盘报告", "intraday": "盘中报告", "pre_run": "预跑报告"}.get(report.get("scan_phase"), "扫描报告")
    lines = ["# Tommy Capital A股扫描报告", "", f"**{phase} · {status} · {mode} · 盈利质量与全市场覆盖升级版**", ""]
    if str(report.get("generated_at", "")).startswith("2026-10-07"):
        lines += ["> 这是2026年10月7日预跑，不是10月8日开盘或收盘结果。", ""]
    if not report.get("scan_complete", False):
        lines += ["> **完整性：scan_complete=false。报价目录扫描与逐股技术、领先证据的完整覆盖不同，不能把本报告称为全部市场检查完成。**", ""]
    if report.get("status") == "failed":
        lines += ["> **扫描失败，未形成可用选股结论。候选、背离与做T名单尚未完成检查，不能解读为全市场没有合格股票。**", "",
                  f"扫描启动：{date_label(report.get('generated_at'))}  ", f"扫描停止：{date_label(report.get('finished_at'))}  ",
                  f"目标日线日期（未完成逐股核验）：{date_label(report.get('session'), True)}  ",
                  f"目标15分钟截止（未完成逐股核验）：{date_label(report.get('minute15_cutoff'))}  ", "",
                  "## 实际读取与停止原因", ""]
        lines += table(["项目", "实际结果"], [
            ["报价目录读取", f"{coverage.get('universe', '未取得')}只；源列表{coverage.get('source_universe', '未取得')}只"],
            ["选股与执行结论", "未完成，候选数量无法判断"]])
        lines += table(["环节", "原因"], [["扫描", failure_reason(e)] for e in report.get("errors", [])])
        if run_url:
            lines += [f"[本次 Actions 与日志]({run_url})", ""]
        if os.environ.get("REPORT_ARTIFACT_URL"):
            lines += [f"[下载本次失败诊断附件]({os.environ['REPORT_ARTIFACT_URL']})", ""]
        return "\n".join(lines)
    if not report.get("ranking_complete", False):
        lines += ["> **排名完整性：ranking_complete=false。以下为已核验范围内、通过月线战略入口及周日确认的暂定排名，不是全市场完整排名。**", ""]
        if report.get("ranking_incomplete_reasons"):
            lines += ["未完成项：" + "；".join(report["ranking_incomplete_reasons"]) + "。", ""]
    lines += [f"扫描启动：{date_label(report.get('generated_at'))}  ", f"扫描完成：{date_label(report.get('finished_at'))}  ",
              f"最新完整日线：{date_label(report.get('session'), True)}；15分钟完整K线：{date_label(report.get('minute15_cutoff'))}。", "",
              "时间为北京时间／新加坡时间。月／周只使用已完成周期，不把当月未完成K线当成完整月线。", ""]
    def count(key):
        return coverage.get(key, "未取得")
    lines += table(["检查项目", "实际结果"], [
        ["全市场实际报价目录", f"{count('universe')}只；源列表{count('source_universe')}只"],
        ["基本面评估／已公告财报", f"{count('financially_evaluated')} / {count('financial_data_available')}只"],
        ["A基本面估值通过／B预检与证据估值通过", f"{count('route_a_fundamental_passed')}；{count('route_b_prequalified')} / {count('route_b_fundamental_passed')}只"],
        ["B领先证据、催化或估值待齐", f"B证据未齐{count('leading_evidence_missing')}只；缺失不通过"],
        ["日／周／月成功／全部报价请求", f"{count('technical_completed')} / {count('technical_requested')}只；已尝试{count('technical_attempted')}只"],
        ["选择性重抓恢复／未解决；其中历史不足", f"{count('technical_recovered')} / {count('technical_failed')}；{count('technical_history_shortfall')}只"],
        ["财务质量报表匹配", f"{count('quality_statement_available')}只；缺失不按零利润或零现金流处理"],
        ["独立长历史月线成功／请求", f"{count('extended_monthly_completed')} / {count('extended_monthly_requested')}只；全部完整重叠月的前复权OHLC与日线聚合核对"],
        ["60分钟成功／请求", f"{count('minute60_completed')} / {count('minute60_requested')}只"],
        ["30分钟成功／请求", f"{count('minute30_completed')} / {count('minute30_requested')}只"],
        ["15分钟成功／请求", f"{count('minute15_completed')} / {count('minute15_requested')}只"],
        ["观察池总数（candidate_count）／本报告展示", f"{len(pool)} / {len(displayed)}只；周日确认并且评分输入齐全者才进入主榜，最多20只"],
        ["A池／B池；当前月J<20／恢复路径", f"{count('route_a_pool_count')} / {count('route_b_pool_count')}；{count('monthly_current_low_count')} / {count('monthly_recovery_count')}只"],
        ["周线筑底／扩展结构；普通异常量／首次突破", f"{count('weekly_base_count')} / {count('weekly_structure_count')}；{count('daily_abnormal_volume_count')} / {count('daily_first_breakout_count')}只"],
        ["战略量价确认／再通过原日周月均线", f"{count('strategic_confirmed_count')} / {count('t_trend_confirmed_count')}只"],
        ["观察池评分输入完整／全报价未请求日周月", f"{count('score_inputs_completed')} / {count('unscanned')}只"],
        ["数据源及历史错误", f"{len(report.get('errors', []))}条；完整逐股原因见JSON/CSV附件"]])
    lines += ["## 综合评分：基本面50分＋技术面50分", ""]
    lines += table(["部分", "满分", "评分内容"], [
        ["基本面", "50", "营收4；扣非盈利质量或已验证领先指标14；现金流及利润转化14；估值14；同期核心改善4"],
        ["月线", "12", "超卖／恢复路径5；J上升3；趋势2；MACD动量1；已确认MACD底背离1"],
        ["周线", "10", "筑底／低点抬高／收敛／突破4；趋势2；MACD动量2；MACD底背离2"],
        ["日线", "10", "放量／首次突破4；趋势2；MACD动量2；MACD底背离2"],
        ["60分钟", "8", "价格／MA20结构3；MACD动量2；MACD底背离3"],
        ["30分钟", "5", "价格／MA20结构2；MACD动量1；MACD底背离2"],
        ["15分钟", "5", "价格／MA20结构2；MACD动量1；MACD底背离2"]])
    lines += ["强MACD顶背离或KDJ顶背离扣该周期底背离项，分时过热再扣1分。MACD须通过价格延伸、ATR归一化DIF改善、至少5根拐点间距、同侧零轴、右侧反弹与未破坏确认；微弱背离保留审计但不加分。缺失分项不重分配权重，评分未齐不进入主榜。分数是研究优先级，不是收益预测；财务质量与大周期优先于小周期信号。", "",
        "**A 已兑现型**：保留原盈利、现金流、TTM PE、PB、行业相对估值和流动性规则；当前完整月J<20，或近3个完整月曾<20且最新完整月J上升（恢复路径）。", "",
        "**B 领先拐点型**：保留近6个完整月超卖恢复、价格压缩、营收>10%、已验证销量／订单增长或规模交付、核心业务改善、未来6个月催化与有安全边际的三情景估值。TTM亏损允许通过此路线，强制标“盈利尚未兑现”；证据或估值缺失仍不入池。两路统一排名，不为某股票或类别预留名额。", "",
        f"日线量比＝最新完整交易日成交量／此前20个完整交易日平均量，排除检测日。普通异常门槛{number(config.get('daily_volume_abnormal_ratio', 2))}倍；首次平台突破独立门槛{number(config.get('daily_breakout_rvol', 1.5))}倍、收盘位置≥{number(config.get('daily_breakout_close_location', .65))}。", "",
        "## ⭐ 综合评分 TOP 20", "",
        "只呈现月线入口、基本面估值、周线结构及周日趋势、日线量价已确认且六周期评分齐全的股票，按50/50总分排序。不足20只按实际数量列出。其余月线观察股票保留在完整附件，60/30/15分钟不能赋予选股资格，15分钟无背离不会单独淘汰。", ""]
    rows = []
    week_labels = {"base": "筑底", "higher_low": "低点抬高", "volatility_contraction": "收敛", "platform_breakout": "平台突破"}
    for index, row in enumerate(displayed, 1):
        tech = row.get("technical", {})
        weekly = tech.get("weekly_structure", {})
        week = "／".join(week_labels[k] for k, v in weekly.get("conditions", {}).items() if v and k in week_labels) or ("确认" if weekly.get("confirmed") else "待确认")
        profit = row.get("profit_status") or row.get("leading", {}).get("profit_label") or "未取得"
        rows.append([index, row.get("name"), row.get("code"), row.get("route"), number(row.get("score")),
            number(row.get("fundamental_score")), number(row.get("technical_score")), profit,
            number(tech.get("monthly_j")), "当前超卖" if row.get("monthly_low_j_watch") else "恢复路径", week,
            number(tech.get("volume_ratio20")) + "倍", minute_execution_status(row.get("minute15", {})),
            "齐全" if row.get("score_inputs_complete") else "输入未齐"])
    lines += table(["名次", "股票", "代码", "路线", "综合/100", "基本面/50", "技术/50", "TTM状态", "完整月J", "月J路径", "周结构", "20日量比", "15分钟观察状态", "评分数据"], rows) if rows else ["当前已核验范围内暂无同时完成周日确认与完整评分的主榜候选；月线观察池仍见覆盖计数，不解释为全市场无机会。", ""]
    if report.get("scoring_version") != SCORING_VERSION:
        lines += ["> 此文件为旧版扫描，50/50多周期评分尚未重算；未取得的新分项不视作已检查。", ""]
    if displayed:
        lines += ["## TOP20多周期分项与背离确认", "", "每格为该周期得分／满分；底背离只计MACD分项，KDJ背离仍如实展示。确认时间为第3根右侧K线完成时，未确认的拐点不记成信号。", ""]
        detail_rows = []
        labels = {"monthly": "月", "weekly": "周", "daily": "日", "60": "60分钟", "30": "30分钟", "15": "15分钟"}
        for row in displayed:
            score_parts = row.get("score_breakdown", {}).get("technical", {})
            frames = {**row.get("technical", {}).get("timeframes", {}), **row.get("minute_frames", {})}
            frames.setdefault("daily", row.get("technical", {}).get("daily_divergences", {}))
            frames.setdefault("15", row.get("minute15", {}))
            cells = []
            for period, maximum in TECHNICAL_WEIGHTS.items():
                frame = frames.get(period, {})
                part = score_parts.get(period, {})
                if frame.get("status") == "ok":
                    signal_text = "；".join(("MACD" if s.get("indicator") == "MACD_DIF" else "KDJ") +
                        ("底" if s.get("direction") == "bullish" else "顶") + "（" + date_label(s.get("confirmed_at"), period in ["monthly", "weekly", "daily"]) + "）" for s in frame.get("signals", [])) or "无已确认背离"
                else:
                    signal_text = "数据未齐／未检查"
                if frame.get("divergence_status") == "insufficient_bars":
                    signal_text = "背离历史不足，无法判断"
                cells.append(number(part.get("score")) + f"/{maximum}；" + signal_text)
            detail_rows.append([row.get("name") + " " + row.get("code"), *cells])
        lines += table(["股票", *labels.values()], detail_rows)
        lines += ["## TOP20盈利质量", ""]
        quality_rows = []
        for row in displayed:
            quality = row.get("fundamental_quality", {})
            quality_rows.append([row.get("name") + " " + row.get("code"),
                number(quality.get("core_profit_share_ttm")), number(quality.get("cash_conversion_ttm")),
                "低基数，增速加分受限" if quality.get("low_base_flag") else "非低基数／需核对",
                "通过" if quality.get("core_margin_improving") else "未确认"])
        lines += table(["股票", "TTM扣非／归母利润", "TTM经营现金流／归母利润", "同比基数", "同期核心利润率改善"], quality_rows)
        lines += ["现金流比率使用报表总额；亏损企业不对负利润计算转化率，B路线按现金流／营收及已核验业务证据评分。", "",
                  "## TOP20月J路径、首次突破与执行限制", ""]
        review_rows = []
        t_rows = []
        for row in displayed:
            tech = row.get("technical", {})
            recovery = tech.get("monthly_recovery", {})
            path = " → ".join(number(p.get("j")) for p in recovery.get("history", [])[-3:]) or number(recovery.get("previous_j")) + " → " + number(tech.get("monthly_j"))
            first = tech.get("first_volume_breakout", {}).get("first_event") or {}
            execution = row.get("execution", {})
            if not row.get("t_trend_confirmed"):
                execution_text = "尚未通过全部做T趋势条件，分时仅作执行研究"
            elif not row.get("minute15", {}).get("signals"):
                execution_text = "基本面及大周期通过，无15分钟已确认背离"
            else:
                execution_text = "执行量价已确认，仍须确认持仓" if execution.get("entry_watch") else "15分钟执行观察，等待支撑／60分钟／30分钟／量价确认"
            held_t = row.get("confirmed_holding") and row.get("tactical", {}).get("entry_eligible")
            if held_t:
                t_rows.append(row)
            review_rows.append([row.get("name") + " " + row.get("code"), path, date_label(tech.get("monthly_bar_date"), True),
                date_label(first.get("date") or first.get("confirmed_at"), True) if first else "未确认",
                execution_text, "已确认持仓做T观察合格" if held_t else "无已确认做T资格"])
        lines += table(["股票", "最近完整月J", "月线截至", "首次放量突破", "15分钟执行观察", "持仓做T"], review_rows)
        lines += [f"**已确认持仓且做T底背离进场观察合格：{len(t_rows)}只。** " + ("名单见上述TOP20。" if t_rows else "当前展示名单为空；历史持仓记忆不视为当前持仓确认。"), ""]
    lines += ["## 数据缺失与复核", ""]
    error_counts = {}
    for message in report.get("errors", []):
        if "独立长历史月线" in message:
            kind = "独立月线历史或复权价格未通过"
        elif "分钟" in message:
            kind = "分时缺失／完整K线／复权校验"
        elif "周/月" in message:
            kind = "完整周／月历史不足"
        elif "三路日线" in message:
            kind = "前复权日线源不可用／日期价格未通过"
        elif "日线" in message:
            kind = "日线历史或最新日期不足"
        else:
            kind = "全局或证据数据源错误"
        error_counts[kind] = error_counts.get(kind, 0) + 1
    lines += table(["缺失类别", "记录数"], list(error_counts.items())) if error_counts else ["未记录行情／历史接口错误；B证据缺口见上方覆盖。", ""]
    lines += ["完整JSON/CSV保留全观察池、池外记录、逐股失败、证据和评分细项；可读报告不再展开其他股票。候选不足20只时按实际数量展示。低J、周线筑底、放量和背离均不单独成为买点。做T仍须原基本面估值、战略量价、日周月趋势全部确认，以及当日60分钟结构、30分钟无顶背离冲突、支撑反弹与价格／量能确认；过热时等待降温。没有当前确认持仓则不授予做T资格，本系统没有下单授权。", ""]
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
        parser.error("必须使用完整report.json，不能把TOP20摘要当成全部结果")
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(report, args.run_url), encoding="utf-8")


if __name__ == "__main__":
    main()
