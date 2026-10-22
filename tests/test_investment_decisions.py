"""Frozen hypothetical inputs only; no test fixture is published as market data."""
import copy
import json
from datetime import date, datetime

import pandas as pd
import pytest

from tommy_capital.backtest import ablation_score, freeze_ablation, walk_forward_splits, attach_benchmarks, summarize_checks
from tommy_capital.backtest import snapshot_turnover, market_regime
from tommy_capital.business_classifier import classify_business
from tommy_capital.data import SHANGHAI
from tommy_capital.diagnostics import report_diagnostics, failure_category
from tommy_capital.intrinsic_value import discounted_cash_flow, scenario_value, reverse_valuation, evaluate_intrinsic
from tommy_capital.investment_research import build_research, research_queue, validate_public_research
from tommy_capital.portfolio_risk import position_limit, analyze_portfolio, valuation_technical_matrix, compare_opportunity
from tommy_capital.quality_analysis import analyze_quality, candidate_v2_score, V2_WEIGHTS
from tommy_capital.research import freeze_snapshot
from tommy_capital.thesis_tracker import save_thesis
from tommy_capital.reporting import render_markdown
from tommy_capital.cli import clean, make_summary, write_outputs
from tommy_capital.valuation_view import refresh
from test_balanced_top20 import report_fixture
from test_core import good_row


def evidence():
    return {"records": [{"id": "basis", "url": "https://example.org/offline-fixture", "published_at": "2026-10-01",
                         "field": "valuation_basis", "verification": "verified", "sha256": "a"*64}]}


def model(method="normalized_profit"):
    return {"method": method, "as_of": "2026-10-08", "reviewed": True, "source_ids": ["basis"],
            "assumption_basis": "Frozen unit-test assumptions, not a real company valuation",
            "scenarios": {name: {"normalized_profit_cny": profit, "fair_pe": 10}
                          for name, profit in zip(("bear", "base", "bull"), (60., 100., 140.))}}


def company():
    return {"code": "600000", "price": 7., "market_cap": 700., "profit_ttm": 60.}


def test_three_scenarios_margin_is_discount_to_value_not_upside_and_reverse_is_separate():
    result = evaluate_intrinsic(company(), [model()], evidence(), "2026-10-08")
    assert result["status"] == "complete" and result["scenario_range"] == [6., 14.]
    assert result["safety_margin"] == pytest.approx(.3)
    assert result["bear_downside"] == pytest.approx(1/7)
    assert result["models"][0]["upside_to_base"] == pytest.approx(3/7)
    assert result["price_judgment"] == "低估（假设下）" and not result["influences_ranking"]
    reverse = reverse_valuation(300., 6., 25., 3)
    assert reverse["required_profit_cny"] == 12.
    assert reverse["required_earnings_cagr"] == pytest.approx(2**(1/3)-1)
    assert reverse_valuation(300., -6., 25., 3)["status"] == "unavailable"


@pytest.mark.parametrize("problem", ["future_model", "old_model", "unreviewed", "missing_source", "future_source", "unverified_source", "no_hash", "no_cap", "unordered", "missing_case"])
def test_insufficient_evidence_cannot_turn_into_intrinsic_value(problem):
    row, m, ev = company(), model(), evidence()
    if problem == "future_model": m["as_of"] = "2026-10-09"
    elif problem == "old_model": m["as_of"] = "2025-01-01"
    elif problem == "unreviewed": m["reviewed"] = False
    elif problem == "missing_source": m["source_ids"] = ["absent"]
    elif problem == "future_source": ev["records"][0]["published_at"] = "2026-10-09"
    elif problem == "unverified_source": ev["records"][0]["verification"] = "unavailable"
    elif problem == "no_hash": ev["records"][0].pop("sha256")
    elif problem == "no_cap": row.pop("market_cap")
    elif problem == "unordered": m["scenarios"]["bear"]["normalized_profit_cny"] = 200.
    elif problem == "missing_case": m["scenarios"].pop("bull")
    result = evaluate_intrinsic(row, [m], ev, "2026-10-08")
    assert result["status"] == "unavailable" and result["price_judgment"] == "无法判断"


