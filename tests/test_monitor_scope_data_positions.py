"""Offline scope/privacy/session fixtures; no personal positions or live quotes."""
import copy
import json
from datetime import datetime

import pandas as pd
import pytest

from tommy_capital.data import DataError,SHANGHAI
from tommy_capital.monitor import monitor_scope,run_monitor,project_scope_result,merge_observed_bars
from tommy_capital.monitor_data import aggregate_120
from tommy_capital.positions import load_positions,save_position,update_profit_observation
from tommy_capital.cli import load_holdings
from tommy_capital.monitor_risk import resonance,holding_risk
from tommy_capital.macd_monitor import load_monitor_config
from test_volume_tracking_report import accumulated


def test_watch_threshold_ignore_hold_priority_dedup_and_delete_fallback():
    history=accumulated();code=history["sessions"][0]["top10"][0]["code"]
    report={"session":"2026-10-12","top10_history":history}
    prefs={"schema":1,"ignored_codes":[code]}
    assert monitor_scope(report=report,preferences=prefs)==[]
    scope=monitor_scope(report=report,preferences=prefs,positions=[{"code":code,"name":"本机持仓"},{"code":"000001","name":"独立持仓"}])
    assert len(scope)==2 and next(r for r in scope if r["code"]==code)["sources"]==["持仓"]
    both=monitor_scope(report=report,preferences={"schema":1,"ignored_codes":[]},positions=[{"code":code}])
    assert len(both)==1 and both[0]["sources"]==["Top10观察","持仓"]
    # Deleting a holding continues public watch if still qualified; both absent stops monitoring.
    assert len(monitor_scope(report=report,preferences={"schema":1,"ignored_codes":[]}))==1
    assert monitor_scope(report=report,preferences=prefs,positions=[])==[]
    assert report["top10_history"]==history


def session_frame():
    times=pd.to_datetime(["2026-09-30 10:30","2026-09-30 11:30","2026-09-30 14:00","2026-09-30 15:00",
                          "2026-10-08 10:30","2026-10-08 11:30","2026-10-08 14:00","2026-10-08 15:00"])
    return pd.DataFrame({"open":range(10,18),"close":range(11,19),"high":range(12,20),"low":range(9,17),"volume":100.},index=times)


def test_120_aggregation_never_crosses_lunch_holiday_or_day_and_excludes_forming_half():
    frame=session_frame();cal=pd.DataFrame({"trade_date":pd.to_datetime(["2026-09-30","2026-10-08","2026-10-09"])})
    result=aggregate_120(frame,cal,pd.Timestamp("2026-10-08 15:00"))
    assert result.index.strftime("%H:%M").tolist()==["11:30","15:00","11:30","15:00"]
    assert result.iloc[0].open==10 and result.iloc[0].close==12 and result.iloc[0].volume==200
    assert result.iloc[1].open==12 and result.iloc[1].close==14  # Afternoon is not joined to 11:30.
    partial=aggregate_120(frame.iloc[:-1],cal,pd.Timestamp("2026-10-08 11:30"))
    assert partial.index[-1]==pd.Timestamp("2026-10-08 11:30")
    with pytest.raises(DataError,match="缺少完整60分钟"):
        aggregate_120(frame.drop(pd.Timestamp("2026-10-08 14:00")),cal,pd.Timestamp("2026-10-08 15:00"))
    invalid=frame.copy();invalid.index=list(invalid.index[:-1])+[pd.Timestamp("2026-10-01 15:00")]
    with pytest.raises(DataError):
        aggregate_120(invalid.sort_index(),cal,pd.Timestamp("2026-10-08 15:00"))


def test_positions_crud_legacy_compatibility_compare_and_swap_and_optional_fields(tmp_path):
    file=tmp_path/"holdings.json"
    file.write_text('{"holdings":["000001"]}')
    assert load_holdings(file)=={"000001"} and load_positions(file)["holdings"][0]["cost"] is None
    changed=save_position(file,{"code":"600000","name":"代码持仓"},expected_revision=None)
    assert load_holdings(file)=={"000001","600000"}
    with pytest.raises(ValueError,match="其他窗口"):
        save_position(file,delete_code="000001",expected_revision=None)
    edited=save_position(file,{"code":"600000","name":"代码持仓","cost":"10.5","shares":"100"},expected_revision=changed["revision"])
    assert load_positions(file)["holdings"][-1]["shares"]==100
    save_position(file,delete_code="600000",expected_revision=edited["revision"])
    assert load_holdings(file)=={"000001"}
    with pytest.raises(ValueError):
        save_position(file,{"code":"000002","cost":-10},expected_revision=load_positions(file)["revision"])


