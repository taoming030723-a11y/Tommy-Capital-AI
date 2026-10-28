"""Bounded financial-only recovery and explicit publication degradation.

No financial fallback values, security selection, scoring or tracking mutations.
"""
import copy
import argparse
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .data import DataError, SHANGHAI, normalize_finance, merge_finance_quality, latest_finance

VERSION = "financial_integrity_v1"
CONFIG_PATH = Path(__file__).resolve().parents[1]/"financial-integrity-config.json"
FINANCIAL_PREFIXES = ("财报 ", "财务质量 ")
QUALITY_INPUTS = ("core_profit_ttm", "cfo_ttm", "core_profit_ytd", "cfo_ytd", "previous_same_core_profit_ytd", "previous_same_cfo_ytd")


def quality_input_count(frame):
    values = frame.reindex(columns=QUALITY_INPUTS).apply(pd.to_numeric, errors="coerce")
    return int(values.replace([float("inf"), -float("inf")], float("nan")).notna().all(axis=1).sum())


def validate_policy(policy):
    if type(policy.get("schema")) is not int or policy["schema"] != 1:
        raise DataError("财报保护配置schema无效")
    for key, lower, upper in (("retry_passes", 1, 2), ("workers", 1, 2), ("retry_budget_seconds", 10, 300), ("reference_max_age_days", 1, 31)):
        if type(policy.get(key)) is not int or not lower <= policy[key] <= upper:
            raise DataError("财报保护配置超出有界重试/并发范围："+key)
    for key in ("minimum_quality_coverage", "minimum_reference_coverage", "batch_count_drop_fraction", "coverage_drop_percentage_points"):
        value = policy.get(key)
        if type(value) not in (float, int) or not 0 < value < (100 if key.endswith("points") else 1):
            raise DataError("财报保护阈值无效："+key)
    if not .8 <= policy["minimum_quality_coverage"] <= policy["minimum_reference_coverage"]:
        raise DataError("财报保护最低覆盖不得低于80%，参考覆盖不得更低")
    return policy


