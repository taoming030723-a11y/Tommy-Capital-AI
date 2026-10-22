"""Financial research facts with explicit missingness; candidate V2 stays offline."""
from .scoring import finite, financial_sector

V2_WEIGHTS = {"capital_quality": 12, "earnings_cash_quality": 12,
              "intrinsic_margin": 14, "growth_inflection": 8, "balance_safety": 4}


def ratio(a, b):
    a, b = finite(a), finite(b)
    return a / b if a is not None and b is not None and b > 0 else None


def analyze_quality(row):
    number = lambda key: finite(row.get(key))
    nopat, opening, closing = (number(k) for k in ("nopat_ttm", "invested_capital_open_cny", "invested_capital_close_cny"))
    average = (opening + closing) / 2 if opening is not None and closing is not None and min(opening, closing) > 0 else None
    roic = ratio(nopat, average)
    cfo, capex, maintenance = (number(k) for k in ("cfo_ttm", "capex_ttm_cny", "maintenance_capex_ttm_cny"))
    fcf = cfo - capex if cfo is not None and capex is not None and capex >= 0 else None
    owner_cash = cfo - maintenance if cfo is not None and maintenance is not None and maintenance >= 0 else None
    debt, cash = number("interest_bearing_debt_cny"), number("cash_cny")
    net_debt = debt - cash if debt is not None and cash is not None and min(debt, cash) >= 0 else None
    financial = financial_sector(row)
    values = {"roic": None if financial else roic, "free_cash_flow_cny": None if financial else fcf,
              "cash_after_maintenance_capex_cny": None if financial else owner_cash,
              "cash_conversion": None if financial else ratio(cfo, number("profit_ttm")),
              "core_profit_share": ratio(number("core_profit_ttm"), number("profit_ttm")),
              "net_debt_cny": net_debt, "fcf_yield": None if financial else ratio(fcf, number("market_cap")),
              "receivables_to_revenue": ratio(number("receivables_cny"), number("revenue_ttm")),
              "inventory_to_revenue": ratio(number("inventory_cny"), number("revenue_ttm"))}
    return {"status": "complete" if all(v is not None for v in values.values()) else "partial",
            "metrics": values, "missing": [k for k, v in values.items() if v is None],
            "financial_sector": financial, "moat": "unknown", "incremental_roic": "unknown",
            "basis": "金额均为人民币总额；ROIC=实际NOPAT/期初期末平均投入资本；FCF=经营现金流−资本开支；维持性开支另列，不用CFO冒充自由现金流",
            "influences_ranking": False}


def candidate_v2_score(components):
    """Caller supplies reviewed component scores; absence is not zero or reweighting."""
    parts, missing = {}, []
    for name, maximum in V2_WEIGHTS.items():
        score = finite(components.get(name))
        if isinstance(components.get(name), bool) or score is None:
            missing.append(name)
        elif not 0 <= score <= maximum:
            raise ValueError("V2分项超出范围：" + name)
        else:
            parts[name] = score
    return {"version": "fundamental_v2_research_only", "weights": V2_WEIGHTS,
            "score": None if missing else sum(parts.values()), "parts": parts, "missing": missing,
            "production_enabled": False, "validation_status": "unvalidated_out_of_sample"}