def test_profit_maximum_starts_at_observation_not_unseen_purchase_history_and_cost_change_resets():
    now=datetime(2026,10,15,15,30,tzinfo=SHANGHAI)
    position={"code":"600000","cost":10.,"shares":100,"opened_on":"2025-01-01"}
    first=update_profit_observation(None,position,{"price":12.,"quote_at":"2026-10-15T15:00:00"},now)
    second=update_profit_observation(first,position,{"price":11.,"quote_at":"2026-10-16T15:00:00"},now)
    assert second["maximum_observed_profit_pct"]==pytest.approx(20)
    assert second["observed_profit_drawdown_percentage_points"]==pytest.approx(10)
    assert second["observation_started_at"]==now.isoformat()
    changed=update_profit_observation(second,{**position,"cost":11.},{"price":11.,"quote_at":"2026-10-16T15:00:00"},now)
    assert changed["maximum_observed_profit_pct"]==0
    assert "现金分红" in changed["price_basis"]


def test_scope_union_fetches_once_and_public_mode_refuses_personal_payload():
    calls=[]
    def loader(provider,code,now,calendar):
        calls.append(code)
        return {p:{"status":"unavailable","error":"offline fixture missing"} for p in ("daily","120","60","30","15")}
    class Provider:
        def fetch(self,*args,**kwargs):
            return pd.DataFrame([{"code":"600000","name":"离线测试","price":10.,"quote_at":"2026-10-15T15:00:00"}])
    scope=[{"code":"600000","name":"离线测试","sources":["Top10观察","持仓"]}]
    now=datetime(2026,10,15,16,tzinfo=SHANGHAI)
    cal=pd.DataFrame({"trade_date":pd.to_datetime(["2026-10-15","2026-12-31"])})
    result,_=run_monitor(scope,Provider(),now,cal,positions=[{"code":"600000"}],frame_loader=loader)
    assert calls==["600000"] and not result["monitor_complete"]
    assert all("无法判断" in f["label"] for f in result["rows"][0]["frames"].values())
    with pytest.raises(ValueError,match="个人持仓"):
        run_monitor(scope,public=True)
    with pytest.raises(ValueError,match="重复代码"):
        run_monitor(scope*2)
    assert project_scope_result(result,[])["rows"]==[]
    watch=project_scope_result(result,[{"code":"600000","sources":["Top10观察"]}])
    assert "holding_risk" not in watch["rows"][0] and "profit_observation" not in watch["rows"][0]


def frame(signals=(),status="ok"):
    return {"signals":list(signals),"status":status}


def test_hierarchical_resonance_conflict_and_15m_only_cannot_trigger_profit_reduction():
    top={"direction":"bearish","state":"confirmed"};bottom={"direction":"bullish","state":"confirmed"}
    frames={p:frame() for p in ("daily","120","60","30","15")}
    frames["15"]=frame([top])
    mixed=resonance(frames)
    risk=holding_risk(frames,{"material_advance":True},mixed,load_monitor_config())
    assert mixed["level"]=="local" and risk["partial_reduction_scenario"] is None
    frames["daily"]=frame([top]);frames["120"]=frame([top]);frames["60"]=frame([top])
    combined=resonance(frames)
    assert combined["level"]=="macro_resonance"
    risk=holding_risk(frames,{"material_advance":True,"short_support_lost":True},combined,load_monitor_config())
    assert risk["label"]=="价格结构确认转弱" and risk["orders_authorized"] is False
    frames["30"]=frame([bottom])
    assert resonance(frames)["label"]=="周期信号分歧"


def test_price_structure_does_not_invent_profit_trend_or_fundamental_deterioration():
    import numpy as np
    from tommy_capital.monitor_risk import price_structure
    close=np.r_[np.full(240,100.),np.linspace(101.,150.,59),149.9]
    daily=pd.DataFrame({"open":close,"close":close,"high":close+.2,"low":close-.2,"volume":1000.},
                       index=pd.bdate_range("2024-01-02",periods=300))
    daily.loc[daily.index[-1],"volume"]=5000.
    result=price_structure(daily,daily.index[-1].date(),load_monitor_config())
    assert result["material_advance"] and result["high_volume_stagnation"]
    assert "未知" in result["fundamental_risk"]
    daily.loc[:,["open","close","high","low"]]=np.column_stack([close[::-1],close[::-1],close[::-1]+.2,close[::-1]-.2])
    declining=price_structure(daily,daily.index[-1].date(),load_monitor_config())
    assert not declining["material_advance"] and not declining["high_volume_stagnation"]


def test_restored_stock_cannot_silently_join_unobserved_gap_or_use_stale_cache_as_current():
    data=session_frame()
    old={"bars":{t.isoformat():[float(row[k]) for k in ("open","high","low","close","volume")] for t,row in data.iloc[:3].iterrows()},
         "last_bar":data.index[2].isoformat()}
    with pytest.raises(DataError,match="跨缺口"):
        merge_observed_bars(data.iloc[5:],old)
    with pytest.raises(DataError,match="早于"):
        merge_observed_bars(data.iloc[:2],old)
    merged=merge_observed_bars(data.iloc[1:6],old)
    assert len(merged)==6 and merged.index[-1]==data.index[5]
