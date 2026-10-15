"""Local Windows-compatible positions and focused MACD monitor."""
import json
import os
from pathlib import Path

import streamlit as st

from tommy_capital.data import Provider, DataError
from tommy_capital.positions import load_positions, save_position
from tommy_capital.monitor import monitor_scope, refresh_local, project_scope_result, archive_local_state

ROOT=Path(__file__).resolve().parent
HOLDINGS=Path(os.environ.get("TOMMY_HOLDINGS_PATH",str(ROOT/"holdings.json")))
STATE=Path(os.environ.get("TOMMY_MONITOR_STATE",str(ROOT/".tommy/monitor")))
RESULTS=Path(os.environ.get("TOMMY_RESULTS_DIRECTORY",str(ROOT/"results")))


def lookup_name(code):
    return Provider().fetch("quote_tx_one",ttl=3600,symbol=code).iloc[0]["name"]


def render_results(result,role,alerts_only):
    if result.get("error") or result.get("scope_warning"):
        st.warning(result.get("error") or result["scope_warning"])
    rows=[r for r in result.get("rows",[]) if role in r.get("sources",[])]
    if alerts_only:
        rows=[r for r in rows if any(f.get("signals") for f in r["frames"].values()) or
              r.get("holding_risk",{}).get("label") not in (None,"正常持有观察")]
    st.caption("真实检查时间："+str(result.get("checked_at","尚未刷新"))+"；行情时间以各周期最后完整K线为准。")
    if not rows:
        st.info("暂无符合当前筛选条件的监控结果。")
        return
    st.dataframe([{"股票":str(r.get("name") or "名称待核验")+"（"+r["code"]+"）","身份":"／".join(r["sources"]),
        **{label:r["frames"].get(p,{}).get("label","数据不足，无法判断") for p,label in
           (("daily","日线"),("120","120分钟"),("60","60分钟"),("30","30分钟"),("15","15分钟"))},
        "综合状态":r.get("holding_risk",{}).get("label") or r["combined"]["label"],
        "数据完整":r["data_complete"]} for r in rows],hide_index=True)
    for row in rows:
        with st.expander(str(row.get("name") or row["code"])+" · 原因、枢轴与事件历史"):
            if row.get("holding_risk"):
                st.write(row["holding_risk"]["label"]+"："+"；".join(row["holding_risk"]["reasons"]))
                st.write(row["holding_risk"]["message"])
                st.json({"价格结构事实":row.get("price_structure",{})},expanded=False)
            st.write("共振："+row["combined"]["label"])
            for period,frame in row["frames"].items():
                st.markdown("**"+period+"："+frame.get("label","未检查")+"**")
                st.caption("行情截止："+str(frame.get("last_bar") or frame.get("source_last_bar") or "缺失")+"；来源："+frame.get("source","未取得"))
                if frame.get("error"):
                    st.warning(frame["error"])
                for signal in frame.get("signals",[]):
                    st.json(signal,expanded=False)
                if frame.get("event_history") or frame.get("inactive_signals"):
                    st.json({"事件历史":frame.get("event_history",[]),"已失效或过期":frame.get("inactive_signals",[])},expanded=False)
            profit=row.get("profit_observation")
            if profit and profit.get("status")=="ok":
                st.write(f"当前浮盈 {profit['profit_pct']:.2f}%；已观察最高浮盈 {profit['maximum_observed_profit_pct']:.2f}%；回撤 {profit['observed_profit_drawdown_percentage_points']:.2f}个百分点")
                st.caption("记录起点："+profit["observation_started_at"]+"。"+profit["price_basis"]+"。"+profit["warning"])
            st.json(row.get("new_events",[]),expanded=False)


