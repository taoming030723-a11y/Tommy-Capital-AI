"""Small-universe monitor CLI and local GUI service. No ranking mutations."""
import argparse
import copy
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd

from .data import DataError, Provider, SHANGHAI
from .tracking import load_history, validate_history, tracking_view, load_preferences
from .positions import load_positions, locked, update_profit_observation
from .macd_monitor import PERIODS, load_monitor_config, replay_frame, identity
from .monitor_data import acquire_frames
from .monitor_risk import resonance, price_structure, holding_risk
from .kd_state import observe_acquired, combine_kd


def monitor_scope(results_directory="results", positions=None, report=None, preferences=None, manual_watch=None):
    report=report or json.loads((Path(results_directory)/"latest-summary.json").read_text())
    if report.get("top10_history") is not None:
        history=validate_history(report["top10_history"])
    else:
        history,issues=load_history(results_directory,report.get("session") or "0000-00-00")
        if issues:
            raise ValueError("累计历史未核验，不能猜测重点监控范围："+"；".join(issues))
    preferences=load_preferences() if preferences is None else preferences
    watch=tracking_view({"session":report.get("session"),"rankings":[],"observations":[]},history,preferences)
    scope={r["code"]:{"code":r["code"],"name":r.get("name"),"sources":["Top10观察"],"appearance_count":r["appearance_count"]} for r in watch["rows"]}
    for observation in manual_watch or []:
        row=scope.setdefault(observation["code"],{"code":observation["code"],"name":observation.get("name"),"sources":[]})
        row["sources"].append("手动观察")
        if observation.get("name"):
            row["name"]=observation["name"]
    for position in positions or []:
        code=position["code"]
        row=scope.setdefault(code,{"code":code,"name":position.get("name"),"sources":[]})
        row["sources"].append("持仓")
        if position.get("name"):
            row["name"]=position["name"]
    return [scope[k] for k in sorted(scope)]


def merge_observed_bars(frame, previous):
    observed=previous.get("bars",{}) if previous else {}
    if not observed:
        return frame
    last=pd.Timestamp(previous["last_bar"])
    if frame.empty or frame.index[-1]<last:
        raise DataError("新行情早于上次已完成K线，不能沿用旧状态冒充更新")
    if last not in frame.index:
        raise DataError("新行情窗口未覆盖上次已观察K线，不能跨缺口拼接；核对后归档并重建本段状态")
    old=pd.DataFrame.from_dict(observed,orient="index",columns=["open","high","low","close","volume"])
    old.index=pd.to_datetime(old.index)
    # Freeze the original EMA start; new older history cannot rewrite old indicators.
    incoming=frame[frame.index>=old.index.min()]
    merged=pd.concat([old,incoming]).loc[lambda x:~x.index.duplicated(keep="last")].sort_index()
    return merged


