"""Offline fault injection: no synthetic prices/financials are live evidence."""
import copy
import json
import time
import subprocess
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from tommy_capital.data import DataError, FINANCE_COLUMNS, Provider, SHANGHAI, latest_finance
from tommy_capital.financial_integrity import (VERSION, assessment, attach_integrity, coverage_issues,
                                              load_financial_data, load_policy, reference_report, validate_integrity)
from tommy_capital.reporting import render_markdown

PERIODS = [date(2026, 6, 30), date(2025, 12, 31), date(2025, 6, 30)]
TODAY = date(2026, 10, 28)
CODES = [f"600{i:03d}" for i in range(10)]


class RecordedFinancialProvider:
    """Received-shape fixtures with a predictable failure, no market requests."""
    def __init__(self, fail=None, persistent=False, mismatch=False):
        self.fail, self.persistent, self.mismatch = fail, persistent, mismatch
        self.calls = []

    def fetch(self, function, **kwargs):
        assert function in ("finance_em_named", "finance_quality_em"), "financial repair must not scan prices"
        kind = "financial" if function == "finance_em_named" else kwargs["statement"]
        period = pd.Timestamp(kwargs["date"]).date()
        fresh = kwargs.get("force_refresh", False)
        self.calls.append((kind, period, fresh, kwargs.get("budget_seconds")))
        if (kind, period) == self.fail and (self.persistent or not fresh):
            raise DataError("财务质量报表实际行数与源声明不符")
        net = {PERIODS[0]: 60., PERIODS[1]: 100., PERIODS[2]: 40.}[period]
        announced = pd.Timestamp(period)+pd.Timedelta(days=25)
        if kind == "financial":
            raw = {k: [1.]*10 for k in FINANCE_COLUMNS}
            raw.update({"股票代码": CODES, "股票简称": ["协议测试"]*10, "所处行业": ["协议测试行业"]*10,
                        "最新公告日期": [announced.isoformat()]*10, "净利润-净利润": [net]*10,
                        "营业总收入-营业总收入": [net*10]*10})
            return pd.DataFrame(raw)
        raw = {"code": CODES, "period": [pd.Timestamp(period)]*10, "quality_announced_at": [announced]*10}
        if kind == "income":
            raw.update(core_profit_ytd=[net*.9]*10, statement_profit_ytd=[net*2 if self.mismatch and period == PERIODS[0] and not fresh else net]*10,
                       statement_revenue_ytd=[net*10]*10)
        else:
            raw["cfo_ytd"] = [net*.8]*10
        return pd.DataFrame(raw)


def report_for(collection, complete=2, pending=7, available=0):
    from test_volume_tracking_report import closing
    report = closing("2026-10-28", complete)
    exemplar = copy.deepcopy(report["rankings"][0])
    for i in range(pending):
        row = copy.deepcopy(exemplar)
        row.update(code=f"000{i:03d}", name="资料待齐示例", score_inputs_complete=False,
                   score_missing_inputs=["fundamental.core_profit_ttm", "fundamental.cfo_ttm"])
        report["rankings"].append(row)
    report["errors"] = list(collection["errors"])
    report["coverage"].update(universe=10, quality_statement_available=available,
                               quality_ttm_available=collection["quality_ttm_available"],
                               financial_period_counts=collection["financial_period_counts"],
                               quality_period_counts=collection["quality_period_counts"])
    report.setdefault("ranking_incomplete_reasons", [])
    return report


def test_transient_failure_retries_only_that_batch_and_keeps_no_false_current_error():
    provider = RecordedFinancialProvider(fail=("income", PERIODS[0]))
    history, collected = load_financial_data(provider, PERIODS, TODAY, CODES)
    assert not collected["errors"] and not collected["remaining_issues"]
    assert collected["retry_rounds"] == collected["recovered_batch_count"] == 1
    retried = [x for x in provider.calls if x[2]]
    assert [(x[0], x[1]) for x in retried] == [("income", PERIODS[0])]
    assert 0 < retried[0][3] <= load_policy()["retry_budget_seconds"]
    assert len(provider.calls) == 10  # nine batches plus one repair, no price requests
    assert latest_finance(history).core_profit_ttm.tolist() == pytest.approx([108.]*10)
    assert latest_finance(history).cfo_ttm.tolist() == pytest.approx([96.]*10)
    report = report_for(collected, pending=0, available=10)
    attach_integrity(report, collected)
    assert report["financial_integrity"]["status"] == "healthy"
    validate_integrity(report, render_markdown(report))


