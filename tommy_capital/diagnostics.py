"""Separate market coverage, evidence, valuation and AI status without hiding gaps."""
from collections import Counter
from .coverage import is_history_shortfall
from .scoring import ranking_eligible, ranked_top20


def ranking_funnel(report):
    """Missing inputs are neither a low score nor a rejected investment thesis."""
    pool = report.get("rankings", [])
    pending = [{"code": row["code"], "name": row.get("name"),
                "missing_inputs": list(row.get("score_missing_inputs", []))}
               for row in pool if not ranking_eligible(row)]
    return {"entry_qualified": len(pool), "score_complete": sum(ranking_eligible(row) for row in pool),
            "top20_displayed": len(ranked_top20(pool)), "score_data_pending": len(pending),
            "pending_rows": pending,
            "failed_financial_batches": [str(e).split(": ", 1)[0] for e in report.get("errors", [])
                                         if str(e).startswith(("财务质量 ", "财报 "))]}


def failure_category(message):
    text = str(message)
    if is_history_shortfall(text):
        return "history_shortfall", False
    if any(x in text for x in ("最新日线", "最新K线", "未覆盖", "行情日期", "日期滞后")):
        return "stale_session", True
    if any(x in text for x in ("前复权因子", "复权价格不一致", "价格锚点", "收盘价不一致")):
        return "adjustment_validation", True
    if any(x in text for x in ("三路日线", "HTTP", "超时", "连接", "qfqday", "不可用")):
        return "source_unavailable", True
    return "validation_or_unknown", True


def report_diagnostics(report):
    gaps = [g for g in report.get("data_gaps", []) if not g.get("resolved")]
    queue, counts = [], Counter()
    for gap in gaps:
        category, retry = failure_category("\n".join(gap.get("attempt_errors", [])))
        counts[category] += 1
        queue.append({"code": gap["code"], "name": gap.get("name"), "category": category,
                      "retryable": retry, "attempts": len(gap.get("attempt_errors", [])),
                      "last_error": str(gap.get("attempt_errors", [""])[-1])[-1500:],
                      "next_action": "等待真实历史积累，不缩短历史门槛" if not retry else "下一次定向重试，保留日期/价格/复权校验"})
    coverage = report.get("coverage", {})
    valuations = list((report.get("valuations") or {}).values())
    reviews = (report.get("technical_reviews") or {}).get("rows", [])
    ai = Counter((r.get("ai") or {}).get("status", "not_requested") for r in reviews)
    valuation_gaps = Counter("financial_quality_missing" if any("扣非" in s or "现金流" in s for s in v.get("reasons", [])) else
                             "history_or_price_or_source" for v in valuations if v.get("status") not in ("complete", "not_applicable"))
    return {"schema": 1, "session": report.get("session"), "scan_complete": report.get("scan_complete", False),
            "ranking_funnel": ranking_funnel(report),
            "market": {"requested": coverage.get("technical_requested", 0), "verified": coverage.get("technical_completed", 0),
                       "missing": len(gaps), "failure_categories": dict(counts)},
            "financial": {"available": coverage.get("financial_data_available", 0),
                          "quality_statement_available": coverage.get("quality_statement_available", 0),
                          "leading_evidence_missing": coverage.get("leading_evidence_missing", 0)},
            "valuation": {"requested": len(valuations), "completed": sum(v.get("status") == "complete" for v in valuations),
                          "incomplete": sum(v.get("status") not in ("complete", "not_applicable") for v in valuations),
                          "missing_categories": dict(valuation_gaps)},
            "ai": {"counts": dict(ai), "completed": ai["complete"],
                   "mode": "model_explanations" if ai["complete"] else "program_rules_only",
                   "configuration": "not_configured" if ai["not_configured"] else "unknown_or_not_checked"},
            "retry_queue": queue, "influences_ranking": False}
