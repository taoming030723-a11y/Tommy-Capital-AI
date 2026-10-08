import math
import pandas as pd
from .inflection import leading_check
from .scoring import balanced_score

SELECTION_RULE = "dual_route_monthly_recovery"


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def prepare_universe(spot, finance, config):
    universe = spot.merge(finance.drop(columns="name"), on="code", how="left")
    universe["pe_ttm"] = universe.market_cap / universe.profit_ttm.where(universe.profit_ttm > 0)
    valid = universe[(universe.pe_ttm > 0) & (universe.pe_ttm <= 200) &
                     universe.industry.notna() & ~universe.name.str.contains("ST|退", case=False, na=False)]
    if "quote_clock_ok" in valid:
        valid = valid[valid.quote_clock_ok]
    peers = valid.groupby("industry").pe_ttm.agg(["median", "count"])
    universe["industry_pe_median"] = universe.industry.map(peers["median"])
    universe["industry_pe_samples"] = universe.industry.map(peers["count"])
    universe["industry_pe_ratio"] = universe.pe_ttm / universe.industry_pe_median
    return universe


def base_checks(row, config, today):
    """Reject absent/nonfinite inputs; never replace missing financials with zero."""
    reasons = []
    def minimum(field, bound, strict=False):
        val = numeric(row.get(field))
        if val is None:
            reasons.append(f"{field} 缺失")
        elif (val <= bound if strict else val < bound):
            reasons.append(f"{field} 未达下限 {bound}")
    def maximum(field, bound):
        val = numeric(row.get(field))
        if val is None:
            reasons.append(f"{field} 缺失")
        elif val > bound:
            reasons.append(f"{field} 超过上限 {bound}")
    if "ST" in str(row.get("name", "")).upper() or "退" in str(row.get("name", "")):
        reasons.append("风险警示/退市名称")
    if row.get("quote_clock_ok") is False:
        reasons.append("行情时刻未确认当前盘中报价（可能停牌/数据滞后）")
    minimum("price", 0, True)
    minimum("market_cap", 0, True)
    # During market hours, partial-day turnover is not a full-day liquidity
    # measurement. The technical stage uses 20 completed sessions instead.
    if not row.get("liquidity_deferred", False):
        minimum("turnover", config["min_turnover_cny"])
    minimum("profit_ytd", 0, True)
    minimum("profit_ttm", 0, True)
    minimum("revenue_yoy", config["min_revenue_yoy_pct"])
    minimum("profit_yoy", config["min_profit_yoy_pct"])
    minimum("cfo_per_share_ytd", 0, True)
    previous_cfo = numeric(row.get("previous_same_cfo_per_share_ytd"))
    current_cfo = numeric(row.get("cfo_per_share_ytd"))
    if previous_cfo is None:
        reasons.append("上年同期经营现金流/股缺失，无法确认现金流没有恶化")
    elif current_cfo is not None and current_cfo < previous_cfo:
        reasons.append("经营现金流/股低于上年同期；不比较相邻累计季度")
    minimum("pe_ttm", 0, True)
    maximum("pe_ttm", config["max_pe_ttm"])
    minimum("pb", 0, True)
    maximum("pb", config["max_pb"])
    minimum("industry_pe_samples", config["min_industry_pe_samples"])
    maximum("industry_pe_ratio", config["max_industry_pe_ratio"])
    period = pd.to_datetime(row.get("period"), errors="coerce")
    if pd.isna(period) or not 0 <= (today - period.date()).days <= config["max_finance_age_days"]:
        reasons.append("报告期缺失/过旧")
    return reasons


def leading_prechecks(row, config, today):
    """Separate B route, without borrowing a positive-TTM-PE exemption for A."""
    reasons = []
    if "ST" in str(row.get("name", "")).upper() or "退" in str(row.get("name", "")):
        reasons.append("风险警示/退市名称")
    if row.get("quote_clock_ok") is False:
        reasons.append("行情时刻未确认当前盘中报价")
    for field in ["price", "market_cap", "pb", "revenue_ytd", "revenue_ttm"]:
        value = numeric(row.get(field))
        if value is None or value <= 0:
            reasons.append(f"{field} 缺失/非正")
    for field in ["profit_ytd", "profit_ttm", "cfo_per_share_ytd"]:
        if numeric(row.get(field)) is None:
            reasons.append(f"{field} 缺失；不能把缺失解释为亏损转折")
    if numeric(row.get("pb")) is not None and row["pb"] > config["max_pb"]:
        reasons.append("PB超过原风险上限")
    growth = numeric(row.get("revenue_yoy"))
    if growth is None or growth <= config["leading_min_revenue_growth_pct"]:
        reasons.append("营收同比未超过领先拐点门槛")
    if not row.get("liquidity_deferred", False):
        turnover = numeric(row.get("turnover"))
        if turnover is None or turnover < config["min_turnover_cny"]:
            reasons.append("成交额缺失/未达流动性门槛")
    period = pd.to_datetime(row.get("period"), errors="coerce")
    if pd.isna(period) or not 0 <= (today - period.date()).days <= config["max_finance_age_days"]:
        reasons.append("报告期缺失/过旧")
    return reasons


