"""Optional decision support built from already verified scan facts; no re-scoring."""
import argparse
import copy
import json
from pathlib import Path
from .business_classifier import classify_business
from .quality_analysis import analyze_quality, candidate_v2_score
from .intrinsic_value import evaluate_intrinsic
from .diagnostics import report_diagnostics
from .scoring import ranked_top20


def validate_public_research(value):
    forbidden = {"cost", "shares", "holdings", "position_basis", "profit_observation", "private_journal", "unrealized_profit_cny"}
    def inspect(item):
        if isinstance(item, dict):
            if forbidden.intersection(item):
                raise ValueError("公开企业研究含私人持仓字段，拒绝发布")
            for child in item.values():
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
    inspect(value)


def research_queue(report):
    rows = []
    for row in report.get("rankings", []) + report.get("observations", []):
        financial = row.get("financial_routes", {})
        if not financial.get("route_b_prequalified") or financial.get("route_b_passed"):
            continue
        path = row.get("technical", {}).get("monthly_recovery", {})
        monthly = path.get("route_b_path") is True
        core = row.get("quality_statement_matched") is True and financial.get("leading", {}).get("core_improving") is True
        rows.append({"code": row["code"], "name": row.get("name"), "monthly_path": monthly,
                     "core_financial_improvement": core, "priority": int(monthly)*2+int(core),
                     "basis": "已核验财报的改善线索，不冒充订单、催化或领先经营证据通关",
                     "missing_evidence": financial.get("route_b_reasons", []),
                     "status": "pending_evidence", "auto_qualified": False})
    return sorted(rows, key=lambda x: (-x["priority"], x["code"]))


def build_research(report, assumptions=None, policy=None):
    before = copy.deepcopy({k: report.get(k) for k in ("rankings", "coverage", "top10_history", "config", "scoring_version")})
    assumptions = assumptions or {}
    rows = {r["code"]: r for r in report.get("rankings", [])+report.get("observations", [])}
    codes = {r["code"] for r in ranked_top20(report.get("rankings", []))}
    codes.update(r["code"] for r in report.get("top10_tracking", {}).get("rows", []))
    studies = {}
    for code in sorted(codes):
        row = rows.get(code, {"code": code})
        company = assumptions.get(code, {})
        studies[code] = {"code": code, "name": row.get("name"), "classification": classify_business(row),
                         "quality": analyze_quality(row), "valuation": evaluate_intrinsic(row, company.get("models", []), row.get("leading_evidence", {}), report["session"], policy),
                         "candidate_fundamental_v2": candidate_v2_score({}),
                         "thesis_status": "待人工记录投资理由、可证伪条件与下一次复核日"}
    queue = research_queue(report)
    completed = [r for r in report.get("rankings", [])+report.get("observations", []) if r.get("technical")]
    result = {"version": "investment_decision_research_v1", "public": True, "session": report["session"], "rows": studies,
              "queue_count": len(queue), "research_queue": queue,
              "market_environment": {"weekly_trend_breadth": sum(r["technical"].get("weekly_trend") is True for r in completed)/len(completed) if completed else None,
                                     "sample_count": len(completed), "scope": "仅通过数据核验的股票，非全市场完整广度", "influences_ranking": False,
                                     "indices": (report.get("forward_evaluation") or {}).get("market_environment", {})},
              "intrinsic_completed": sum(r["valuation"]["status"] == "complete" for r in studies.values()),
              "assumptions": assumptions, "policy": policy, "influences_ranking": False, "orders_authorized": False}
    assert before == {k: report.get(k) for k in before}
    validate_public_research(result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="reports/latest")
    parser.add_argument("--assumptions", default="operations/investment-research.json")
    parser.add_argument("--policy", default="investment-config.json")
    args = parser.parse_args()
    root = Path(args.out)
    report = json.loads((root/"report.json").read_text())
    if report.get("status") not in ("complete", "partial"):
        return
    file = Path(args.assumptions)
    inputs = json.loads(file.read_text()) if file.exists() else {"schema": 1, "companies": {}}
    if inputs.get("schema") != 1 or not isinstance(inputs.get("companies"), dict):
        raise ValueError("研究假设格式无效")
    policy = json.loads(Path(args.policy).read_text())
    report["investment_research"] = build_research(report, inputs["companies"], policy)
    report["data_quality"] = report_diagnostics(report)
    from .cli import make_summary, write_outputs, clean
    write_outputs(root, report)
    (root/"summary.json").write_text(json.dumps(clean(make_summary(report)),ensure_ascii=False,indent=2,allow_nan=False))
    (root/"retry-queue.json").write_text(json.dumps(report["data_quality"]["retry_queue"],ensure_ascii=False,indent=2))
    (root/"research-queue.json").write_text(json.dumps(report["investment_research"]["research_queue"],ensure_ascii=False,indent=2))
    from .kd_research import freeze_kd
    kd_snapshot = freeze_kd(report)
    if kd_snapshot is not None:
        (root/"kd-observation-snapshot.json").write_text(json.dumps(kd_snapshot,ensure_ascii=False,indent=2,allow_nan=False))
    print(json.dumps({"research_stocks": len(report["investment_research"]["rows"]), "intrinsic_completed": report["investment_research"]["intrinsic_completed"], "pending_evidence":report["investment_research"]["queue_count"]}))


if __name__ == "__main__":
    main()
