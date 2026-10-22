"""New explicitly authorized model changes, isolated from monitoring."""
import copy
from datetime import date

import pytest

from tommy_capital.cli import DEFAULTS, validate_config
from tommy_capital.strategy import evaluate, financial_routes
from tommy_capital.loss_recovery import loss_recovery_check
from tommy_capital.scoring import balanced_score, TECHNICAL_WEIGHTS
from test_balanced_top20 import full_technical
from test_core import good_row


def recovering():
    row={**good_row(),"profit_ttm":-1000000.,"market_cap":100000000.,"revenue_ttm":100000000.,
         "revenue_yoy":5.,"pb":1.5,"period":"2026-06-30","pe_ttm":-100.,
         "profit_ytd":-50.,"core_profit_ytd":-60.,"revenue_ytd":1000.,"cfo_ytd":-10.}
    row["loss_recovery_periods"]=[{"period":period,"same_period":prior,"profit":-50.,"prior_profit":-100.,
        "core_profit":-60.,"prior_core_profit":-120.,"revenue":1000.,"prior_revenue":950.,
        "cfo":-10.,"prior_cfo":-30.,"quality_matched":True} for period,prior in
        (("2026-03-31","2025-03-31"),("2026-06-30","2025-06-30"))]
    return row


def test_still_loss_making_can_qualify_and_score_but_never_as_profit_realized():
    row=recovering()
    finance=financial_routes(row,DEFAULTS,date(2026,10,15))
    assert finance["route_a_reasons"] and not finance["route_b_reasons"]
    assert finance["leading"]["variant"]=="loss_recovery"
    tech=full_technical()
    scored=balanced_score(row,tech,"B",finance["leading"])
    assert scored["fundamental_score"]>0 and scored["technical_score"]<=50
    assert set(scored["score_breakdown"]["technical"])=={"monthly","weekly"}
    assert finance["leading"]["loss_recovery"]["passed"]
    assert "盈利尚未兑现" in finance["leading"]["loss_recovery"]["valuation_basis"]


@pytest.mark.parametrize("problem",["one_period","worse_core","worse_cash","no_quality","bad_comparable","nonconsecutive","expensive","negative_growth"])
def test_loss_recovery_needs_two_matched_quality_periods_and_real_valuation(problem):
    row=recovering()
    if problem=="one_period": row["loss_recovery_periods"]=row["loss_recovery_periods"][-1:]
    elif problem=="worse_core": row["loss_recovery_periods"][-1]["core_profit"]=-150.
    elif problem=="worse_cash": row["loss_recovery_periods"][-1]["cfo"]=-100.
    elif problem=="no_quality": row["loss_recovery_periods"][-1]["quality_matched"]=False
    elif problem=="bad_comparable": row["loss_recovery_periods"][-1]["same_period"]="2025-03-31"
    elif problem=="nonconsecutive": row["loss_recovery_periods"][0]["period"]="2025-12-31"
    elif problem=="expensive": row["market_cap"]=row["revenue_ttm"]*10
    else: row["revenue_yoy"]=-1
    assert not loss_recovery_check(row,DEFAULTS,date(2026,10,15))["passed"]


@pytest.mark.parametrize("problem",["falling","below","few","not_consecutive","day_below","unversioned"])
def test_weekly_ma55_entry_gate_still_rejects_failures_regardless_of_score_weights(problem):
    technical=full_technical()
    assert evaluate(good_row(),technical,DEFAULTS,date(2026,10,15))["eligible"]
    reference=technical["timeframes"]["weekly"]["macro_profile"]["weekly_ma55"]
    if problem=="falling":reference["direction"]="falling"
    elif problem=="below":reference["latest_week_above"]=False
    elif problem=="few":reference["above_count"]=2
    elif problem=="not_consecutive":reference["consecutive_above"]=1
    elif problem=="day_below":technical["daily_close_qfq"]=reference["ma55_qfq"]
    else:reference.pop("version")
    result=evaluate(good_row(),technical,DEFAULTS,date(2026,10,15))
    assert not result["eligible"] and any("MA55" in reason for reason in result["reasons"])
    assert TECHNICAL_WEIGHTS=={"monthly":20,"weekly":30}


def test_model_config_ranges_and_old_review_cannot_bypass_new_gate():
    from tommy_capital.review import review_report
    from tommy_capital.data import SHANGHAI
    from test_balanced_top20 import report_fixture
    from datetime import datetime
    bad={**DEFAULTS,"weekly_ma55_min_consecutive":7}
    with pytest.raises(ValueError,match="MA55"):
        validate_config(bad)
    report=report_fixture(1);report["scoring_version"]="weekly_ma55_50_50_v7"
    with pytest.raises(ValueError,match="旧版源报告"):
        review_report(report,datetime(2026,10,15,20,tzinfo=SHANGHAI),"a"*64)


def test_latest_finance_builds_two_exact_same_period_bridges_and_rejects_nan_quality():
    import pandas as pd
    from tommy_capital.data import latest_finance
    rows=[]
    for day in ("2025-03-31","2025-06-30","2025-12-31","2026-03-31","2026-06-30"):
        rows.append({"code":"600000","period":pd.Timestamp(day),"profit_yoy":5.,"profit_ytd":-100. if day.startswith("2025") else -50.,
                     "core_profit_ytd":-120. if day.startswith("2025") else -60.,"revenue_ytd":1000.,"cfo_ytd":-30. if day.startswith("2025") else -10.,
                     "quality_statement_matched":True,"eps_ytd":-.1})
    data=pd.DataFrame(rows)
    result=latest_finance(data).iloc[0]
    assert [p["period"] for p in result.loss_recovery_periods]==["2026-03-31","2026-06-30"]
    assert all(p["quality_matched"] for p in result.loss_recovery_periods)
    data["quality_statement_matched"]=data["quality_statement_matched"].astype(object)
    data.loc[0,"quality_statement_matched"]=float("nan")
    assert latest_finance(data).iloc[0].loss_recovery_periods[0]["quality_matched"] is False


@pytest.mark.parametrize("missing",[None,float("nan"),{},["bad","bad"]])
def test_missing_or_left_joined_finance_object_never_aborts_full_market_scan(missing):
    row={**recovering(),"loss_recovery_periods":missing}
    routes=financial_routes(row,DEFAULTS,date(2026,10,15))
    assert not routes["route_b_passed"] and routes["route_b_loss_recovery"]["periods"]==[]
