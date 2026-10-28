"""Offline protocol regressions for the 2026-10-28 missing-score incident."""
import copy
import json

import pytest

from tommy_capital import live
from tommy_capital.diagnostics import ranking_funnel, report_diagnostics
from tommy_capital.reporting import render_markdown
from tommy_capital.scoring import ranked_top20


def quality_record(code, period="2026-09-30"):
    return {"SECURITY_CODE": code, "SECUCODE": code+".SH", "REPORT_DATE": period,
            "NOTICE_DATE": "2026-10-28", "DEDUCT_PARENT_NETPROFIT": 10., "PARENT_NETPROFIT": 11.,
            "TOTAL_OPERATE_INCOME": 100., "NETCASH_OPERATE": 12.}


def envelope(rows, count, pages=2):
    return {"code": 0, "result": {"pages": pages, "count": count, "data": rows}}


@pytest.mark.parametrize("statement", ["income", "cashflow"])
def test_inconsistent_success_pages_are_evicted_before_full_batch_retry(monkeypatch, tmp_path, statement):
    monkeypatch.setenv("TOMMY_PAGE_CACHE", str(tmp_path))
    calls = []
    def fetch(url, params):
        page = params["pageNumber"]
        calls.append(page)
        # First response claims three rows, but only two are actually returned.
        # Both HTTP responses succeeded and would otherwise stay cached 600s.
        count = 3 if calls.count(page) == 1 else 2
        return envelope([quality_record("60000"+str(page))], count)
    monkeypatch.setattr(live, "get_json", fetch)
    result = live.finance_quality("20260930", statement)
    assert len(result) == 2 and sorted(result.code) == ["600001", "600002"]
    assert calls.count(1) == calls.count(2) == 2
    # A coherent subsequent call may reuse the newly successful pages.
    assert len(live.finance_quality("20260930", statement)) == 2
    assert len(calls) == 4


def test_each_pages_count_must_match_even_if_combined_length_matches_first_page(monkeypatch, tmp_path):
    monkeypatch.setenv("TOMMY_PAGE_CACHE", str(tmp_path))
    calls = []
    def fetch(url, params):
        number = params["pageNumber"]; calls.append(number)
        if number == 1:
            return envelope([quality_record("600001")], 3)
        return envelope([quality_record("600002"), quality_record("600003")], 4 if calls.count(2) == 1 else 3)
    monkeypatch.setattr(live, "get_json", fetch)
    assert len(live.finance_quality("20260930", "income")) == 3
    assert calls.count(1) == calls.count(2) == 2


@pytest.mark.parametrize("problem", ["count", "duplicate", "empty", "pages", "overlap"])
def test_persistently_bad_pages_remain_missing_and_retry_is_bounded(monkeypatch, tmp_path, problem):
    monkeypatch.setenv("TOMMY_PAGE_CACHE", str(tmp_path))
    calls = []
    def fetch(url, params):
        page = params["pageNumber"]; calls.append(page)
        rows = [quality_record("60000"+str(page))]
        count, pages = 3 if problem == "count" else 2, 2
        if problem == "duplicate": rows = [quality_record("600001")]
        if problem == "empty" and page == 2: rows = []
        if problem == "pages" and page == 2: pages = 3
        if problem == "overlap":
            rows = [quality_record("600001"), quality_record("60000"+str(page+1))]
            count = 4
        return envelope(rows, count, pages)
    monkeypatch.setattr(live, "get_json", fetch)
    with pytest.raises(ValueError, match="仍不完整"):
        live.finance_quality("20260930", "income")
    assert calls.count(1) == calls.count(2) == 2
    assert not list(tmp_path.glob("*.json"))  # do not poison the next worker retry


def test_clearing_one_failed_query_preserves_other_statements_and_periods(monkeypatch, tmp_path):
    monkeypatch.setenv("TOMMY_PAGE_CACHE", str(tmp_path))
    calls = []
    def fetch(url, params):
        calls.append(params)
        if params["reportName"] == "RPT_DMSK_FN_CASHFLOW":
            return envelope([quality_record("600001")], 1, 1)
        return envelope([quality_record("600001")], 2, 1)
    monkeypatch.setattr(live, "get_json", fetch)
    assert len(live.finance_quality("20260930", "cashflow")) == 1
    with pytest.raises(ValueError): live.finance_quality("20260930", "income")
    assert len(live.finance_quality("20260930", "cashflow")) == 1
    assert sum(p["reportName"] == "RPT_DMSK_FN_CASHFLOW" for p in calls) == 1


def test_explicit_unannounced_quarter_is_empty_but_wrong_period_stays_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("TOMMY_PAGE_CACHE", str(tmp_path))
    monkeypatch.setattr(live, "get_json", lambda *a: {"code": 9201, "result": None})
    assert live.finance_quality("20260930", "income").empty
    assert live.finance("20260930").empty
    monkeypatch.setenv("TOMMY_PAGE_CACHE", str(tmp_path/"wrong-period"))
    monkeypatch.setattr(live, "get_json", lambda *a: envelope([quality_record("600001", "2026-06-30")], 1, 1))
    with pytest.raises(ValueError, match="报告期不匹配"):
        live.finance_quality("20260930", "income")


def test_ordinary_finance_uses_the_same_bounded_coherence_guard(monkeypatch, tmp_path):
    from test_live_clients import record
    monkeypatch.setenv("TOMMY_PAGE_CACHE", str(tmp_path))
    calls = []
    def fetch(url, params):
        page = params["pageNumber"]; calls.append(page)
        return envelope([record("60000"+str(page))], 3 if calls.count(page) == 1 else 2)
    monkeypatch.setattr(live, "get_json", fetch)
    assert len(live.finance("20260630")) == 2 and calls.count(1) == calls.count(2) == 2


def test_pending_score_data_is_separate_from_qualified_and_top20_and_does_not_mutate_them():
    from test_volume_tracking_report import closing
    report = closing("2026-10-28", 2)
    exemplar = report["rankings"][0]
    for i in range(7):
        item = copy.deepcopy(exemplar)
        item.update(code=f"600{i:03d}", name="数据待齐示例"+str(i), score_inputs_complete=False,
                    score_missing_inputs=["fundamental.core_profit_ttm", "fundamental.cfo_ttm"])
        report["rankings"].append(item)
    report["errors"] = ["财务质量 income 2026-09-30: finance_quality_em 失败：分页行数不符"]
    original = copy.deepcopy(report)
    funnel = ranking_funnel(report)
    assert (funnel["entry_qualified"], funnel["score_complete"], funnel["top20_displayed"], funnel["score_data_pending"]) == (9, 2, 2, 7)
    assert len(funnel["pending_rows"]) == 7 and report_diagnostics(report)["ranking_funnel"] == funnel
    text = render_markdown(report)
    assert "7只；不是低分淘汰" in text and "入口合格、评分数据待齐（不参与排名）" in text
    assert "TTM扣非利润、TTM经营现金流" in text and "财务质量 income 2026-09-30" in text
    assert report == original and len(ranked_top20(report["rankings"])) == 2
    json.dumps(funnel, allow_nan=False)