def test_permanent_failure_is_bounded_and_must_publish_explicit_diagnostics():
    provider = RecordedFinancialProvider(fail=("income", PERIODS[0]), persistent=True)
    history, collected = load_financial_data(provider, PERIODS, TODAY, CODES)
    assert len(provider.calls) == 11 and collected["retry_rounds"] == 2
    assert len(collected["batches"]["income:2026-06-30"]["attempts"]) == 3
    assert latest_finance(history).core_profit_ytd.isna().all()
    report = report_for(collected)
    original = copy.deepcopy(report["rankings"])
    guard = attach_integrity(report, collected)
    assert guard["status"] == "degraded" and guard["publication_mode"] == "diagnostic_partial"
    assert report["status"] == "partial" and report["ranking_complete"] is False and report["scan_complete"] is False
    assert report["rankings"] == original
    text = render_markdown(report)
    assert "财报数据异常：本次仅为诊断与已核验部分" in text and "7只；不是低分淘汰" in text
    validate_integrity(report, text)


def test_coherent_http_version_mismatch_is_refreshed_without_weakening_income_bridge():
    provider = RecordedFinancialProvider(mismatch=True)
    history, collected = load_financial_data(provider, PERIODS, TODAY, CODES)
    assert collected["retry_rounds"] == 1 and not collected["remaining_issues"]
    assert latest_finance(history).quality_statement_matched.all()
    # Only the latest period whose statement differs is refreshed, not all history.
    assert {(k, p) for k, p, fresh, _ in provider.calls if fresh} == {(k, PERIODS[0]) for k in ("financial", "income", "cashflow")}


def test_historical_bridge_mismatch_is_not_hidden_by_good_latest_statement_coverage():
    class HistoricalMismatch(RecordedFinancialProvider):
        def fetch(self, function, **kwargs):
            frame = super().fetch(function, **kwargs)
            if function == "finance_quality_em" and kwargs["statement"] == "income" and kwargs["date"] == "20250630" and not kwargs.get("force_refresh"):
                frame["statement_profit_ytd"] *= 2
            return frame
    provider = HistoricalMismatch()
    history, collected = load_financial_data(provider, PERIODS, TODAY, CODES)
    assert {i["kind"] for i in collected["initial_issues"]} == {"ttm_inputs_incomplete"}
    assert latest_finance(history).core_profit_ttm.tolist() == pytest.approx([108.]*10)
    assert collected["quality_ttm_available"] == 10
    assert {(k, p) for k, p, fresh, _ in provider.calls if fresh} == {(k, PERIODS[2]) for k in ("financial", "income", "cashflow")}


def test_coherent_but_regressed_batch_is_refreshed_without_padded_rows():
    class MissingOne(RecordedFinancialProvider):
        def fetch(self, function, **kwargs):
            frame = super().fetch(function, **kwargs)
            if function == "finance_quality_em" and kwargs["statement"] == "income" and kwargs["date"] == "20260630" and not kwargs.get("force_refresh"):
                frame = frame.iloc[:-1]
            return frame
    ref = {"session": "2026-10-27", "quality_ratio": 1., "financial_period_counts": {}, "quality_period_counts": {"income:2026-06-30": 10}}
    provider = MissingOne()
    history, collected = load_financial_data(provider, PERIODS, TODAY, CODES, ref)
    assert len(latest_finance(history)) == 10 and collected["quality_ttm_available"] == 10
    assert not collected["remaining_issues"]
    assert [(k, p) for k, p, fresh, _ in provider.calls if fresh] == [("income", PERIODS[0])]


def test_failed_fresh_attempt_cannot_keep_an_older_successful_but_regressed_response():
    class FailedRepair(RecordedFinancialProvider):
        def fetch(self, function, **kwargs):
            frame = super().fetch(function, **kwargs)
            if function == "finance_quality_em" and kwargs["statement"] == "income" and kwargs["date"] == "20260630":
                if kwargs.get("force_refresh"): raise DataError("新请求失败")
                frame = frame.iloc[:-1]
            return frame
    ref = {"session": "2026-10-27", "quality_ratio": 1., "financial_period_counts": {}, "quality_period_counts": {"income:2026-06-30": 10}}
    history, collection = load_financial_data(FailedRepair(), PERIODS, TODAY, CODES, ref)
    assert latest_finance(history).core_profit_ytd.isna().all()
    assert "income:2026-06-30" not in collection["quality_period_counts"]
    assert collection["errors"]