def test_fcff_and_fcfe_use_correct_discount_rates_and_equity_bridge():
    dcf = discounted_cash_flow([10., 11.], .1, .02)
    expected = 10/1.1+11/1.1**2+11*1.02/(.1-.02)/1.1**2
    assert dcf["value_cny"] == pytest.approx(expected)
    bridge = {"net_debt_cny": 20., "minority_interest_cny": 3., "preferred_equity_cny": 2., "nonoperating_assets_cny": 4.}
    fcff = scenario_value("fcff_dcf", {"fcff_cny": [10., 11.], "wacc": .1, "terminal_growth": .02, **bridge})
    assert fcff["equity_value_cny"] == pytest.approx(expected-20-3-2+4)
    assert scenario_value("fcfe_dcf", {"fcfe_cny": [10.,11.], "cost_of_equity": .1, "terminal_growth": .02})["equity_value_cny"] == pytest.approx(expected)
    with pytest.raises(ValueError, match="FCFE"):
        scenario_value("fcfe_dcf", {"fcfe_cny":[10.,11.], "cost_of_equity":.1,"terminal_growth":.02,"net_debt_cny":20.})
    with pytest.raises(ValueError, match="minority"):
        scenario_value("fcff_dcf", {"fcff_cny":[10.,11.],"wacc":.1,"terminal_growth":.02,"net_debt_cny":20.})
    with pytest.raises(ValueError): discounted_cash_flow([10.], .02, .03)
    with pytest.raises(ValueError): discounted_cash_flow([10.,None], .1, .02)


def test_sotp_does_not_mix_enterprise_and_equity_and_financial_model_is_explicit():
    bridge={"net_debt_cny":20.,"minority_interest_cny":0.,"preferred_equity_cny":0.,"nonoperating_assets_cny":0.}
    segments=[{"name":"a","basis":"enterprise_value","value_cny":100.},{"name":"b","basis":"enterprise_value","value_cny":50.}]
    assert scenario_value("sotp", {"segments":segments,**bridge})["equity_value_cny"] == 130.
    segments[1]["basis"]="equity_value"
    with pytest.raises(ValueError): scenario_value("sotp",{"segments":segments,**bridge})
    assert scenario_value("pb_roe",{"normalized_roe":.15,"terminal_growth":.03,"cost_of_equity":.1,"common_book_equity_cny":100.})["equity_value_cny"] == pytest.approx(100*.12/.07)


def test_quality_uses_average_capital_and_capex_and_missing_fields_are_unknown():
    r=analyze_quality({"nopat_ttm":20.,"invested_capital_open_cny":100.,"invested_capital_close_cny":300.,"cfo_ttm":50.,"capex_ttm_cny":45.,"profit_ttm":25.,"market_cap":1000.})
    assert r["metrics"]["roic"] == .1 and r["metrics"]["free_cash_flow_cny"] == 5.
    assert r["metrics"]["cash_after_maintenance_capex_cny"] is None
    bank=analyze_quality({"industry":"银行", "cfo_ttm":100.,"profit_ttm":10.})
    assert bank["metrics"]["cash_conversion"] is None
    assert sum(V2_WEIGHTS.values()) == 50
    assert candidate_v2_score({})["score"] is None and not candidate_v2_score(V2_WEIGHTS)["production_enabled"]


@pytest.mark.parametrize("row,kind", [({"industry":"银行"},"financial"),({"profit_ttm":-1.},"loss_reversal"),({"industry":"煤炭","profit_ttm":20.},"cyclical"),({"profit_ttm":20.,"revenue_yoy":30.},"high_growth"),({},"unknown")])
def test_business_classification_is_provisional_not_a_scoring_gate(row,kind):
    result=classify_business(row)
    assert result["type"]==kind and result["status"]=="provisional" and not result["influences_ranking"]


