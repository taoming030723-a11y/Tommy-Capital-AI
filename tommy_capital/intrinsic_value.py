"""Evidence-gated scenario mathematics. Forecast assumptions are never market facts."""
import hashlib
import json
import math
import re
from datetime import date
from urllib.parse import urlparse
from .scoring import finite


def required(case, key, positive=False):
    value = finite(case.get(key))
    if isinstance(case.get(key), bool) or value is None or positive and value <= 0:
        raise ValueError("缺少有效假设：" + key)
    return value


def equity_bridge(enterprise_value, case):
    # All adjustments are explicit; an absent minority interest is not zero.
    adjustments = [required(case, k) for k in ("minority_interest_cny", "preferred_equity_cny", "nonoperating_assets_cny")]
    if any(v < 0 for v in adjustments):
        raise ValueError("少数股权、优先股与非经营资产需明确非负金额")
    return enterprise_value - required(case, "net_debt_cny") - adjustments[0] - adjustments[1] + adjustments[2]


def discounted_cash_flow(cashflows, discount_rate, terminal_growth):
    if (not isinstance(cashflows, list) or not 1 <= len(cashflows) <= 20 or
            any(isinstance(x, bool) or finite(x) is None for x in cashflows)):
        raise ValueError("现金流需1至20个明确年度预测，不允许缺失填零")
    rate, growth = finite(discount_rate), finite(terminal_growth)
    if rate is None or growth is None or not -1 < growth < rate < 1 or rate <= 0 or cashflows[-1] <= 0:
        raise ValueError("折现率须为正并高于永续增长率，末期现金流须为正")
    pv = sum(float(cf) / (1 + rate) ** year for year, cf in enumerate(cashflows, 1))
    terminal = cashflows[-1] * (1 + growth) / (rate - growth) / (1 + rate) ** len(cashflows)
    return {"value_cny": pv + terminal, "explicit_pv_cny": pv, "terminal_pv_cny": terminal,
            "terminal_share": terminal / (pv + terminal) if pv + terminal > 0 else None}


def scenario_value(method, case):
    details = {}
    if method == "normalized_profit":
        value = required(case, "normalized_profit_cny", True) * required(case, "fair_pe", True)
    elif method in ("fcff_dcf", "fcfe_dcf"):
        field, discount = ("fcff_cny", "wacc") if method == "fcff_dcf" else ("fcfe_cny", "cost_of_equity")
        details = discounted_cash_flow(case.get(field), required(case, discount, True), required(case, "terminal_growth"))
        value = equity_bridge(details["value_cny"], case) if method == "fcff_dcf" else details["value_cny"]
        if method == "fcfe_dcf" and any(k in case for k in ("wacc", "net_debt_cny")):
            raise ValueError("FCFE必须匹配股权成本，不再扣净债务")
    elif method == "ev_sales":
        value = equity_bridge(required(case, "revenue_cny", True) * required(case, "ev_sales", True), case)
    elif method == "sotp":
        segments = case.get("segments", [])
        if (not segments or len({s.get("name") for s in segments}) != len(segments)
                or any(not s.get("name") or s.get("basis") != "enterprise_value" for s in segments)):
            raise ValueError("SOTP需不同分部的明确企业价值，禁止混用股权价值或重复分部")
        value = equity_bridge(sum(required(s, "value_cny") for s in segments), case)
    elif method == "pb_roe":
        roe, growth, cost = required(case, "normalized_roe"), required(case, "terminal_growth"), required(case, "cost_of_equity", True)
        if not growth < cost < 1 or roe <= growth:
            raise ValueError("PB-ROE稳定期需ROE和股权成本均高于增长率")
        value = required(case, "common_book_equity_cny", True) * (roe - growth) / (cost - growth)
    else:
        raise ValueError("未知内在价值方法")
    if not math.isfinite(value):
        raise ValueError("估值计算不是有限数值")
    return {"equity_value_cny": max(0., value), "negative_equity_floored_at_zero": value < 0, **details}


def reverse_valuation(market_cap, normalized_profit, fair_pe, years):
    cap, profit, pe, horizon = map(finite, (market_cap, normalized_profit, fair_pe, years))
    if any(v is None or v <= 0 for v in (cap, profit, pe, horizon)):
        return {"status": "unavailable", "reason": "反向估值需要正的正常化利润、合理PE、实际市值和年限；不对负利润计算CAGR"}
    required_profit = cap / pe
    return {"status": "hypothesis", "required_profit_cny": required_profit,
            "required_earnings_cagr": (required_profit / profit) ** (1 / horizon) - 1,
            "years": horizon, "fair_pe_assumption": pe, "basis": "市场要求的盈利假设，不是盈利预测"}