def test_future_announcements_are_still_excluded_after_every_recovery_attempt():
    class Future(RecordedFinancialProvider):
        def fetch(self, function, **kwargs):
            frame = super().fetch(function, **kwargs)
            if function == "finance_quality_em" and kwargs["statement"] == "income" and kwargs["date"] == "20260630":
                frame["quality_announced_at"] = pd.Timestamp("2026-10-29")
            return frame
    history, collection = load_financial_data(Future(), PERIODS, TODAY, CODES)
    assert latest_finance(history).core_profit_ytd.isna().all()
    assert collection["retry_rounds"] == 2 and collection["remaining_issues"]


def test_all_primary_failures_are_retried_but_never_invent_a_ranking():
    class NoStatements:
        def __init__(self): self.calls = []
        def fetch(self, function, **kwargs):
            self.calls.append((function, kwargs.get("force_refresh", False)))
            if function == "finance_em_named": raise DataError("主财报源不可用")
            return pd.DataFrame()
    provider = NoStatements()
    with pytest.raises(DataError, match="定向重试后停止排名"):
        load_financial_data(provider, PERIODS, TODAY, CODES)
    assert Counter(x[0] for x in provider.calls) == {"finance_em_named": 9, "finance_quality_em": 6}


def test_no_automatic_retry_for_a_real_small_candidate_pool_when_financial_data_is_complete():
    provider = RecordedFinancialProvider()
    _, collected = load_financial_data(provider, PERIODS, TODAY, CODES)
    assert len(provider.calls) == 9 and collected["retry_rounds"] == 0
    report = report_for(collected, pending=0, available=10)
    before = copy.deepcopy(report["rankings"])
    assert attach_integrity(report, collected)["status"] == "healthy"
    assert len(report["rankings"]) == 2 and report["rankings"] == before


def test_score_missing_is_limited_even_when_batch_coverage_looks_healthy():
    _, collection = load_financial_data(RecordedFinancialProvider(), PERIODS, TODAY, CODES)
    report = report_for(collection, available=10)
    assert attach_integrity(report, collection)["status"] == "limited"
    assert report["scan_complete"] is False and report["ranking_complete"] is False
    assert "入口合格、评分数据待齐" in render_markdown(report)
    validate_integrity(report, render_markdown(report))


@pytest.mark.parametrize("mutation", ["missing_guard", "false_healthy", "false_complete", "hidden_errors", "changed_counts", "cached_retry"])
def test_publication_guard_rejects_hidden_or_forged_financial_completeness(mutation):
    _, collection = load_financial_data(RecordedFinancialProvider(fail=("income", PERIODS[0]), persistent=True), PERIODS, TODAY, CODES)
    report = report_for(collection); attach_integrity(report, collection)
    if mutation == "missing_guard": report.pop("financial_integrity")
    if mutation == "false_healthy": report["financial_integrity"]["status"] = "healthy"
    if mutation == "false_complete": report.update(status="complete", scan_complete=True, ranking_complete=True)
    if mutation == "hidden_errors": report["errors"] = []
    if mutation == "changed_counts": report["coverage"]["quality_period_counts"] = {}
    if mutation == "cached_retry": report["financial_integrity"]["collection"]["batches"]["income:2026-06-30"]["attempts"][-1]["forced_refresh"] = False
    with pytest.raises((ValueError, DataError)):
        validate_integrity(report, render_markdown(report))


def test_publisher_checks_the_guard_before_any_github_write(tmp_path, monkeypatch):
    from tommy_capital.cli import clean, make_summary, write_outputs
    from tommy_capital.publish import publish
    _, collection = load_financial_data(RecordedFinancialProvider(), PERIODS, TODAY, CODES)
    report = report_for(collection, pending=0, available=10); attach_integrity(report, collection)
    report.pop("financial_integrity")
    write_outputs(tmp_path, report)
    (tmp_path/"summary.json").write_text(json.dumps(clean(make_summary(report))))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("must not publish missing protection"))
    with pytest.raises(ValueError, match="诊断缺失"):
        publish(tmp_path, "test-token", "owner/repo")