def financial_routes(row, config, today):
    a = base_checks(row, config, today)
    b = leading_prechecks(row, config, today)
    leading = leading_check(row, row.get("leading_evidence", {}), config, today)
    return {"route_a_passed": not a, "route_a_reasons": a,
            "route_b_prequalified": not b, "route_b_passed": not b and leading["passed"],
            "route_b_reasons": b + leading["reasons"], "leading": leading}


def evaluate(row, technical, config, today):
    finance = row.get("financial_routes") or financial_routes(row, config, today)
    liquidity_reasons = []
    if row.get("liquidity_deferred", False):
        average = numeric(technical.get("average_turnover20_cny"))
        if average is None or average < config["min_turnover_cny"]:
            liquidity_reasons.append("最近20个完整交易日平均成交额缺失/低于流动性门槛")
    monthly_j = numeric(technical.get("monthly_j"))
    recovery = technical.get("monthly_recovery", {})
    previous = numeric(recovery.get("previous_j"))
    minimum3, minimum6 = numeric(recovery.get("min_j_3_months")), numeric(recovery.get("min_j_6_months"))
    rising = monthly_j is not None and previous is not None and monthly_j > previous
    low = monthly_j is not None and monthly_j < 20
    path_a = low or (minimum3 is not None and minimum3 < 20 and rising)
    path_b = minimum6 is not None and minimum6 < 20 and rising
    a_reasons = finance["route_a_reasons"] + liquidity_reasons + ([] if path_a else ["未满足A路线完整月J超卖/近3月恢复路径"])
    b_reasons = finance["route_b_reasons"] + liquidity_reasons + ([] if path_b else ["未满足B路线近6月月J恢复路径"])
    if not technical.get("price_compression", {}).get("experienced"):
        b_reasons += ["近126交易日价格压缩未达门槛"]
    routes = (["A"] if not a_reasons else []) + (["B"] if not b_reasons else [])
    eligible = bool(routes)
    route = routes[0] if routes else None
    reasons = [] if eligible else ["A：" + r for r in a_reasons] + ["B：" + r for r in b_reasons]
    strategy_reasons = reasons.copy()
    if not rising:
        strategy_reasons.append("完整月J尚未向上恢复")
    if not technical.get("weekly_structure", {}).get("confirmed"):
        strategy_reasons.append("周线筑底/低点抬高/收敛/平台突破未确认")
    breakout = technical.get("first_volume_breakout", {}).get("active", False)
    daily_confirmed = breakout if route == "B" else (breakout or technical.get("daily_volume", {}).get("abnormal", False))
    if not daily_confirmed:
        strategy_reasons.append("B首次平台放量突破未确认" if route == "B" else "日线量价确认未满足")
    trend_reasons = [f"{field} 未确认" for field in ["daily_trend", "weekly_trend", "monthly_trend"] if not technical.get(field)]
    t_reasons = strategy_reasons + trend_reasons
    scoring = balanced_score(row, technical, route, finance["leading"]) if eligible else {
        "score": None, "score_breakdown": {}, "fundamental_score": None, "technical_score": None}
    return {"eligible": eligible, "reasons": reasons, **scoring, "route": route, "qualified_routes": routes,
            "route_a_reasons": a_reasons, "route_b_reasons": b_reasons,
            "financial_routes": finance, "leading": finance["leading"],
            "strategic_eligible": not strategy_reasons, "strategy_reasons": strategy_reasons,
            "t_trend_confirmed": not t_reasons, "t_reasons": t_reasons,
            "legacy_ma_confirmed": not trend_reasons,
            "selection_rule": SELECTION_RULE,
            "monthly_low_j_watch": eligible and low,
            "monthly_recovery_watch": eligible and not low,
            "manual_review": ["核对扣非净利润及非经常性损益", "核对行业景气、订单及公告原文",
                              "核对财报重述/股本变化、负债及股东减持", "以自身止损和仓位制度作最后决策"]}