def evidence_valid(model, evidence, as_of, maximum_age_days=90):
    reasons = []
    try:
        model_date = date.fromisoformat(model["as_of"])
        if not 0 <= (as_of - model_date).days <= maximum_age_days:
            reasons.append("假设日期在未来或超过配置有效期")
    except (KeyError, ValueError, TypeError):
        reasons.append("缺少有效模型日期")
    if model.get("reviewed") is not True or not model.get("assumption_basis"):
        reasons.append("尚未人工复核估值假设及正常化依据")
    records = {}
    for record in evidence.get("records", []):
        try:
            valid = (record.get("verification") == "verified" and
                     urlparse(record["url"]).scheme == "https" and
                     re.fullmatch(r"[a-f0-9]{64}", record.get("sha256", "")) and
                     date.fromisoformat(record["published_at"]) <= as_of)
            if valid:
                records[record["id"]] = record
        except (KeyError, ValueError, TypeError):
            pass
    ids = model.get("source_ids", [])
    if not ids or not set(ids) <= set(records):
        reasons.append("缺少已抓取核对、带日期及SHA256的原始估值依据")
    elif not any(records[i].get("field") == "valuation_basis" for i in ids):
        reasons.append("估值必须有专门valuation_basis证据，不能仅用规模交付或产品上市事实替代盈利/现金流假设依据")
    return reasons, [records[i] for i in ids if i in records]


def evaluate_intrinsic(row, models, evidence, session, policy=None):
    policy = {"minimum_margin": .2, "overvalue_ratio": 1.2, "maximum_model_age_days": 90,
              "method_disagreement_ratio": 1.3, **(policy or {})}
    if not (0 <= policy["minimum_margin"] < 1 and policy["overvalue_ratio"] > 1
            and policy["maximum_model_age_days"] > 0 and policy["method_disagreement_ratio"] > 1):
        raise ValueError("独立估值研究配置无效")
    price, cap = finite(row.get("price")), finite(row.get("market_cap"))
    result = {"version": "intrinsic_research_v1", "code": row.get("code"), "session": session,
              "price_judgment": "无法判断", "status": "unavailable", "models": [], "reasons": [],
              "data_complete": False, "model_confidence": "insufficient_evidence", "influences_ranking": False}
    if price is None or cap is None or min(price, cap) <= 0:
        result["reasons"] = ["当前价格或实际总市值缺失，不能计算每股价值"]
        return result
    shares = cap / price
    as_of = date.fromisoformat(session)
    for model in models or []:
        reasons, sources = evidence_valid(model, evidence or {}, as_of, policy["maximum_model_age_days"])
        item = {"method": model.get("method"), "status": "unavailable", "reasons": reasons,
                "sources": sources, "assumption_basis": model.get("assumption_basis"), "scenarios": {}}
        try:
            if reasons:
                raise ValueError("；".join(reasons))
            from .scoring import financial_sector
            if financial_sector(row) and model.get("method") != "pb_roe":
                raise ValueError("金融企业需专门PB-ROE及资产质量研究，不套普通企业现金流模型")
            for label in ("bear", "base", "bull"):
                case = model.get("scenarios", {}).get(label)
                if not isinstance(case, dict):
                    raise ValueError("三情景输入未齐")
                values = scenario_value(model["method"], case)
                item["scenarios"][label] = {**values, "per_share": values["equity_value_cny"] / shares}
            bear, base, bull = [item["scenarios"][k]["per_share"] for k in ("bear", "base", "bull")]
            if not 0 <= bear <= base <= bull or base <= 0:
                raise ValueError("三情景价值必须悲观≤中性≤乐观，且中性为正")
            item.update(status="complete", safety_margin=1-price/base, upside_to_base=base/price-1,
                        bear_downside=max(0., 1-bear/price), assumptions=model)
            if model["method"] == "normalized_profit":
                case = model["scenarios"]["base"]
                item["reverse"] = reverse_valuation(cap, model.get("current_normalized_profit_cny"), case["fair_pe"], model.get("reverse_years", 3))
        except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            if str(exc) not in item["reasons"]:
                item["reasons"].append(str(exc))
        result["models"].append(item)
    completed = [m for m in result["models"] if m["status"] == "complete"]
    if not completed:
        result["reasons"] = ["没有完整、已核验依据的三情景模型；历史PE参考不冒充内在价值"]
        return result
    primary = completed[0]
    base = primary["scenarios"]["base"]["per_share"]
    result.update(status="complete", data_complete=True, primary_method=primary["method"],
                  model_confidence="multi_model_cross_check" if len(completed)>1 else "reviewed_scenarios",
                  scenario_range=[primary["scenarios"]["bear"]["per_share"], primary["scenarios"]["bull"]["per_share"]],
                  safety_margin=primary["safety_margin"], bear_downside=primary["bear_downside"],
                  price_judgment="低估（假设下）" if primary["safety_margin"] >= policy["minimum_margin"] else
                    "高估（假设下）" if price/base >= policy["overvalue_ratio"] else "合理区间（假设下）",
                  model_disagreement=len(completed)>1 and max(m["scenarios"]["base"]["per_share"] for m in completed)/min(m["scenarios"]["base"]["per_share"] for m in completed)>policy["method_disagreement_ratio"],
                  basis="明确预测假设下的价值区间，不是统计置信区间；原始证据核验不代表预测已兑现")
    return result
