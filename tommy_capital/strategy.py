import math
import pandas as pd


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


def score(row, technical):
    # All scores are research heuristics, not return forecasts or fair values.
    growth = min(max(float(row["revenue_yoy"]), 0), 40) / 40 * 10
    profit = min(max(float(row["profit_yoy"]), 0), 60) / 60 * 15
    cash = 5.0 if float(row["cfo_per_share_ytd"]) > 0 else 0.0
    ratio = float(row["industry_pe_ratio"])
    valuation = max(0.0, min(25.0, (1.5 - ratio) / 1.5 * 25))
    trend = sum(10 for key in ["daily_trend", "weekly_trend", "monthly_trend"] if technical[key])
    improvement = 5.0 if row["fundamental_improving"] else 0.0
    low_j = 5.0 if row["fundamental_improving"] and technical["monthly_j"] < 20 else 0.0
    volume = 5.0 if (technical["volume_ratio20"] or 0) >= 1.2 else 0.0
    breakdown = {"revenue_growth_10": growth, "profit_growth_15": profit, "positive_cfo_5": cash,
                 "industry_valuation_25": valuation, "large_timeframe_trend_30": trend,
                 "profit_growth_acceleration_5": improvement, "monthly_j_bonus_5": low_j,
                 "volume_confirmation_5": volume}
    return round(sum(breakdown.values()), 2), {k: round(v, 2) for k, v in breakdown.items()}


def evaluate(row, technical, config, today):
    reasons = base_checks(row, config, today)
    if row.get("liquidity_deferred", False):
        average = numeric(technical.get("average_turnover20_cny"))
        if average is None or average < config["min_turnover_cny"]:
            reasons.append("最近20个完整交易日平均成交额缺失/低于流动性门槛")
    for field in ["daily_trend", "weekly_trend", "monthly_trend"]:
        if not technical.get(field):
            reasons.append(f"{field} 未确认")
    total, breakdown = (score(row, technical) if not base_checks(row, config, today) else (None, {}))
    return {"eligible": not reasons, "reasons": reasons, "score": total,
            "score_breakdown": breakdown,
            "monthly_low_j_watch": bool(technical.get("monthly_j", 100) < 20 and row.get("fundamental_improving", False)),
            "manual_review": ["核对扣非净利润及非经常性损益", "核对行业景气、订单及公告原文",
                              "核对财报重述/股本变化、负债及股东减持", "以自身止损和仓位制度作最后决策"]}