def main():
    st.set_page_config(page_title="Tommy Capital 持仓与MACD监控",layout="wide")
    st.title("Tommy Capital · 持仓与MACD监控")
    st.caption("EMA12/26、DEA9；MACD柱=2×(DIF−DEA)。成本、数量、备注与持仓事件仅保存在本机，无自动下单。")
    page=st.sidebar.radio("页面",["持续Top10观察池","我的实际持仓"])
    if st.sidebar.button("同步公开观察名单与忽略设置"):
        try:
            from tommy_capital.monitor_sync import sync_public_inputs
            directory,commit=sync_public_inputs(ROOT/".tommy/public-inputs")
            st.session_state["results_directory"]=str(directory)
            st.session_state["public_commit"]=commit
            st.success("公开名单已同步；本机持仓未上传。")
        except (ValueError,OSError,DataError) as exc:
            st.error(str(exc))
    results_directory=Path(st.session_state.get("results_directory",str(RESULTS)))
    if st.session_state.get("public_commit"):
        st.sidebar.caption("公开名单版本："+st.session_state["public_commit"][:12])
    if st.sidebar.button("一键刷新全部重点监控",type="primary"):
        try:
            with st.spinner("仅获取观察股与持仓股的完整五周期行情…"):
                st.session_state["monitor_result"]=refresh_local(HOLDINGS,results_directory,STATE)
        except (ValueError,OSError,DataError) as exc:
            st.error(str(exc))
    alerts_only=st.sidebar.checkbox("只看出现警报的股票")
    with st.sidebar.expander("历史复权或配置变化后的状态重建"):
        st.caption("先核对数据变化。重建会保留旧事件归档，从新数据起点重新观察，不改写历史确认。")
        rebuild=st.checkbox("已核对变化，归档并重新建立本机监控状态")
        if st.button("归档旧监控状态",disabled=not rebuild):
            try:
                archive_local_state(STATE)
                st.session_state.pop("monitor_result",None)
                st.success("旧事件已归档；请重新刷新监控。")
            except (ValueError,OSError) as exc:
                st.error(str(exc))
    result=st.session_state.get("monitor_result")
    if result is None and (STATE/"latest.json").exists():
        result=json.loads((STATE/"latest.json").read_text())
    try:
        positions=load_positions(HOLDINGS)
    except (ValueError,OSError) as exc:
        st.error(str(exc));return
    try:
        preference_path=results_directory.parent/"operations/watchlist-preferences.json" if results_directory==RESULTS else results_directory/"watchlist-preferences.json"
        from tommy_capital.tracking import load_preferences
        current_scope=monitor_scope(results_directory,positions["holdings"],preferences=load_preferences(preference_path))
        if result:
            result=project_scope_result(result,current_scope)
    except (OSError,ValueError):
        current_scope=[{"code":p["code"],"name":p["name"],"sources":["持仓"]} for p in positions["holdings"]]
        if result:
            result=project_scope_result(result,current_scope)
    if page=="持续Top10观察池":
        st.header("持续Top10观察池")
        try:
            preference_path=results_directory.parent/"operations/watchlist-preferences.json" if results_directory==RESULTS else results_directory/"watchlist-preferences.json"
            from tommy_capital.tracking import load_preferences
            scope=monitor_scope(results_directory,positions["holdings"],preferences=load_preferences(preference_path))
            watch=[r for r in scope if "Top10观察" in r["sources"]]
            st.dataframe([{"代码":r["code"],"名称":r.get("name"),"累计正式收盘Top10次数":r["appearance_count"],"身份":"／".join(r["sources"])} for r in watch],hide_index=True)
            st.caption("累计三个不同正式收盘日，不要求连续；已忽略观察股退出此池，实际持仓仍独立监控。")
        except (OSError,ValueError) as exc:
            st.warning("公开累计历史尚未核验："+str(exc))
        if result:
            render_results(result,"Top10观察",alerts_only)
        return
    st.header("我的实际持仓")
    st.dataframe(positions["holdings"],hide_index=True)
    if st.session_state.pop("delete_complete",False):
        st.session_state.pop("position_selected",None)
        st.session_state.pop("editor_selected",None)
    choices=["新增"]+[p["code"] for p in positions["holdings"]]
    selected=st.selectbox("添加或选择要编辑的持仓",choices,key="position_selected")
    row=next((p for p in positions["holdings"] if p["code"]==selected),{})
    if st.session_state.get("editor_selected")!=selected:
        st.session_state["editor_selected"]=selected
        st.session_state["editor_revision"]=positions["revision"]
    code=st.text_input("六位A股代码",value=row.get("code",""),disabled=selected!="新增",key="code_"+selected)
    name=row.get("name","")
    if len(code)==6 and code.isascii() and code.isdigit():
        if code not in st.session_state.get("resolved_names",{}):
            try:
                name=lookup_name(code)
                st.session_state.setdefault("resolved_names",{})[code]=name
            except (ValueError,DataError,OSError) as exc:
                st.warning("名称接口暂未核验；可仅保存代码继续监控。"+str(exc))
        name=st.session_state.get("resolved_names",{}).get(code,name)
    st.write("股票名称："+(name or "等待六位代码/名称核验"))
    with st.form("position_form_"+selected):
        cost=st.text_input("持仓成本（可空）",value=str(row.get("cost") or ""))
        shares=st.text_input("股数（可空）",value=str(row.get("shares") or ""))
        opened=st.text_input("建仓日期 YYYY-MM-DD（可空）",value=row.get("opened_on") or "")
        note=st.text_area("备注（仅本机）",value=row.get("note", ""))
        submitted=st.form_submit_button("保存持仓",type="primary")
    if submitted:
        try:
            if selected=="新增" and any(p["code"]==code for p in positions["holdings"]):
                raise ValueError("该代码已在持仓中，请选择已有代码后编辑，未覆盖原成本和股数")
            updated=save_position(HOLDINGS,{"code":code,"name":name,"cost":cost,"shares":shares,"opened_on":opened,"note":note},expected_revision=st.session_state["editor_revision"])
            st.session_state["editor_revision"]=updated["revision"]
            st.success("持仓已保存在本机；可点击一键刷新。")
            st.rerun()
        except (ValueError,OSError) as exc:
            st.error(str(exc))
    if selected!="新增" and st.button("删除已清仓股票"):
        try:
            save_position(HOLDINGS,delete_code=selected,expected_revision=st.session_state["editor_revision"])
            st.session_state["delete_complete"]=True
            st.rerun()
        except (ValueError,OSError) as exc:
            st.error(str(exc))
    if st.button("重新加载持仓编辑版本"):
        st.session_state["editor_revision"]=positions["revision"]
        st.rerun()
    if result:
        current_codes={p["code"] for p in positions["holdings"]}
        private_view={**result,"rows":[r for r in result.get("rows",[]) if r["code"] in current_codes]}
        render_results(private_view,"持仓",alerts_only)


if __name__=="__main__":
    main()