def test_risk_budget_excludes_missing_positions_and_rounds_only_to_supplied_lot():
    sizing=position_limit(100000.,100.,92.,.005,.2,1)
    assert sizing["shares_ceiling"]==62 and sizing["planned_loss_cny"]==496
    p=analyze_portfolio([{"code":"600000","shares":100.},{"code":"600001"}],{"600000":{"price":10.,"session":"2026-10-08"}},cash=1000.,as_of="2026-10-08")
    assert p["status"]=="partial" and p["investable_equity_cny"] is None
    assert all(r["weight"] is None for r in p["rows"])
    complete=analyze_portfolio([{"code":"600000","shares":100.,"cost":8.}],{"600000":{"price":10.,"session":"2026-10-08"}},cash=1000.,as_of="2026-10-08")
    assert complete["rows"][0]["weight"]==.5 and complete["rows"][0]["unrealized_profit_cny"]==200.
    assert complete["portfolio_drawdown"] is None


def test_valuation_technical_matrix_does_not_turn_15m_noise_into_a_sell_order():
    result=valuation_technical_matrix("高估（假设下）",False,True)
    assert result["partial_reduction_scenario"] is None and not result["orders_authorized"]
    assert valuation_technical_matrix("合理",True,True)["partial_reduction_scenario"] == [1/3,.5]
    assert compare_opportunity({"price":10.,"base_value":15.,"bear_value":8.},{"price":10.})["status"] == "insufficient_evidence"


def test_trial_weights_preserve_totals_and_cannot_mutate_production_rankings():
    report=report_fixture(25);original=copy.deepcopy(report)
    row=report["rankings"][0]
    assert ablation_score(row,"v9") == pytest.approx(row["fundamental_score"]+row["technical_score"])
    trial=freeze_ablation(report,datetime(2026,10,8,17,tzinfo=SHANGHAI))
    assert set(trial["trials"])=={"v9","ma55_12","without_weekly_histogram","fundamental_ma55_only"}
    assert all(len(v)==20 for v in trial["trials"].values())
    assert not trial["point_in_time_universe_complete"] and report==original
    row["score_breakdown"]["technical"]["weekly"].pop("component_maxima")
    with pytest.raises(ValueError,match="不能补造"):ablation_score(row,"v9")


def test_walk_forward_purges_label_horizon_and_benchmark_gaps_stay_missing():
    days=pd.bdate_range("2025-01-01",periods=240)
    splits=walk_forward_splits(days)
    assert splits and pd.Timestamp(splits[0]["test_start"])>pd.Timestamp(splits[0]["training_end"])
    with pytest.raises(ValueError):walk_forward_splits(days,purge_sessions=5)
    checks=[{"status":"observed","entry_session":"2026-10-08","exit_session":"2026-10-09","horizon_sessions":5,"hypothetical_net_return_pct":8.}]
    prices=pd.DataFrame({"日期":["2026-10-08","2026-10-09"],"开盘":[100.,102.],"收盘":[101.,104.],"最高":[102.,105.],"最低":[99.,100.]})
    result=attach_benchmarks(checks,["2026-10-08","2026-10-09"],lambda *args:prices)
    assert result[0]["benchmarks"][0]["hypothetical_net_excess_pct"]==pytest.approx(4.)
    missing=attach_benchmarks(checks,["2026-10-08","2026-10-09"],lambda *args:prices.iloc[:1])
    assert missing[0]["benchmarks"][0]["status"]=="missing"
    assert not summarize_checks(checks)["survivorship_bias_resolved"]


