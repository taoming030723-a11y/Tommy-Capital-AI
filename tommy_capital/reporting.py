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


def ranking_rows(rows):
    output = []
    for row in rows:
        technical = row.get("technical", {})
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
    coverage = report.get("coverage", {})
    status = {"complete": "完成", "partial": "部分完成", "failed": "失败", "running": "扫描中"}.get(report.get("status"), "未确定")
    mode = {"intraday": "盘中扫描", "closed_session": "最近完整交易日扫描", "daily_baseline": "日线预检"}.get(report.get("scan_type"), "尚未确定")
    candidates = report.get("rankings", [])
    observations = report.get("observations", [])
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
        ["战略候选总数", f"{len(candidates)}只"],
        ["数据问题", f"{len(report.get('errors', []))}条（详见缺失清单）"]])
    scope = "全部基本面／估值初筛合格股票" if coverage.get("minute15_scope") == "screened" else "战略候选及月线低J观察股"
    lines += [f"15分钟范围：{scope}；覆盖数按实际成功检查计。以下背离按确认时间排序，同一股票可能同时出现MACD与KDJ记录，条数不等于股票数。", "",
              "## ⭐ 战略候选 TOP 10", "",
              f"本次共有{len(candidates)}只通过战略规则的候选，前10只按综合评分排列。评分表示研究优先级，不代表收益概率。", ""]
    if candidates:
        lines += expandable(["股票", "代码", "综合评分", "日线趋势", "周线趋势", "月线趋势", "月线J"],
                            ranking_rows(candidates), 10, "查看其余战略候选")
    else:
        lines += ["本次未列出战略候选；结合检查状态和数据缺失判断，不能将检查失败理解为全市场没有候选。", ""]
    lines += ["## 📈 日线背离", ""]
    lines += signal_section(report, "日线底背离", "daily", "bullish", coverage.get("technical_completed", 0))
    lines += signal_section(report, "日线顶背离", "daily", "bearish", coverage.get("technical_completed", 0))
    lines += ["## ⏱ 15分钟背离", ""]
    lines += signal_section(report, "15分钟底背离", "15m", "bullish", coverage.get("minute15_completed", 0))
    lines += signal_section(report, "15分钟顶背离", "15m", "bearish", coverage.get("minute15_completed", 0))
    watches = [r for r in candidates + observations if r.get("monthly_low_j_watch")]
    watches.sort(key=lambda r: (-(r.get("score") or 0), r.get("code", "")))
    lines += ["## 👀 月线 J < 20 ＋ 基本面改善", "",
              f"共{len(watches)}只观察股。仅作观察加分，不能替代估值与日／周／月趋势规则。改善采用已公告累计净利润同比增速较相邻报告期加快的代理，仍需核对扣非与经营证据。", ""]
    if watches:
        rows = [[r.get("name"), r.get("code"), number(r.get("technical", {}).get("monthly_j")),
                 number(r.get("previous_profit_yoy")) + "%", number(r.get("profit_yoy")) + "%",
                 "通过" if r.get("eligible") else "未通过，仅观察"] for r in watches]
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
              "基本面、估值与日／周／月大周期决定方向；日线和15分钟背离只作观察，不单独成为买点。做T仅限已经确认的持仓且通过战略规则，并结合60分钟结构、支撑阻力与量价确认；本系统没有下单授权。", "",
              "战略候选仍需人工核对扣非净利润、行业景气、订单、公告、负债及股本变化。数据缺失不会被补成行情或解释成没有背离。", ""]
    if run_url:
        lines += [f"[查看本次 Actions、完整报告附件与运行日志]({run_url})", "",
                  "附件保留完整JSON、CSV、HTML与中文Markdown；逐股原始错误和数据来源可在附件里复核。", ""]
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
