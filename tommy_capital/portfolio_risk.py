"""Private, explicit-input portfolio arithmetic; no orders or assumed positions."""
import math
import numpy as np
import pandas as pd
from .scoring import finite


def position_limit(equity, entry, invalidation, risk_fraction=.005, single_stock_cap=.2, lot_size=100):
    values = [finite(v) for v in (equity, entry, invalidation, risk_fraction, single_stock_cap)]
    equity, entry, invalidation, risk_fraction, single_stock_cap = values
    if any(v is None for v in values) or not 0 < invalidation < entry or equity <= 0 or not 0 < risk_fraction <= 1 or not 0 < single_stock_cap <= 1:
        raise ValueError("需明确可投资权益、入场价及更低的失效价，风险和单股上限须在0到1之间")
    if type(lot_size) is not int or lot_size <= 0:
        raise ValueError("交易单位须为正整数；按证券实际规则填写")
    count = math.floor(min(equity*risk_fraction/(entry-invalidation), equity*single_stock_cap/entry)/lot_size)*lot_size
    return {"shares_ceiling": count, "position_value_cny": count*entry,
            "planned_loss_cny": count*(entry-invalidation), "risk_budget_cny": equity*risk_fraction,
            "basis": "失效价、交易单位均由用户确认；未保证可成交，跳空/停牌/跌停/T+1可能使实际损失更大", "orders_authorized": False}


def valuation_technical_matrix(valuation_label, technical_weak, material_advance=False):
    expensive = valuation_label in ("高估", "相对偏高", "高估（假设下）")
    if expensive and technical_weak:
        label = "利润保护优先观察"
    elif technical_weak:
        label = "核验技术及投资逻辑失效条件"
    elif expensive:
        label = "核对兑现预期与仓位上限"
    else:
        label = "按原趋势及投资逻辑观察"
    return {"label": label, "valuation_known": valuation_label not in (None, "无法判断", "估值待核验"),
            "partial_reduction_scenario": [1/3, .5] if technical_weak and material_advance else None,
            "orders_authorized": False, "basis": "估值与技术双条件研究；单独15分钟背离不定义技术转弱"}


def analyze_portfolio(positions, quotes, cash=None, histories=None, as_of=None, minimum_return_samples=40):
    rows, missing = [], []
    cash = finite(cash)
    if cash is None or cash < 0:
        missing.append("investable_cash")
    for position in positions:
        code = position["code"]
        quote = quotes.get(code, {})
        price, shares = finite(quote.get("price")), finite(position.get("shares"))
        valid = price is not None and price > 0 and shares is not None and shares > 0 and (as_of is None or quote.get("session") == as_of)
        value = price*shares if valid else None
        if not valid:
            missing.append(code + ".current_price_or_shares")
        cost = finite(position.get("cost"))
        rows.append({"code": code, "name": position.get("name"), "industry": quote.get("industry") or "行业未知",
                     "market_value_cny": value, "price": price, "quote_session": quote.get("session"),
                     "unrealized_profit_cny": (price-cost)*shares if valid and cost is not None and cost>0 else None})
    complete = not missing
    equity = sum(r["market_value_cny"] for r in rows) + cash if complete else None
    sectors = {}
    for row in rows:
        row["weight"] = row["market_value_cny"]/equity if complete and equity>0 else None
        if row["weight"] is not None:
            sectors[row["industry"]] = sectors.get(row["industry"], 0) + row["weight"]
    returns, correlations, risks = {}, [], []
    for row in rows:
        raw = (histories or {}).get(row["code"])
        if raw is None:
            continue
        series = pd.Series(raw, dtype=float).sort_index()
        series.index = pd.to_datetime(series.index)
        if series.index.duplicated().any() or (series <= 0).any() or not np.isfinite(series).all():
            risks.append(row["code"]+"历史价格无效")
            continue
        if as_of is not None:
            series = series.loc[:as_of]
        values = series.pct_change(fill_method=None).dropna()
        if len(values) >= minimum_return_samples:
            returns[row["code"]] = values
    codes = sorted(returns)
    for i, a in enumerate(codes):
        for b in codes[i+1:]:
            pair = pd.concat([returns[a], returns[b]], axis=1).dropna()
            correlation = pair.iloc[:, 0].corr(pair.iloc[:, 1]) if len(pair) >= minimum_return_samples else None
            correlations.append({"a": a, "b": b, "samples": len(pair), "correlation": finite(correlation)})
    return {"status": "complete" if complete else "partial", "as_of": as_of, "rows": rows,
            "investable_equity_cny": equity, "sector_exposure": sectors, "missing": missing,
            "largest_position_weight": max((r["weight"] for r in rows), default=0) if complete else None,
            "correlations": correlations, "history_errors": risks, "portfolio_drawdown": None,
            "drawdown_reason": "需带入出金及仓位变更的真实权益时间序列，不用当前持仓倒推历史组合",
            "protected_funds_policy": "只输入可投资现金；学费等保护资金不计入风险预算",
            "orders_authorized": False}


def compare_opportunity(holding, candidate, switching_cost_fraction=0.):
    if not 0 <= switching_cost_fraction < 1:
        raise ValueError("增量换仓成本需在0到1之间")
    def risk_reward(value, cost):
        price, base, bear = [finite(value.get(k)) for k in ("price", "base_value", "bear_value")]
        if any(x is None for x in (price, base, bear)) or not 0 <= bear < price < base:
            return None
        return (base-price-price*cost)/(price-bear+price*cost)
    old, new = risk_reward(holding, 0.), risk_reward(candidate, switching_cost_fraction)
    return {"status": "comparable" if old is not None and new is not None else "insufficient_evidence",
            "holding_scenario_reward_risk": old, "candidate_scenario_reward_risk": new,
            "switching_cost_fraction": switching_cost_fraction,
            "conclusion": "比较假设区间及换仓成本，不自动决定换仓", "orders_authorized": False}