def test_missing_chinese_alert_cannot_pass_audit_and_old_reports_keep_their_contract():
    _, collection = load_financial_data(RecordedFinancialProvider(fail=("income", PERIODS[0]), persistent=True), PERIODS, TODAY, CODES)
    report = report_for(collection); attach_integrity(report, collection)
    with pytest.raises(ValueError, match="显著提示"): validate_integrity(report, "正常结果")
    validate_integrity({"status": "partial"})
    with pytest.raises(ValueError, match="失败诊断"):
        validate_integrity({"status": "failed", "scan_complete": True, "rankings": []})


def summary(day, available=99, errors=None):
    return {"session": day, "scan_phase": "closing", "scan_type": "after_close", "status": "partial",
            "scan_complete": False,
            "selection_rule": "dual_route_monthly_recovery", "generated_at": day+"T15:30:00+08:00", "finished_at": day+"T16:30:00+08:00",
            "errors": errors or [], "coverage": {"universe": 100, "quality_statement_available": available,
            "financial_period_counts": {}, "quality_period_counts": {"income:2026-06-30": 100}}}


@pytest.mark.parametrize("batch_error", ["财务质量 income 2026-06-30: 未完成", "财报 2026-09-30: 主财报未完成"])
def test_degraded_day_cannot_become_the_new_normal_and_future_or_ancient_reference_is_ignored(tmp_path, batch_error):
    for day, value in [("2026-10-27", summary("2026-10-27")), ("2026-10-28", summary("2026-10-28", 54)),
                       ("2026-10-30", summary("2026-10-30")), ("2026-10-01", summary("2026-10-01"))]:
        root = tmp_path/day; root.mkdir(); (root/"closing-summary.json").write_text(json.dumps(value))
    reference = reference_report(tmp_path, "2026-10-29", load_policy())
    assert reference["session"] == "2026-10-27" and len(reference["sha256"]) == 64
    assert reference_report(tmp_path, "2026-10-01", load_policy()) is None
    report = summary("2026-10-27", errors=[batch_error])
    (tmp_path/"2026-10-27/closing-summary.json").write_text(json.dumps(report))
    assert reference_report(tmp_path, "2026-10-29", load_policy()) is None


def test_reference_is_coverage_only_not_a_financial_fallback_and_batch_regression_is_retried():
    ref = {"session": "2026-10-27", "quality_ratio": .99, "quality_statement_available": 99, "universe": 100,
           "financial_period_counts": {}, "quality_period_counts": {"income:2026-06-30": 100}}
    issues = coverage_issues(94, 100, {}, {"income:2026-06-30": 90}, [], ref, load_policy())
    assert {i["kind"] for i in issues} == {"quality_coverage_drop", "batch_count_regression"}
    _, collection = load_financial_data(RecordedFinancialProvider(), PERIODS, TODAY, CODES, ref)
    assert collection["retry_rounds"] == 2  # source stays at 10 real fixture rows, not padded to 100
    assert any(i["kind"] == "batch_count_regression" for i in collection["remaining_issues"])


def test_force_refresh_bypasses_outer_and_nested_page_cache_but_updates_the_canonical_key(tmp_path, monkeypatch):
    calls = []
    def worker(*args, **kwargs):
        calls.append(json.loads(kwargs["input"]))
        return SimpleNamespace(stdout=pd.DataFrame({"value": [len(calls)]}).to_json(orient="split"))
    monkeypatch.setattr(subprocess, "run", worker)
    provider = Provider(cache=tmp_path, retries=1)
    first = provider.fetch("finance_quality_em", date="20260630", statement="income")
    second = provider.fetch("finance_quality_em", date="20260630", statement="income", force_refresh=True, budget_seconds=5)
    third = provider.fetch("finance_quality_em", date="20260630", statement="income")
    assert first.value.iloc[0] == 1 and second.value.iloc[0] == third.value.iloc[0] == 2
    assert len(calls) == 2 and calls[-1]["kwargs"]["refresh_pages"] is True
    assert "force_refresh" not in calls[-1]["kwargs"] and "budget_seconds" not in calls[-1]["kwargs"]
    assert provider.lineage[-2]["financial_pages_refreshed"] and provider.lineage[-1]["cache_hit"]