def run_monitor(scope, provider=None, now=None, calendar=None, state=None, config=None, positions=None, public=False, frame_loader=acquire_frames):
    if public and (positions or any(set(item.get("sources",[]))&{"持仓","手动观察"} for item in scope)):
        raise ValueError("公开云端监控禁止加载个人持仓")
    if len({r["code"] for r in scope})!=len(scope):
        raise ValueError("重点监控范围含重复代码，拒绝重复抓取")
    provider,now,config=provider or Provider(),now or datetime.now(SHANGHAI),config or load_monitor_config()
    state=copy.deepcopy(state or {"schema":1,"stocks":{}})
    if not isinstance(state,dict) or type(state.get("schema")) is not int or state["schema"]!=1 or not isinstance(state.get("stocks"),dict):
        raise ValueError("监控状态版本不匹配")
    if public and any("profit_observation" in value for value in state.get("stocks",{}).values()):
        raise ValueError("私有浮盈状态禁止用于公开云端监控")
    if public and state.get("visibility")=="local_private":
        raise ValueError("个人本机监控事件状态禁止用于公开云端")
    fingerprint=identity(config)
    if state.get("configuration_id",fingerprint)!=fingerprint:
        return {"schema":1,"checked_at":now.isoformat(),"public":public,"status":"configuration_changed",
                "error":"配置已变更；请先归档旧事件并建立独立状态，不改写旧确认信号", "scope":scope,"rows":[],"monitor_complete":False},state
    state["configuration_id"]=fingerprint
    state["visibility"]="public_watch" if public else "local_private"
    rows=[]
    try:
        calendar=calendar if calendar is not None else provider.fetch("tool_trade_date_hist_sina",ttl=86400)
        if "trade_date" not in calendar or calendar.empty:
            raise DataError("真实交易日历缺失")
        if pd.to_datetime(calendar.trade_date).dt.date.max()<now.date():
            raise DataError("真实交易日历未覆盖检查日期")
    except (DataError,ValueError) as exc:
        return {"schema":1,"checked_at":now.isoformat(),"public":public,"status":"unavailable","error":str(exc),"rows":[],"scope":scope,"monitor_complete":False},state
    position_map={p["code"]:p for p in positions or []}
    for item in scope:
        code=item["code"]
        old=state["stocks"].setdefault(code,{"frames":{}})
        results={}
        structure={"fundamental_risk":"未知；未取得已验证的新基本面风险"}
        quote=None
        kd_monitor=combine_kd({})
        try:
            acquired=frame_loader(provider,code,now,calendar)
            kd_reused={}
            for period in PERIODS:
                source=acquired.get(period,{"status":"unavailable","error":"数据源未返回此周期"})
                if source["status"]!="ok":
                    results[period]={**source,"period":period,"label":"数据不足，无法判断","signals":[],"events":[]};continue
                try:
                    frame=merge_observed_bars(source["frame"],old["frames"].get(period))
                except DataError as exc:
                    results[period]={"status":"history_gap_or_stale","period":period,"label":"数据不足，无法判断；历史连续性未通过",
                        "error":str(exc),"source":source["source"],"source_last_bar":source["cutoff"],"signals":[],"events":[]}
                    continue
                result,updated=replay_frame(frame,code,period,config,old["frames"].get(period))
                # Historical first-computable bar and actual first observation are separate clocks.
                for event in updated["events"].values():
                    event.setdefault("first_observed_at",now.isoformat())
                    if event.get("confirmed_at"):
                        event.setdefault("confirmation_observed_at",now.isoformat())
                    for evidence in event.get("additional_confirmations",[]):
                        evidence.setdefault("observed_at",now.isoformat())
                for transition in updated["transitions"]:
                    transition.setdefault("observed_at",now.isoformat())
                old["frames"][period]=updated
                results[period]={**result,"source":source["source"],"source_last_bar":source["cutoff"],"checked_at":now.isoformat()}
                if result.get("status")=="ok":
                    kd_reused[period]=frame
                else:
                    acquired[period]={"status":"unavailable","error":"原监控历史一致性未通过，K/D也保持未知"}
            if acquired["daily"]["status"]=="ok" and len(acquired["daily"]["frame"])>=60:
                structure=price_structure(acquired["daily"]["frame"],now.date(),config)
            from .data import completed_session
            kd_monitor=observe_acquired(acquired,completed_session(calendar,now),kd_reused)
        except (DataError,ValueError,KeyError) as exc:
            results={p:{"status":"unavailable","label":"数据不足，无法判断","error":str(exc),"signals":[],"events":[]} for p in PERIODS}
        combined=resonance(results)
        row={**item,"frames":results,"combined":combined,"price_structure":structure,
             "data_complete":all(f["status"]=="ok" for f in results.values()),"kd_monitor":kd_monitor}
        if code in position_map:
            row["holding_risk"]=holding_risk(results,structure,combined,config)
            try:
                if position_map[code].get("cost") is None:
                    raise DataError("未填写成本：技术监控正常；不计算浮盈")
                quote=provider.fetch("quote_tx_one",ttl=30,symbol=code).iloc[0].to_dict()
                quote_at=pd.Timestamp(quote["quote_at"])
                # An old quote cannot create a current profit observation.
                from .data import completed_session
                days=set(pd.to_datetime(calendar.trade_date).dt.date)
                expected=now.date() if now.date() in days and now.hour*60+now.minute>=570 else completed_session(calendar,now)
                quote_at=quote_at.tz_localize(SHANGHAI) if quote_at.tzinfo is None else quote_at.tz_convert(SHANGHAI)
                if quote_at.date()!=expected or quote_at>pd.Timestamp(now)+pd.Timedelta(minutes=1):
                    raise DataError("报价时间未通过核验，不更新浮盈记录")
                row["profit_observation"]=update_profit_observation(old.get("profit_observation"),position_map[code],quote,now)
                old["profit_observation"]=row["profit_observation"]
            except (DataError,ValueError,KeyError) as exc:
                row["profit_observation"]={"status":"unavailable","reason":str(exc)}
        new_events=[{**e,"period":p} for p,f in results.items() for e in f.get("events",[]) if e["occurred_at"]==f.get("last_bar")]
        row["new_events"]=new_events
        row["alerts_new"]=[e for e in new_events if e["state"] in ("suspected","confirmed")]
        rows.append(row)
    return {"schema":1,"checked_at":now.isoformat(),"finished_at":datetime.now(SHANGHAI).isoformat(),"public":public,"status":"complete" if all(r["data_complete"] for r in rows) else "partial",
        "monitor_complete":all(r["data_complete"] for r in rows),"scope":scope,"rows":rows,
        "formula":"EMA12/EMA26，DEA9；MACD柱=2×(DIF−DEA)","orders_authorized":False},state


def atomic_json(path,value):
    file=Path(path);file.parent.mkdir(parents=True,exist_ok=True)
    temporary=file.with_name(file.name+"."+uuid.uuid4().hex+".tmp")
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    try:
        os.chmod(temporary,0o600)
    except OSError:
        pass
    temporary.replace(file)


