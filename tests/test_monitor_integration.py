"""Focused-monitor integration cannot change research decisions or leak local positions."""
import copy
import json
import sys
from datetime import datetime

import pandas as pd
import pytest

from tommy_capital.data import SHANGHAI
from tommy_capital.monitor import main,run_monitor,validate_public_monitor
from tommy_capital.macd_monitor import load_monitor_config
from tommy_capital.cli import write_outputs,make_summary
from tommy_capital.watchlist import refreshed_entries
from tommy_capital.reporting import render_markdown
from test_watchlist_preferences import report_with_watch,preferences


def empty_frames():
    return {p:{"status":"unavailable","label":"数据不足，无法判断","signals":[],"error":"offline missing"} for p in ("daily","120","60","30","15")}


def public_result(code):
    from tommy_capital.monitor_risk import resonance
    scope=[{"code":code,"sources":["Top10观察"],"appearance_count":3}]
    return {"public":True,"checked_at":"2026-10-15T16:00:00+08:00","status":"partial","monitor_complete":False,"scope":scope,
        "rows":[{**scope[0],"frames":empty_frames(),"combined":resonance(empty_frames()),"data_complete":False}]}


def test_cli_attaches_independent_result_without_touching_market_counts_history_or_ranking(tmp_path,monkeypatch):
    report=report_with_watch()
    write_outputs(tmp_path,report)
    before=json.loads((tmp_path/"report.json").read_text())
    monkeypatch.setattr("tommy_capital.monitor.run_monitor",lambda *a,**k:(public_result(report["top10_tracking"]["rows"][0]["code"]),{"schema":1,"stocks":{}}))
    monkeypatch.setattr(sys,"argv",["monitor","--public","--research-report",str(tmp_path/"report.json"),"--out",str(tmp_path),"--state",str(tmp_path/"nonexistent.json")])
    main()
    after=json.loads((tmp_path/"report.json").read_text())
    for key,value in before.items():
        assert after[key]==value,key
    assert after["macd_monitor"]["monitor_complete"] is False
    assert json.loads((tmp_path/"summary.json").read_text())==make_summary(after)
    assert (tmp_path/"report.md").read_text()==render_markdown(after)
    assert json.loads((tmp_path/"nonexistent.json").read_text())=={"schema":1,"stocks":{}}


def test_ignore_restore_updates_monitor_view_but_preserves_all_confirmed_market_facts():
    report=report_with_watch();code=report["top10_tracking"]["rows"][0]["code"]
    report["macd_monitor"]=public_result(code)
    summary=make_summary(report);markdown=render_markdown(report)
    now=datetime(2026,10,15,21,tzinfo=SHANGHAI)
    entries=refreshed_entries(summary,markdown,summary,markdown,preferences(code),now,"owner")
    changed=json.loads(entries[0]["content"])
    assert changed["macd_monitor"]["rows"]==[]
    assert changed["macd_monitor"]["all_rows"]==summary["macd_monitor"]["rows"]
    for key in summary:
        if key not in ("macd_monitor","top10_tracking"):
            assert changed[key]==summary[key]
    updated_md=next(e["content"] for e in entries if e["path"]=="results/latest-report.md")
    restored=refreshed_entries(changed,updated_md,changed,updated_md,preferences(),now,"owner")
    restored_row=json.loads(restored[0]["content"])["macd_monitor"]["rows"][0]
    for key,value in summary["macd_monitor"]["rows"][0].items():
        assert restored_row[key]==value


@pytest.mark.parametrize("key",["cost","shares","note","profit_observation","holding_risk"])
def test_public_publication_guard_rejects_all_personal_fields(key):
    result=public_result("600000")
    validate_public_monitor(result,{"schema":1,"stocks":{}})
    result["rows"][0][key]="private fixture"
    with pytest.raises(ValueError,match="个人"):
        validate_public_monitor(result)
    with pytest.raises(ValueError,match="私有"):
        validate_public_monitor({"public":False,"rows":[]})


def test_changed_thresholds_freeze_existing_events_and_stale_profit_quote_is_not_recorded():
    config=load_monitor_config();changed=copy.deepcopy(config);changed["hist_peak_reduction_pct"]=30
    from tommy_capital.macd_monitor import identity
    state={"schema":1,"stocks":{},"configuration_id":identity(config)}
    result,after=run_monitor([],config=changed,state=state)
    assert result["status"]=="configuration_changed" and after==state
    class Provider:
        def fetch(self,*a,**k):
            return pd.DataFrame([{"price":12.,"quote_at":"2026-10-14 15:00:00"}])
    now=datetime(2026,10,15,11,tzinfo=SHANGHAI)
    scope=[{"code":"600000","sources":["持仓"]}]
    result,state=run_monitor(scope,Provider(),now,pd.DataFrame({"trade_date":pd.to_datetime(["2026-10-14","2026-10-15","2026-12-31"])}),
        positions=[{"code":"600000","cost":10.}],frame_loader=lambda *a:empty_frames())
    assert result["rows"][0]["profit_observation"]["status"]=="unavailable"
    assert "profit_observation" not in state["stocks"]["600000"]


def test_monitor_config_strict_threshold_cannot_be_lowered_and_nonfinite_is_rejected(tmp_path):
    config=load_monitor_config();config["strict"]["macd_min_dif_improvement_atr"]=0.
    path=tmp_path/"monitor.json";path.write_text(json.dumps(config))
    with pytest.raises(ValueError,match="不得弱于"):
        load_monitor_config(path)
    config=load_monitor_config();config["risk"]["high_volume_ratio"]=float("nan")
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError,match="有限正数"):
        load_monitor_config(path)