def test_independent_research_survives_enrichment_without_changing_frozen_inputs(tmp_path):
    report=report_fixture(3);report["ranking_complete"]=False
    frozen=freeze_snapshot(report);before=copy.deepcopy(report)
    report["investment_research"]=build_research(report)
    report["data_quality"]=report_diagnostics(report)
    assert all(r["valuation"]["status"]=="unavailable" for r in report["investment_research"]["rows"].values())
    write_outputs(tmp_path,report);refresh(tmp_path)
    saved=json.loads((tmp_path/"report.json").read_text())
    assert freeze_snapshot(saved)==frozen
    assert saved["rankings"]==clean(before["rankings"])
    assert "企业研究类别" in (tmp_path/"report.md").read_text()
    assert make_summary(saved)["data_quality"]==report["data_quality"]
    with pytest.raises(ValueError,match="私人持仓"):validate_public_research({"cost":10.})


def test_research_queue_and_failure_categories_do_not_grant_eligibility():
    report=report_fixture(0)
    report["data_gaps"]=[{"code":"600000","resolved":False,"attempt_errors":["日线历史不足250根（实际99根）"]},
                         {"code":"600001","resolved":False,"attempt_errors":["最新K线未覆盖要求日期"]}]
    report["observations"]=[{"code":"600002","quality_statement_matched":True,"financial_routes":{"route_b_prequalified":True,"route_b_passed":False,"leading":{"core_improving":True}},"technical":{"monthly_recovery":{"route_b_path":True}}}]
    queue=research_queue(report)
    assert queue[0]["priority"]==3 and not queue[0]["auto_qualified"]
    diag=report_diagnostics(report)
    assert diag["market"]["missing"]==2 and diag["retry_queue"][0]["retryable"] is False
    assert diag["ai"]["mode"]=="program_rules_only"
    assert failure_category("旧日线不足250根，或最新K线未覆盖")[0]=="stale_session"


def test_local_thesis_is_revision_guarded_and_does_not_assume_holdings(tmp_path):
    file=tmp_path/"theses.json"
    first=save_thesis("600000","测试假设","下次财报验证","经营证据反转时失效","2026-11-01",file)
    assert first["companies"]["600000"]["evidence_status"]=="user_hypothesis_not_verified_fact"
    with pytest.raises(ValueError,match="另一窗口"):
        save_thesis("600000","新假设","确认条件","失效条件",None,file)


def test_reverse_does_not_treat_future_base_case_profit_as_current_profit():
    value=evaluate_intrinsic(company(),[model()],evidence(),"2026-10-08")
    assert value["models"][0]["reverse"]["status"]=="unavailable"
    with pytest.raises(ValueError):evaluate_intrinsic(company(),[model()],evidence(),"2026-10-08",{"minimum_margin":2})


def test_market_context_is_causal_and_never_becomes_a_ranking_gate():
    days=pd.bdate_range("2025-01-01",periods=140)
    frame=pd.DataFrame({"日期":days,"收盘":[10+i*.01 for i in range(140)]})
    cutoff=days[125].date().isoformat()
    before=market_regime(frame,cutoff)
    frame.loc[126:,"收盘"]=1000.
    assert market_regime(frame,cutoff)==before and not before["influences_ranking"]
    assert market_regime(frame.iloc[:50],cutoff)["status"]=="unavailable"


def test_turnover_separates_model_versions_and_does_not_multiply_same_session():
    rows=[{"model_version":"v9","data_session":"2026-10-08","decision_at":"2026-10-08T17:00:00+08:00","top20":[{"code":"600000"}]},
          {"model_version":"v8","data_session":"2026-10-09","decision_at":"2026-10-09T17:00:00+08:00","top20":[{"code":"600001"}]},
          {"model_version":"v9","data_session":"2026-10-12","decision_at":"2026-10-12T17:00:00+08:00","top20":[{"code":"600001"}]}]
    result=snapshot_turnover(rows)
    assert len(result)==1 and result[0]["model_version"]=="v9" and result[0]["name_turnover"]==1.