def validate_public_monitor(result,state=None):
    if result is None:
        return
    if result.get("public") is not True:
        raise ValueError("私有MACD监控结果禁止发布到公开仓库")
    forbidden={"cost","shares","note","opened_on","position_basis","profit_observation","holding_risk"}
    def inspect(value):
        if isinstance(value,dict):
            if forbidden.intersection(value) or set(value.get("sources",[]))&{"持仓","手动观察"}:
                raise ValueError("公开监控含个人持仓/浮盈信息，拒绝发布")
            for child in value.values():
                inspect(child)
        elif isinstance(value,list):
            for child in value:
                inspect(child)
    inspect(result)
    if state is not None:
        if state.get("visibility")=="local_private":
            raise ValueError("个人本机监控事件状态禁止公开发布")
        inspect(state)


def archive_local_state(directory):
    """Explicitly preserve old observations before rebuilding a changed price basis/config."""
    directory=Path(directory)
    with locked(directory/"refresh"):
        archive=directory/"archives"/(datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S")+"-"+uuid.uuid4().hex[:8])
        archive.mkdir(parents=True)
        for name in ("state.json","latest.json"):
            path=directory/name
            if path.exists():
                path.replace(archive/name)
    return archive


def project_scope_result(result,scope):
    current={r["code"]:r for r in scope}
    rows=[]
    for row in result.get("rows",[]):
        if row["code"] not in current:
            continue
        row={**row,"sources":current[row["code"]]["sources"]}
        if "持仓" not in row["sources"]:
            row.pop("holding_risk",None);row.pop("profit_observation",None)
        rows.append(row)
    return {**result,"scope":scope,"rows":rows}


def refresh_local(holdings_path="holdings.json",results_directory="results",state_directory=".tommy/monitor",provider=None,now=None,controls_path=None):
    from .report_controls import load_controls,effective_preferences,public_preferences
    positions=load_positions(holdings_path)["holdings"]
    controls=load_controls(controls_path or Path(state_directory).parent/"report-controls.json")
    scope_error=None
    try:
        preferences=effective_preferences(public_preferences(results_directory),controls)
        scope=monitor_scope(results_directory,positions,preferences=preferences,manual_watch=controls["manual_watch"])
    except (OSError,ValueError) as exc:
        scope_error=str(exc)
        manual={p["code"]:{"code":p["code"],"name":p.get("name"),"sources":["手动观察"]} for p in controls["manual_watch"]}
        for p in positions:
            manual.setdefault(p["code"],{"code":p["code"],"name":p.get("name"),"sources":[]})["sources"].append("持仓")
        scope=[manual[code] for code in sorted(manual)]
    root=Path(state_directory);root.mkdir(parents=True,exist_ok=True)
    with locked(root/"refresh"):
        path=root/"state.json"
        state=json.loads(path.read_text()) if path.exists() else None
        result,state=run_monitor(scope,provider=provider,now=now,state=state,positions=positions)
        if scope_error:
            result.update(scope_warning="公开累计观察范围未核验，仅监控本机手动观察与明确持仓："+scope_error,
                          monitor_complete=False,status="partial")
        atomic_json(path,state);atomic_json(root/"latest.json",result)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--public",action="store_true")
    parser.add_argument("--holdings")
    parser.add_argument("--research-report")
    parser.add_argument("--results-directory",default="results")
    parser.add_argument("--out",default=".tommy/monitor")
    parser.add_argument("--state")
    parser.add_argument("--config",default="monitor-config.json")
    args=parser.parse_args()
    if args.public and args.holdings:
        parser.error("公开模式禁止读取个人持仓；本机持仓不会自动同步云端")
    report=json.loads(Path(args.research_report).read_text()) if args.research_report else None
    if report and report.get("status") not in ("complete","partial"):
        print("研究扫描未发布有效结果，跳过重点监控");return
    positions=load_positions(args.holdings or "holdings.json")["holdings"] if not args.public else []
    scope=[]
    state_path=Path(args.state or (Path(args.out)/"state.json"))
    state_path.parent.mkdir(parents=True,exist_ok=True)
    with locked(state_path.parent/"refresh"):
        state=json.loads(state_path.read_text()) if state_path.exists() else None
        try:
            scope=monitor_scope(args.results_directory,positions,report=report)
            result,state=run_monitor(scope,state=state,positions=positions,public=args.public,config=load_monitor_config(args.config))
        except (DataError,ValueError,OSError) as exc:
            # A focused-data/config failure must remain visible without erasing a valid market scan.
            result={"schema":1,"checked_at":datetime.now(SHANGHAI).isoformat(),"public":args.public,
                    "status":"unavailable","error":str(exc),"scope":scope,"rows":[],"monitor_complete":False}
            state=state or {"schema":1,"stocks":{}}
        atomic_json(Path(args.out)/"monitor.json",result)
        atomic_json(Path(args.out)/"monitor-state.json",state)
        if args.public:
            validate_public_monitor(result,state)
        atomic_json(state_path,state)
    if report is not None:
        from .cli import write_outputs,make_summary
        original=copy.deepcopy(report["rankings"])
        report["macd_monitor"]=result
        assert report["rankings"]==original
        write_outputs(Path(args.out),report)
        atomic_json(Path(args.out)/"summary.json",make_summary(report))
    print(json.dumps({"monitor_stock_count":len(scope),"status":result["status"],"monitor_complete":result["monitor_complete"]},ensure_ascii=False))


if __name__=="__main__":
    main()
