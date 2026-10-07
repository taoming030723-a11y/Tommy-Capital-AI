"""Evidence-bound leading-business and scenario-valuation route.

Unavailable evidence is unknown, never False data or an automatic PE waiver.
The ledger is company-neutral: the same checks apply to every security code.
"""
import hashlib
import html
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def load_evidence(path, today, session=None):
    if path is None:
        return {}, {"companies_registered": 0, "sources_requested": 0, "sources_verified": 0,
                    "facts_verified": 0, "source_failures": [], "lineage": []}
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if raw.get("schema_version") != 1 or not isinstance(raw.get("companies"), dict):
        raise ValueError("领先拐点证据文件需为schema_version=1及companies代码字典")
    result, fetched = {}, {}
    stats = {"companies_registered": len(raw["companies"]), "sources_requested": 0,
             "sources_verified": 0, "facts_verified": 0, "source_failures": [], "lineage": []}
    for code, company in raw["companies"].items():
        if not re.fullmatch(r"\d{6}", code) or not isinstance(company, dict):
            raise ValueError("领先拐点证据的证券代码/对象格式错误")
        domains = company.get("official_domains", [])
        if not domains or any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]+\.[a-z]{2,}", d) for d in domains):
            raise ValueError(f"{code} 官方来源域名需显式登记")
        facts, records, verified_ids = {}, [], set()
        for evidence in company.get("facts", []):
            item = {**evidence, "verification": "unavailable"}
            try:
                url = evidence["url"]
                parsed = urlparse(url)
                host = parsed.hostname or ""
                if (parsed.scheme != "https" or parsed.username or parsed.password or
                        not any(host == d or host.endswith("." + d) for d in domains)):
                    raise ValueError("证据不是登记的官方HTTPS来源")
                published = datetime.fromisoformat(evidence["published_at"]).date()
                if not 0 <= (today - published).days <= 225:
                    raise ValueError("证据发布日期在未来或超过225天")
                assertions = evidence.get("match_all", [])
                if not assertions or any(not isinstance(s, str) or len(s) < 4 for s in assertions):
                    raise ValueError("缺少原始来源核对文本")
                if url not in fetched:
                    stats["sources_requested"] += 1
                    try:
                        response = (session or requests).get(url, timeout=(10, 25),
                            headers={"User-Agent": "TommyCapital-Research/2.0"}, allow_redirects=False)
                        response.raise_for_status()
                        if len(response.content) > 3_000_000:
                            raise ValueError("来源超过3MB，需另行提供经过核对的结构化证据")
                        response.encoding = response.apparent_encoding or "utf-8"
                        plain = re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", " ", response.text)))
                        fetched[url] = (plain, hashlib.sha256(response.content).hexdigest(), None)
                        stats["sources_verified"] += 1
                        stats["lineage"].append({"function": "official_leading_evidence", "url": url,
                            "sha256": fetched[url][1], "fetched_at": datetime.now().astimezone().isoformat()})
                    except (requests.RequestException, ValueError) as exc:
                        fetched[url] = (None, None, str(exc))
                plain, digest, error = fetched[url]
                if error:
                    raise ValueError(error)
                if not all(re.sub(r"\s+", "", s) in plain for s in assertions):
                    raise ValueError("原始页面未匹配登记的事实文本，不能使用旧快照通关")
                if not (published.isoformat() in plain or published.strftime("%Y%m%d") in parsed.path or
                        published.strftime("%Y年%m月%d日") in plain):
                    raise ValueError("无法核对来源日期")
                field = evidence["field"]
                if field not in {"key_product_sales_growth_pct", "order_growth_pct", "new_product_mass_delivery",
                                 "core_business_trend_improving", "future_catalyst", "valuation_basis"}:
                    raise ValueError("未知领先证据字段")
                if field in facts:
                    raise ValueError("同一字段证据重复，需明确合并口径")
                facts[field] = evidence["value"]
                verified_ids.add(evidence["id"])
                stats["facts_verified"] += 1
                item.update(verification="verified", sha256=digest)
            except (KeyError, ValueError, TypeError) as exc:
                item["error"] = str(exc)
                stats["source_failures"].append(f"{code} 领先证据 {evidence.get('id', '?')}：{exc}")
            records.append(item)
        result[code] = {"facts": facts, "records": records, "verified_ids": sorted(verified_ids),
                        "valuation_model": company.get("valuation_model"), "name": company.get("name")}
    return result, stats