def load_policy(path=CONFIG_PATH):
    try:
        return validate_policy(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise DataError("财报保护配置不可用："+str(exc)) from exc


def reference_report(results_directory, session, policy):
    """Only an older, published closing report with healthy financial coverage.

    A degraded day cannot silently become the next day's normal baseline.
    The reference is a coverage comparison, never a financial-data fallback.
    """
    session = date.fromisoformat(str(session))
    for file in sorted(Path(results_directory).glob("*/closing-summary.json"), reverse=True):
        try:
            day = date.fromisoformat(file.parent.name)
            if not 0 < (session-day).days <= policy["reference_max_age_days"]:
                continue
            raw = file.read_bytes(); summary = json.loads(raw)
            if (summary.get("session") != day.isoformat() or summary.get("scan_phase") != "closing" or
                    summary.get("scan_type") != "after_close" or summary.get("status") not in ("complete", "partial") or
                    summary.get("scan_complete") is not (summary.get("status") == "complete") or
                    summary.get("model_review") or summary.get("selection_rule") != "dual_route_monthly_recovery"):
                continue
            start, finish = [datetime.fromisoformat(summary[k]) for k in ("generated_at", "finished_at")]
            if (start.tzinfo is None or finish.tzinfo is None or start > finish or
                    any(t.astimezone(SHANGHAI).date() != day for t in (start, finish))):
                continue
            coverage = summary["coverage"]
            total, available = coverage["universe"], coverage["quality_statement_available"]
            if type(total) is not int or type(available) is not int or not 0 <= available <= total or total == 0:
                continue
            if available/total < policy["minimum_reference_coverage"]:
                continue
            if any(str(e).startswith(FINANCIAL_PREFIXES) for e in summary.get("errors", [])):
                continue
            if (summary.get("financial_integrity") or {}).get("status") == "degraded":
                continue
            return {"session": day.isoformat(), "universe": total, "quality_statement_available": available,
                    "quality_ratio": available/total, "sha256": hashlib.sha256(raw).hexdigest(),
                    "summary_path": f"results/{day}/closing-summary.json", "report_path": f"results/{day}/closing-report.md",
                    "quality_period_counts": coverage.get("quality_period_counts", {}),
                    "financial_period_counts": coverage.get("financial_period_counts", {})}
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
            continue
    return None


def coverage_issues(available, total, financial_counts, quality_counts, failed_keys, reference, policy, ttm_available=None):
    issues = []
    for key in sorted(failed_keys):
        issues.append({"kind": "failed_batch", "key": key, "reason": "财报批次获取/校验未完成："+key})
    ratio = available/total if total else 0.
    if not total or ratio < policy["minimum_quality_coverage"]:
        issues.append({"kind": "quality_coverage_low", "reason": f"财报质量可用{available}/{total}，低于保护阈值{policy['minimum_quality_coverage']:.0%}"})
    if ttm_available is not None and (not total or ttm_available/total < policy["minimum_quality_coverage"]):
        issues.append({"kind": "ttm_inputs_incomplete", "reason": f"扣非/经营现金流当前、TTM及同比输入可用{ttm_available}/{total}，不足以完整评分"})
    if reference:
        drop = (reference["quality_ratio"]-ratio)*100
        if drop+1e-9 >= policy["coverage_drop_percentage_points"]:
            issues.append({"kind": "quality_coverage_drop", "reason": f"较{reference['session']}财报质量覆盖下降{drop:.2f}个百分点"})
        for category, counts, old in (("financial", financial_counts, reference["financial_period_counts"]),
                                      ("quality", quality_counts, reference["quality_period_counts"])):
            for key, observed in counts.items():
                expected = old.get(key)
                if type(expected) is int and expected > 0 and observed < expected*(1-policy["batch_count_drop_fraction"]):
                    request_key = "financial:"+key if category == "financial" else key
                    issues.append({"kind": "batch_count_regression", "key": request_key,
                                   "reason": f"{request_key}可用行数{observed}，低于最近已核验的{expected}行"})
    return issues


def _assemble(frames, today):
    primary = [f for (kind, _), f in frames.items() if kind == "financial" and not f.empty]
    if not primary:
        raise DataError("没有可用已公告财报，停止排名")
    quality = {kind: [f for (k, _), f in frames.items() if k == kind and not f.empty] for kind in ("income", "cashflow")}
    return merge_finance_quality(pd.concat(primary, ignore_index=True), quality, today)


def load_financial_data(provider, periods, today, quoted_codes, reference=None, policy=None):
    """Recover only failed/regressed batches before any stock technical scan.

    Sources must still pass all original statement/version/announcement/TTM
    validation. A fresh attempt bypasses both Provider and nested page caches.
    """
    policy = validate_policy(copy.deepcopy(policy)) if policy is not None else load_policy()
    periods = sorted(set(periods), reverse=True)
    if not periods or any(not isinstance(p, date) or p >= today for p in periods):
        raise DataError("财报请求期必须为当时已经结束的报告期")
    requests = [(kind, p) for kind in ("financial", "income", "cashflow") for p in periods]
    frames, failures, records = {}, {}, {}
    recovery_deadline = None

    def fetch(request, fresh):
        kind, period = request
        kwargs = {"date": period.strftime("%Y%m%d")}
        function = "finance_em_named" if kind == "financial" else "finance_quality_em"
        kwargs.update({"schema_version": 2} if kind == "financial" else {"statement": kind})
        if fresh:
            kwargs["force_refresh"] = True
            remaining = recovery_deadline-time.monotonic()
            if remaining <= 0:
                raise DataError("财报定向恢复总时间预算耗尽")
            kwargs["budget_seconds"] = remaining
        ttl = 900 if kind != "financial" or period == periods[0] else 86400
        raw = provider.fetch(function, ttl=ttl, allow_empty=True, **kwargs)
        return normalize_finance(raw, period, today) if kind == "financial" and not raw.empty else raw

    def collect(pending, fresh):
        with ThreadPoolExecutor(max_workers=policy["workers"]) as executor:
            futures = {executor.submit(fetch, request, fresh): request for request in pending}
            for future in as_completed(futures):
                request = futures[future]; kind, period = request
                key = kind+":"+period.isoformat()
                record = records.setdefault(key, {"kind": kind, "period": period.isoformat(), "attempts": []})
                attempt = {"forced_refresh": fresh, "checked_at": datetime.now(SHANGHAI).isoformat()}
                try:
                    frame = future.result()
                    frames[request] = frame
                    failures.pop(request, None)
                    attempt.update(status="received", rows=len(frame))
                except (DataError, ValueError) as exc:
                    # Do not retain an older successful response after a failed
                    # refresh; otherwise a new error could be hidden by old data.
                    frames.pop(request, None)
                    failures[request] = str(exc)
                    attempt.update(status="unavailable", error=str(exc)[-1500:])
                record["attempts"].append(attempt)

    # Keep the existing source concurrency limits and primary/quality sequence.
    collect([r for r in requests if r[0] == "financial"], False)
    collect([r for r in requests if r[0] != "financial"], False)
    quote_set = set(quoted_codes)
    initial_issues = []
    retry_rounds = 0
    recovery_deadline = time.monotonic()+policy["retry_budget_seconds"]
    for round_number in range(policy["retry_passes"]+1):
        history = _assemble(frames, today) if any(k == "financial" and not f.empty for (k, _), f in frames.items()) else None
        if history is None and not failures:
            raise DataError("没有可用已公告财报，源明确返回空表，停止排名")
        finance = latest_finance(history) if history is not None else pd.DataFrame(columns=["code", "period", "quality_statement_matched"])
        selected = finance[finance.code.isin(quote_set)]
        available = int(selected.get("quality_statement_matched", pd.Series(False, index=selected.index)).eq(True).sum())
        ttm_available = quality_input_count(selected)
        financial_counts = {p.isoformat(): len(f) for (kind, p), f in frames.items() if kind == "financial"}
        quality_counts = {kind+":"+p.isoformat(): len(f) for (kind, p), f in frames.items() if kind != "financial"}
        failed_keys = [kind+":"+p.isoformat() for kind, p in failures]
        issues = coverage_issues(available, len(quote_set), financial_counts, quality_counts, failed_keys, reference, policy, ttm_available)
        if round_number == 0:
            initial_issues = copy.deepcopy(issues)
        if not issues or round_number == policy["retry_passes"] or time.monotonic() >= recovery_deadline:
            break
        target_keys = set(failed_keys) | {issue["key"] for issue in issues if issue["kind"] == "batch_count_regression"}
        if not target_keys:
            # A version mismatch can be coherent at HTTP level. Refresh only
            # the report periods whose latest public statements do not match.
            # Include historical bridge periods: latest income may match while
            # a prior same-quarter cashflow/earnings statement is still absent.
            matched = history.get("quality_statement_matched", pd.Series(False, index=history.index)).eq(True)
            complete = history.reindex(columns=["core_profit_ytd", "cfo_ytd"]).notna().all(axis=1)
            missing = history[history.code.isin(quote_set) & ~(matched & complete)]
            for p in pd.to_datetime(missing.period, errors="coerce").dropna().dt.date.unique():
                target_keys.update(kind+":"+p.isoformat() for kind in ("financial", "income", "cashflow"))
        pending = [r for r in requests if r[0]+":"+r[1].isoformat() in target_keys]
        if not pending:
            break
        retry_rounds += 1
        collect(pending, True)
    errors = [("财报 " if kind == "financial" else "财务质量 "+kind+" ")+p.isoformat()+": "+error
              for (kind, p), error in sorted(failures.items())]
    if history is None:
        raise DataError("没有可用已公告财报，定向重试后停止排名："+"；".join(errors))
    refetched = sum(len(r["attempts"]) > 1 and r["attempts"][-1]["status"] == "received" for r in records.values())
    return history, {"version": VERSION, "policy": policy, "reference": copy.deepcopy(reference),
                     "financial_period_counts": financial_counts, "quality_period_counts": quality_counts,
                     "quality_ttm_available": ttm_available,
                     "initial_issues": initial_issues, "remaining_issues": issues, "retry_rounds": retry_rounds,
                     "retry_budget_exhausted": bool(issues and time.monotonic() >= recovery_deadline),
                     "batches": records, "errors": errors,
                     "successful_refetch_batch_count": refetched, "recovered_batch_count": refetched if not issues else 0,
                     "basis": "财报批次定向恢复；不重扫全市场、不沿用旧财报填空；原字段、报告期及公告/TTM校验保留"}


def assessment(report, collection):
    policy = validate_policy(collection["policy"])
    c = report["coverage"]
    available, total = c.get("quality_statement_available"), c.get("universe")
    if type(available) is not int or type(total) is not int or not 0 <= available <= total or not total:
        raise DataError("财报保护缺少可信覆盖计数")
    from .diagnostics import ranking_funnel
    funnel = ranking_funnel(report)
    failed_keys = [key for key, item in collection["batches"].items() if item["attempts"][-1]["status"] != "received"]
    ttm_available = c.get("quality_ttm_available")
    if type(ttm_available) is not int or not 0 <= ttm_available <= total:
        raise DataError("财报保护缺少扣非/现金流输入覆盖计数")
    issues = coverage_issues(available, total, c.get("financial_period_counts", {}), c.get("quality_period_counts", {}),
                             failed_keys, collection.get("reference"), policy, ttm_available)
    pending = funnel["score_data_pending"]
    return {"version": VERSION, "status": "degraded" if issues else "limited" if pending else "healthy",
            "publication_mode": "diagnostic_partial" if issues else "verified_subset" if pending else "normal",
            "quality_ratio": available/total, "quality_statement_available": available, "quality_ttm_available": ttm_available, "universe": total,
            "alerts": issues, "score_data_pending": pending, "reference": collection.get("reference"),
            "retry_rounds": collection["retry_rounds"], "recovered_batch_count": collection["recovered_batch_count"],
            "successful_refetch_batch_count": collection["successful_refetch_batch_count"],
            "collection": collection, "influences_scoring": False, "orders_authorized": False}


def attach_integrity(report, collection):
    guard = assessment(report, collection)
    report["financial_integrity_required"] = VERSION
    report["financial_integrity"] = guard
    if guard["status"] == "degraded":
        message = "财报数据异常："+"；".join(i["reason"] for i in guard["alerts"])+"；仅发布诊断与已核验部分，不能解释为全市场合格股票减少"
        if message not in report["errors"]:
            report["errors"].append(message)
    if guard["status"] in ("degraded", "limited"):
        report.update(status="partial", scan_complete=False, ranking_complete=False)
        reason = "财报覆盖异常，榜单仅为数据齐全的已核验部分" if guard["status"] == "degraded" else "入口股仍有评分数据待齐"
        if reason not in report["ranking_incomplete_reasons"]:
            report["ranking_incomplete_reasons"].append(reason)
    return guard


def validate_integrity(report, markdown=None):
    """Publication/audit invariant; cannot downgrade a data error to normal."""
    required = report.get("financial_integrity_required")
    guard = report.get("financial_integrity")
    if report.get("status") == "failed":
        if report.get("scan_complete") is not False or report.get("rankings"):
            raise ValueError("失败诊断不能携带正常候选或声明扫描完整")
        return
    if required is None and guard is None:
        return  # Existing historical reports retain their original contract.
    if required != VERSION or not isinstance(guard, dict) or guard.get("version") != VERSION:
        raise ValueError("财报保护标记/诊断缺失，拒绝作为正常结果发布")
    collection = guard["collection"]
    policy = validate_policy(collection["policy"])
    if (collection.get("version") != VERSION or type(collection.get("retry_rounds")) is not int or
            not 0 <= collection["retry_rounds"] <= policy["retry_passes"]):
        raise ValueError("财报恢复记录版本或次数无效")
    for key in ("financial_period_counts", "quality_period_counts"):
        if collection[key] != report["coverage"].get(key):
            raise ValueError("财报恢复记录与覆盖数量不一致")
    if collection["quality_ttm_available"] != report["coverage"].get("quality_ttm_available"):
        raise ValueError("财报恢复记录与扣非/现金流输入数量不一致")
    reference = collection.get("reference")
    if reference and not 0 < (date.fromisoformat(report["session"])-date.fromisoformat(reference["session"])).days <= policy["reference_max_age_days"]:
        raise ValueError("财报覆盖参考不属于此前有效日期")
    if reference:
        n, q = reference.get("universe"), reference.get("quality_statement_available")
        if (type(n) is not int or type(q) is not int or not 0 <= q <= n or not n or
                reference.get("quality_ratio") != q/n or q/n < policy["minimum_reference_coverage"] or
                not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", ""))) or
                reference.get("report_path") != f"results/{reference['session']}/closing-report.md"):
            raise ValueError("财报覆盖参考缺少可信数量、日期或来源摘要")
    if sorted(collection["errors"]) != sorted(str(e) for e in report["errors"] if str(e).startswith(FINANCIAL_PREFIXES)):
        raise ValueError("未完成财报批次与实际失败原因不一致")
    for batch in collection["batches"].values():
        attempts = batch["attempts"]
        if (not 1 <= len(attempts) <= policy["retry_passes"]+1 or
                attempts[0].get("forced_refresh") is not False or
                any(a.get("forced_refresh") is not True for a in attempts[1:])):
            raise ValueError("财报定向恢复未真正绕过缓存或超过次数上限")
    financial_counts, quality_counts, unavailable = {}, {}, set()
    refetched = 0
    for key, batch in collection["batches"].items():
        kind, period = batch["kind"], batch["period"]
        if kind not in ("financial", "income", "cashflow") or key != kind+":"+period:
            raise ValueError("财报批次身份无效")
        final = batch["attempts"][-1]
        if final["status"] == "received":
            if type(final.get("rows")) is not int or final["rows"] < 0:
                raise ValueError("财报批次实际行数无效")
            (financial_counts if kind == "financial" else quality_counts)[period if kind == "financial" else key] = final["rows"]
            refetched += int(len(batch["attempts"]) > 1)
        elif final["status"] == "unavailable":
            unavailable.add(("财报 " if kind == "financial" else "财务质量 "+kind+" ")+period)
        else:
            raise ValueError("财报批次结果无效")
    if financial_counts != collection["financial_period_counts"] or quality_counts != collection["quality_period_counts"]:
        raise ValueError("财报覆盖计数与各批实际结果不一致")
    if unavailable != {e.split(": ", 1)[0] for e in collection["errors"]}:
        raise ValueError("财报失败批次在诊断中被隐藏")
    if refetched != collection["successful_refetch_batch_count"]:
        raise ValueError("成功重取数量与实际批次不一致")
    expected = assessment(report, guard["collection"])
    if guard != expected:
        raise ValueError("财报保护状态与实际计数/缺失不一致")
    if guard["status"] != "healthy" and (report.get("status") != "partial" or report.get("scan_complete") is not False or report.get("ranking_complete") is not False):
        raise ValueError("财报缺失不能冒充完整扫描或完整排名")
    if guard["status"] == "degraded":
        if not any(str(e).startswith("财报数据异常：") for e in report.get("errors", [])):
            raise ValueError("财报异常未写入失败原因")
        if markdown is not None and "财报数据异常：本次仅为诊断与已核验部分" not in markdown:
            raise ValueError("财报异常未在中文报告显著提示")
    elif guard["status"] == "limited" and markdown is not None and "评分数据待齐：部分入口股未参与排名" not in markdown:
        raise ValueError("评分缺失未在中文报告显著提示")


def replay_summary(path, results_directory="results"):
    """Classify a genuine historical summary, without reconstructing new facts.

    Older summaries have no per-attempt recovery journal. They can test whether
    the new coverage alarm recognizes the incident, not whether the supplier
    has since recovered or whether a new production report passes all gates.
    """
    path = Path(path); raw = path.read_bytes(); summary = json.loads(raw)
    if summary.get("scan_phase") != "closing" or summary.get("scan_type") != "after_close" or summary.get("model_review"):
        raise ValueError("只读回放仅接受正式收盘机器摘要")
    policy = load_policy()
    reference = reference_report(results_directory, summary["session"], policy)
    coverage = summary["coverage"]
    failed = []
    for error in summary.get("errors", []):
        primary = re.match(r"^财报 (\d{4}-\d{2}-\d{2}):", str(error))
        quality = re.match(r"^财务质量 (income|cashflow) (\d{4}-\d{2}-\d{2}):", str(error))
        if primary: failed.append("financial:"+primary[1])
        elif quality: failed.append(quality[1]+":"+quality[2])
    entry, complete = summary["candidate_count"], summary["ranking_eligible_count"]
    if type(entry) is not int or type(complete) is not int or not 0 <= complete <= entry:
        raise ValueError("历史摘要入口/评分数量无效")
    alerts = coverage_issues(coverage["quality_statement_available"], coverage["universe"],
                             coverage["financial_period_counts"], coverage["quality_period_counts"], failed, reference,
                             policy, coverage.get("quality_ttm_available"))
    result = {"validation_kind": "read_only_historical_incident_not_new_market_scan", "version": VERSION,
              "session": summary["session"], "source_sha256": hashlib.sha256(raw).hexdigest(),
              "source_run_url": summary.get("workflow_run_url"), "reference": reference,
              "entry_qualified": entry, "score_complete": complete, "score_data_pending": entry-complete,
              "degradation_detected": bool(alerts), "alerts": alerts, "new_market_requests": 0,
              "new_financial_requests": 0, "historical_files_changed": False,
              "limitations": "只验证新保护是否识别已有真实故障；不证明数据源已恢复，不补造旧版缺失的TTM覆盖计数或恢复日志，不是可发布的新扫描。"}
    if hashlib.sha256(path.read_bytes()).hexdigest() != result["source_sha256"]:
        raise ValueError("历史输入在回放中被更改")
    return result


def main():
    parser = argparse.ArgumentParser(description="只读回放真实历史财报故障；不抓行情、不发布结果")
    parser.add_argument("--replay-summary", required=True)
    parser.add_argument("--results-directory", default="results")
    parser.add_argument("--out", default="reports/financial-guard-replay")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    protected = Path(args.results_directory).resolve()
    if out == protected or protected in out.parents:
        raise ValueError("历史回放不得写入正式results目录")
    result = replay_summary(args.replay_summary, args.results_directory)
    out.mkdir(parents=True, exist_ok=True)
    (out/"financial-guard-replay.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    lines = ["# 财报保护 · 真实历史故障只读回放", "", "**这不是新的市场扫描或供应商恢复证明。**", "",
             f"原报告日期：{result['session']}；原运行：{result['source_run_url']}", "",
             "| 入口合格 | 评分齐全 | 数据待齐 | 新保护识别异常 |", "| --- | --- | --- | --- |",
             f"| {result['entry_qualified']} | {result['score_complete']} | {result['score_data_pending']} | {result['degradation_detected']} |", "",
             *["- "+i["reason"] for i in result["alerts"]], "", result["limitations"], "",
             "原摘要SHA256："+result["source_sha256"]]
    (out/"financial-guard-replay.md").write_text("\n".join(lines))
    print(json.dumps({k: v for k, v in result.items() if k not in ("alerts", "reference")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