def test_explicit_page_refresh_does_not_reuse_coherent_but_regressed_page_cache(monkeypatch, tmp_path):
    from tommy_capital import live
    from test_financial_page_coherence import envelope, quality_record
    monkeypatch.setenv("TOMMY_PAGE_CACHE", str(tmp_path))
    calls = []
    def source(url, params):
        calls.append(params["pageNumber"])
        return envelope([quality_record("60000"+str(params["pageNumber"]))], 2)
    monkeypatch.setattr(live, "get_json", source)
    live.finance_quality("20260930", "income")
    live.finance_quality("20260930", "income")
    assert len(calls) == 2
    live.finance_quality("20260930", "income", refresh_pages=True)
    assert len(calls) == 4


def test_existing_refresh_option_also_forces_nested_financial_pages(tmp_path, monkeypatch):
    calls = []
    def worker(*args, **kwargs):
        calls.append(json.loads(kwargs["input"]))
        return SimpleNamespace(stdout=pd.DataFrame({"value": [1]}).to_json(orient="split"))
    monkeypatch.setattr(subprocess, "run", worker)
    Provider(cache=tmp_path, retries=1, refresh=True).fetch("finance_em_named", date="20260630", schema_version=2)
    assert calls[0]["kwargs"]["refresh_pages"] is True


