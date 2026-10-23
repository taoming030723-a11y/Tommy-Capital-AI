"""Focused technical observations, independent of score and ranking reports."""


def render_technical_reviews(result):
    from .reporting import cell, number, table
    names = [("monthly", "月线"), ("weekly", "周线"), ("daily", "日线"),
             ("60", "60分钟"), ("30", "30分钟"), ("15", "15分钟")]
    lines = ["## 观察名单六周期技术评估", "",
             "行情使用各周期已完成K线；技术条件与ChatGPT解释分开，不改变评分、排名或做T资格。基本面与估值复用已有基准。", ""]
    rows = result.get("rows", [])
    def label(frame):
        return frame.get("label", "数据不足，无法判断") if frame.get("status") == "ok" else "数据不足，无法判断"
    lines += table(["股票", *[name for _, name in names], "技术买点判断"], [
        [str(r.get("name") or r["code"]) + "（" + r["code"] + "）",
         *[label(r.get("frames", {}).get(p, {})) for p, _ in names], r.get("decision", "未完成")]
        for r in rows] or [["暂无符合当前观察条件的股票", *["—"] * 7]])
    from .kd_reporting import render_kd_rows
    lines += render_kd_rows(rows)
    for row in rows:
        lines += ["**" + cell(row.get("name") or row["code"]) + "（" + row["code"] + "）**：" + cell(row.get("decision")), ""]
        for period, name in names:
            frame = row.get("frames", {}).get(period, {})
            if frame.get("status") != "ok":
                lines += [name + "：" + cell(frame.get("error") or frame.get("label") or "未完成检查，不能认定没有信号") + "。", ""]
                continue
            ma = frame.get("averages_qfq", {})
            macd, kdj = frame.get("macd", {}), frame.get("kdj", {})
            lines += [name + "：价格" + number(frame.get("price")) + "；MA5/13/20/55 " +
                "/".join(number(ma.get(str(n))) for n in (5, 13, 20, 55)) +
                "；DIF/DEA/柱 " + "/".join(number(macd.get(k)) for k in ("dif", "dea", "histogram")) +
                "；K/D/J " + "/".join(number(kdj.get(k)) for k in ("k", "d", "j")) +
                "；" + cell(macd.get("histogram_change")) + "；完整K线" + cell(frame.get("last_bar")) + "。", ""]
        unmet = row.get("unmet_conditions", [])
        lines += ["尚缺：" + cell("；".join(r["label"] for r in unmet) or "技术观察条件齐备，仍需人工核对基本面资格、支撑及仓位") + "。", ""]
        ai = row.get("ai", {})
        if ai.get("status") == "complete":
            analysis = ai["explanation"]
            lines += ["ChatGPT解释（" + cell(ai.get("model")) + "）：" + cell(analysis["summary"]), ""]
            for period, name in names:
                lines += [name + "：" + cell(analysis["period_comments"][period]), ""]
            lines += ["风险：" + cell("；".join(analysis["risks"])) + "。后续确认：" + cell("；".join(analysis["next_conditions"])) + "。", ""]
    return lines