def valuation_check(row, evidence, config, today):
    model = evidence.get("valuation_model")
    if not isinstance(model, dict):
        return {"status": "missing", "passed": False, "reasons": ["缺少已复核的远期盈利/SOTP/EV-Sales估值模型"]}
    reasons, values = [], {}
    method = model.get("method")
    asof = pd.to_datetime(model.get("as_of"), errors="coerce")
    if pd.isna(asof) or not 0 <= (today - asof.date()).days <= 90:
        reasons.append("估值模型日期缺失/在未来/超过90天")
    if model.get("reviewed") is not True or not model.get("assumption_basis"):
        reasons.append("正常化盈利/分部估值假设尚未复核")
    source_ids = model.get("source_ids", [])
    if not source_ids or not set(source_ids).issubset(evidence.get("verified_ids", [])):
        reasons.append("估值假设所依赖的原始经营证据未核对")
    year = model.get("forecast_year")
    if not isinstance(year, int) or not today.year <= year <= today.year + 2:
        reasons.append("正常化估值年份缺失/超过未来2年")
    for label in ["bear", "base", "bull"]:
        case = model.get("scenarios", {}).get(label, {})
        value = None
        if method == "normalized_forward_profit":
            profit, pe = finite(case.get("net_profit_cny")), finite(case.get("pe"))
            if profit is not None and profit > 0 and pe is not None and 0 < pe <= config["max_forward_pe"]:
                value = profit * pe
        elif method == "sotp":
            segments, debt = case.get("segments", []), finite(case.get("net_debt_cny"))
            components = [finite(s.get("enterprise_value_cny")) for s in segments if isinstance(s, dict)]
            if segments and len(components) == len(segments) and all(v is not None and v > 0 for v in components) and debt is not None:
                value = sum(components) - debt
        elif method == "ev_sales":
            sales, multiple, debt = finite(case.get("revenue_cny")), finite(case.get("ev_sales")), finite(case.get("net_debt_cny"))
            if sales is not None and sales > 0 and multiple is not None and 0 < multiple <= config["max_forward_ev_sales"] and debt is not None:
                value = sales * multiple - debt
        else:
            reasons.append("未知估值方法")
        if value is None or value <= 0:
            reasons.append(f"{label}情景估值数据缺失/不合理/超过倍数上限")
        else:
            values[label] = value
    market_cap = finite(row.get("market_cap"))
    margin, downside = None, None
    if len(values) == 3 and market_cap is not None and market_cap > 0:
        if not values["bear"] <= values["base"] <= values["bull"]:
            reasons.append("Bear/Base/Bull估值次序错误")
        margin = (1 - market_cap / values["base"]) * 100
        downside = max(0.0, (1 - values["bear"] / market_cap) * 100)
        if margin < config["min_forward_margin_pct"]:
            reasons.append("相对Base估值的安全边际不足")
        if downside > config["max_bear_downside_pct"]:
            reasons.append("Bear情景潜在跌幅超过配置上限")
        if method == "normalized_forward_profit":
            forward_pe = market_cap / model["scenarios"]["base"]["net_profit_cny"]
            if forward_pe > config["max_forward_pe"]:
                reasons.append("当前市值对应的Base远期PE过高")
    else:
        reasons.append("市值或三情景估值缺失")
    return {"status": "passed" if not reasons else "not_passed", "passed": not reasons,
            "method": method, "reasons": reasons, "scenario_equity_values_cny": values,
            "base_margin_pct": margin, "bear_downside_pct": downside,
            "model": model, "basis": "情景估值是有来源的研究假设，不是公司盈利承诺或目标收益"}


def leading_check(row, evidence, config, today):
    facts = evidence.get("facts", {})
    sales, orders = finite(facts.get("key_product_sales_growth_pct")), finite(facts.get("order_growth_pct"))
    indicator = ((sales is not None and sales > config["leading_min_business_growth_pct"]) or
                 (orders is not None and orders > config["leading_min_business_growth_pct"]) or
                 facts.get("new_product_mass_delivery") is True)
    previous, current = finite(row.get("previous_same_gross_margin")), finite(row.get("gross_margin"))
    core_proxy = previous is not None and current is not None and current > 0 and current > previous
    core = facts.get("core_business_trend_improving") is True or core_proxy
    catalyst = facts.get("future_catalyst")
    future = False
    if isinstance(catalyst, dict) and catalyst.get("description"):
        event = pd.to_datetime(catalyst.get("expected_date"), errors="coerce")
        future = not pd.isna(event) and today < event.date() <= (pd.Timestamp(today) + pd.DateOffset(months=6)).date()
    reasons = []
    if not indicator:
        reasons.append("缺少已核对的销量/订单增长>30%或规模交付证据")
    if not core:
        reasons.append("核心业务改善证据或毛利率同比改善代理未确认")
    if not future:
        reasons.append("未来6个月明确催化缺失/已发生/日期未核实")
    valuation = valuation_check(row, evidence, config, today)
    reasons += valuation["reasons"]
    return {"passed": not reasons, "status": "passed" if not reasons else "pending_evidence",
            "reasons": reasons, "leading_indicator_confirmed": indicator,
            "core_improving": core, "core_financial_proxy": core_proxy,
            "future_catalyst_confirmed": future, "catalyst": catalyst, "valuation": valuation,
            "records": evidence.get("records", []),
            "profit_label": "盈利尚未兑现" if (finite(row.get("profit_ytd")) or 0) <= 0 else "盈利为正，领先拐点路线"}
