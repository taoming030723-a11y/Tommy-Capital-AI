"""Interactive report page hosted by the existing local Streamlit application."""
import streamlit as st

from .data import DataError
from .positions import code_value
from .report_controls import load_latest_view,save_control
from .tracking import apply_preferences
from .reporting import render_tracking_section


def render_latest_report(directory,controls_path,controls,preferences,positions,result,
                         lookup_name,render_results,render_holdings_editor,refresh,sync_root,alerts_only=False):
    def change(action,code):
        try:
            save_control(controls_path,action,code,expected_revision=controls["revision"])
            st.rerun()
        except (ValueError,OSError) as exc:
            st.error(str(exc))
    st.header("最新报告")
    sync_col,refresh_col=st.columns(2)
    if sync_col.button("同步最新报告",key="sync_report"):
        try:
            from .monitor_sync import sync_public_inputs
            root,commit=sync_public_inputs(sync_root)
            st.session_state["results_directory"]=str(root)
            st.session_state["public_commit"]=commit
            st.rerun()
        except (DataError,ValueError,OSError) as exc:
            st.error(str(exc))
    if refresh_col.button("刷新观察与持仓监控",key="refresh_report",type="primary"):
        try:
            with st.spinner("按当前报告设置刷新重点监控…"):
                st.session_state["monitor_result"]=refresh()
            st.rerun()
        except (DataError,ValueError,OSError) as exc:
            st.error(str(exc))
    try:
        view=load_latest_view(directory)
    except (ValueError,OSError) as exc:
        st.error("最新报告尚未核验："+str(exc))
        # Local lists remain editable when report synchronization is unavailable.
        view=None
    if view:
        summary=view["summary"]
        st.caption("市场报告数据日："+str(summary.get("session"))+"；原扫描完成："+str(summary.get("finished_at"))+
                   "；评分版本："+str(summary.get("scoring_version")))
        st.markdown(view["before_watch"])
        tracking=apply_preferences(summary.get("top10_tracking",{}),preferences) if preferences is not None else {
            **summary.get("top10_tracking",{}),"rows":[],"preferences_status":"invalid"}
        st.markdown("\n".join(render_tracking_section(tracking,summary.get("config"))))
        st.caption("以下按钮立即保存本机偏好，后续同步报告仍生效；云端设置使用表标题的忽略/恢复入口。持仓和手动观察独立保留。")
        columns=st.columns(4)
        for index,row in enumerate(tracking["rows"]):
            if columns[index%4].button("忽略 "+row["code"],key="ignore_"+row["code"],help=str(row.get("name") or row["code"])):
                change("ignore",row["code"])
    ignored=(preferences or {}).get("ignored_codes",[])
    with st.expander("已忽略股票／恢复观察",expanded=bool(ignored)):
        if ignored:
            selected=st.selectbox("选择要恢复的股票",ignored,key="restore_code")
            if st.button("恢复观察",key="restore_watch"):
                change("restore",selected)
        else:
            st.caption("没有已忽略股票。")
    st.subheader("手动添加观察股票")
    with st.form("manual_watch_form",clear_on_submit=True):
        code=st.text_input("手动观察代码（六位）",key="manual_watch_code")
        note=st.text_input("观察备注（可空，仅本机）",key="manual_watch_note")
        added=st.form_submit_button("加入观察")
    if added:
        try:
            code=code_value(code)
            try:
                name=lookup_name(code)
            except (DataError,ValueError,OSError):
                name=""
            save_control(controls_path,"add",code,name,note,controls["revision"])
            st.rerun()
        except (ValueError,OSError) as exc:
            st.error(str(exc))
    manual=controls["manual_watch"]
    if manual:
        monitored={r["code"]:r for r in (result or {}).get("rows",[])}
        st.dataframe([{"代码":r["code"],"名称":r["name"] or "名称待核验","备注":r["note"],
            "监控状态":monitored.get(r["code"],{}).get("combined",{}).get("label","已加入，等待刷新监控")} for r in manual],hide_index=True)
        columns=st.columns(4)
        for index,row in enumerate(manual):
            if columns[index%4].button("移除观察 "+row["code"],key="remove_manual_"+row["code"]):
                change("remove",row["code"])
    else:
        st.caption("尚未手动添加观察股。手动观察不等于实际持仓，不计Top10次数、不参与评分。")
    with st.expander("我的实际持仓（在报告内添加、修改、删除）"):
        render_holdings_editor(positions,None)
    if view:
        # Retain the original TOP20 reasons; MACD observations below use their own timestamps.
        reasons=view["after_watch"].partition("## 多周期MACD背离监控")[0].rstrip()
        if reasons:
            st.markdown(reasons)
    st.subheader("多周期MACD背离监控")
    if result:
        render_results(result,None,alerts_only)
    else:
        st.info("尚未刷新本机重点监控。添加或删除后，点击上方“刷新观察与持仓监控”。")
