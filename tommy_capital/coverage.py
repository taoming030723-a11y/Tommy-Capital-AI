"""Bounded selective recovery; each quoted security produces exactly one final result."""
from concurrent.futures import ThreadPoolExecutor, as_completed

from .data import DataError


def technical_results(rows, inspect, workers, retry_passes=1):
    pending = rows[:]
    failures = {}
    for attempt in range(retry_passes + 1):
        retry = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(inspect, row, attempt > 0): row for row in pending}
            for future in as_completed(futures):
                row = futures[future]
                code = row["code"]
                try:
                    card, warning = future.result()
                    yield row, card, warning, failures.get(code, [])
                except DataError as exc:
                    failures.setdefault(code, []).append(str(exc))
                    # Refreshing identical short history does not create older real bars.
                    historical = "日线不足250根" in str(exc) or "周/月线不足" in str(exc)
                    if attempt < retry_passes and not historical:
                        retry.append(row)
                    else:
                        yield row, None, str(exc), failures[code]
        pending = retry
        if not pending:
            break


def ranking_completeness(report):
    coverage = report.get("coverage", {})
    reasons = []
    if coverage.get("technical_requested", 0) != coverage.get("universe", 0) or coverage.get("unscanned", 0):
        reasons.append("未向全部报价股票请求日周月技术数据")
    if coverage.get("technical_completed", 0) != coverage.get("technical_requested", 0):
        reasons.append("部分日周月历史、最新日期或前复权价格尚未通过")
    if coverage.get("financial_data_available", 0) != coverage.get("universe", 0):
        reasons.append("部分股票最新已公告财报缺失")
    if coverage.get("leading_evidence_missing", 0):
        reasons.append("B路线领先业务、未来催化或三情景估值证据未齐")
    if coverage.get("score_inputs_completed", 0) != coverage.get("monthly_pool_count", 0):
        reasons.append("部分月线观察池的财务质量或六周期评分输入未齐")
    if report.get("errors"):
        reasons.append("仍存在数据源或历史检查错误，详情见审计附件")
    return not reasons and coverage.get("universe", 0) > 0, reasons