@pytest.mark.parametrize("problem", ["bad_json", "bad_time", "missing_data", "bad_frame", "empty_frame"])
def test_corrupted_whole_statement_cache_cannot_trap_retries(tmp_path, monkeypatch, problem):
    calls = []
    def worker(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(stdout=pd.DataFrame({"value": [1]}).to_json(orient="split"))
    monkeypatch.setattr(subprocess, "run", worker)
    provider = Provider(cache=tmp_path, retries=1)
    provider.fetch("finance_quality_em", date="20260630", statement="income")
    path = next(tmp_path.glob("*.json")); payload = json.loads(path.read_text())
    if problem == "bad_json": path.write_text("{")
    else:
        if problem == "bad_time": payload["fetched_epoch"] = None
        if problem == "missing_data": payload.pop("data")
        if problem == "bad_frame": payload["data"] = "not-a-dataframe"
        if problem == "empty_frame": payload["data"] = pd.DataFrame().to_json(orient="split")
        path.write_text(json.dumps(payload))
    assert provider.fetch("finance_quality_em", date="20260630", statement="income").value.iloc[0] == 1
    assert len(calls) == 2 and not provider.lineage[-1]["cache_hit"]


def test_corrupted_page_cache_is_refetched_instead_of_replaying_the_bad_payload(tmp_path, monkeypatch):
    from tommy_capital import live
    from test_financial_page_coherence import quality_record, envelope
    monkeypatch.setenv("TOMMY_PAGE_CACHE", str(tmp_path)); calls = []
    def source(url, params):
        calls.append(1)
        return envelope([quality_record("600001")], 1, 1)
    monkeypatch.setattr(live, "get_json", source)
    live.finance_quality("20260930", "income")
    path = next(tmp_path.glob("*.json"))
    path.write_text(json.dumps({"fetched_epoch": time.time(), "data": None}))
    assert len(live.finance_quality("20260930", "income")) == 1 and len(calls) == 2


def test_recovery_budget_caps_subprocess_timeout_and_no_budget_is_sent_to_market_worker(tmp_path, monkeypatch):
    calls = []
    def worker(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs["timeout"] <= .05
        assert "budget_seconds" not in json.loads(kwargs["input"])["kwargs"]
        return SimpleNamespace(stdout=pd.DataFrame({"value": [1]}).to_json(orient="split"))
    monkeypatch.setattr(subprocess, "run", worker)
    Provider(cache=tmp_path, timeout=180, retries=1).fetch("finance_quality_em", budget_seconds=.05, date="20260630", statement="income")
    assert len(calls) == 1


def test_budget_exhaustion_stops_worker_retries_instead_of_sleeping_past_the_budget(tmp_path, monkeypatch):
    import tommy_capital.data as module
    clock = {"now": 0.}; calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(module.time, "sleep", lambda *a: pytest.fail("must not sleep after budget expiry"))
    def timeout(*args, **kwargs):
        calls.append(kwargs); clock["now"] += kwargs["timeout"]
        raise subprocess.TimeoutExpired("fixture", kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(DataError):
        Provider(cache=tmp_path, timeout=180, retries=10).fetch("finance_quality_em", budget_seconds=.05, date="20260630", statement="income")
    assert len(calls) == 1


def test_financial_recovery_stops_scheduling_more_passes_when_budget_expires(monkeypatch):
    import tommy_capital.financial_integrity as module
    clock = {"now": 0.}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])
    class SlowFailure(RecordedFinancialProvider):
        def fetch(self, function, **kwargs):
            if kwargs.get("force_refresh"): clock["now"] += 181
            return super().fetch(function, **kwargs)
    provider = SlowFailure(fail=("income", PERIODS[0]), persistent=True)
    _, collection = load_financial_data(provider, PERIODS, TODAY, CODES)
    assert collection["retry_budget_exhausted"] and collection["retry_rounds"] == 1
    assert len([x for x in provider.calls if x[2]]) == 1


def test_main_audit_runs_the_financial_guard_before_accepting_a_report(tmp_path):
    from tommy_capital.audit import audit
    from tommy_capital.cli import clean, make_summary, write_outputs
    _, collection = load_financial_data(RecordedFinancialProvider(), PERIODS, TODAY, CODES)
    report = report_for(collection, pending=0, available=10); attach_integrity(report, collection)
    report.pop("financial_integrity")
    write_outputs(tmp_path, report)
    (tmp_path/"summary.json").write_text(json.dumps(clean(make_summary(report))))
    with pytest.raises(ValueError, match="诊断缺失"): audit(tmp_path)


@pytest.mark.parametrize("key,value", [("retry_passes", 100), ("workers", 10), ("retry_budget_seconds", 10000),
                                       ("minimum_quality_coverage", .1), ("minimum_quality_coverage", True)])
def test_integrity_configuration_cannot_disable_bounds_or_turn_them_into_scoring_weights(key, value):
    policy = load_policy(); policy[key] = value
    with pytest.raises(DataError): load_financial_data(RecordedFinancialProvider(), PERIODS, TODAY, CODES, policy=policy)


def test_status_page_marks_degradation_instead_of_normal_partial_success():
    from tommy_capital.scan_status import status_entries
    state = {"session": "2026-10-28", "status": "partial", "started_at": "2026-10-28T15:30:00+08:00",
             "updated_at": "2026-10-28T16:30:00+08:00", "run_url": "https://github.com/owner/repo/actions/runs/1", "financial_integrity_status": "degraded"}
    entries = status_entries(state, "# unchanged report")
    assert "财报数据异常" in entries[1]["content"]
    assert entries[2]["content"] == "# unchanged report"


def test_real_archived_incident_is_recognized_without_mutating_report_or_history():
    from tommy_capital.financial_integrity import replay_summary
    import hashlib
    root = Path(__file__).resolve().parents[1]
    files = [root/"results/2026-10-28/closing-summary.json", root/"results/top10-history.json",
             root/"tommy_capital/scoring.py", root/"tommy_capital/strategy.py", root/"operations/watchlist-preferences.json"]
    before = [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
    result = replay_summary(files[0], root/"results")
    assert (result["entry_qualified"], result["score_complete"], result["score_data_pending"]) == (9, 2, 7)
    # Oct 27 has high quality coverage but its primary Q3 batch also failed;
    # it must not be accepted as a clean reference just because of that ratio.
    assert result["degradation_detected"] and result["reference"]["session"] == "2026-10-26"
    assert {i.get("key") for i in result["alerts"] if i["kind"] == "failed_batch"} == {"income:2026-09-30", "cashflow:2025-09-30"}
    assert result["new_financial_requests"] == result["new_market_requests"] == 0
    assert before == [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]


def test_replay_cli_refuses_to_write_over_the_production_results_directory(tmp_path):
    import sys
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-m", "tommy_capital.financial_integrity", "--replay-summary", str(root/"results/2026-10-28/closing-summary.json"),
                             "--results-directory", str(root/"results"), "--out", str(root/"results/replay")], cwd=root, capture_output=True, text=True)
    assert result.returncode != 0 and "不得写入正式results" in result.stderr
