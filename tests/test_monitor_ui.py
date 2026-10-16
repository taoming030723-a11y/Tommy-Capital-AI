"""Real Streamlit AppTest widgets, with deterministic name/monitor adapters only."""
import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from tommy_capital.positions import load_positions
from tommy_capital.monitor import archive_local_state

ROOT=Path(__file__).resolve().parents[1]


def button(app,label):
    return next(b for b in app.button if b.label==label)


def test_ui_pages_code_only_add_edit_delete_and_alert_filter(tmp_path,monkeypatch):
    positions=tmp_path/"holdings.json"
    monkeypatch.setenv("TOMMY_HOLDINGS_PATH",str(positions))
    monkeypatch.setenv("TOMMY_MONITOR_STATE",str(tmp_path/"monitor"))
    monkeypatch.setenv("TOMMY_RESULTS_DIRECTORY",str(ROOT/"results"))
    from tommy_capital.data import Provider
    import pandas as pd
    def quote(self,function,**kwargs):
        assert function=="quote_tx_one"
        return pd.DataFrame([{"name":"离线界面测试"}])
    monkeypatch.setattr(Provider,"fetch",quote)
    app=AppTest.from_file(str(ROOT/"monitor_app.py"),default_timeout=15).run()
    assert not app.exception
    app.radio[0].set_value("我的实际持仓").run()
    next(t for t in app.text_input if t.label=="六位A股代码").set_value("600000").run()
    assert not app.exception
    button(app,"保存持仓").click().run()
    assert not app.exception
    assert load_positions(positions)["holdings"][0]["cost"] is None
    app.selectbox[0].set_value("600000").run()
    next(t for t in app.text_input if t.label=="持仓成本（可空）").set_value("10.50")
    next(t for t in app.text_input if t.label=="股数（可空）").set_value("100")
    button(app,"保存持仓").click().run()
    assert not app.exception
    row=load_positions(positions)["holdings"][0]
    assert row["cost"]==10.5 and row["shares"]==100 and row["name"]=="离线界面测试"
    app.selectbox[0].set_value("新增").run()
    next(t for t in app.text_input if t.label=="六位A股代码").set_value("600000").run()
    button(app,"保存持仓").click().run()
    assert app.error and load_positions(positions)["holdings"][0]["cost"]==10.5
    app.selectbox[0].set_value("600000").run()
    next(c for c in app.checkbox if c.label=="只看出现警报的股票").check().run()
    button(app,"删除已清仓股票").click().run()
    assert not app.exception and load_positions(positions)["holdings"]==[]


def test_archive_keeps_prior_event_history_and_removes_current_basis(tmp_path):
    (tmp_path/"state.json").write_text('{"schema":1,"stocks":{}}')
    (tmp_path/"latest.json").write_text('{"rows":[]}')
    archive=archive_local_state(tmp_path)
    assert json.loads((archive/"state.json").read_text())["schema"]==1
    assert (archive/"latest.json").exists() and not (tmp_path/"state.json").exists()


def test_latest_report_contains_ignore_restore_manual_watch_holdings_and_refresh(tmp_path,monkeypatch):
    from tommy_capital.cli import make_summary,clean
    from tommy_capital.reporting import render_markdown
    from test_watchlist_preferences import report_with_watch
    import pandas as pd
    root=tmp_path/"results";root.mkdir()
    report=report_with_watch()
    (root/"latest-summary.json").write_text(json.dumps(clean(make_summary(report))))
    (root/"latest-report.md").write_text(render_markdown(report))
    (root/"top10-history.json").write_text(json.dumps(report["top10_history"]))
    holdings,controls=tmp_path/"holdings.json",tmp_path/"report-controls.json"
    monkeypatch.setenv("TOMMY_HOLDINGS_PATH",str(holdings))
    monkeypatch.setenv("TOMMY_REPORT_CONTROLS",str(controls))
    monkeypatch.setenv("TOMMY_RESULTS_DIRECTORY",str(root))
    monkeypatch.setenv("TOMMY_MONITOR_STATE",str(tmp_path/"monitor"))
    from tommy_capital.data import Provider
    monkeypatch.setattr(Provider,"fetch",lambda *a,**kw:pd.DataFrame([{"name":"离线界面测试"}]))
    calls=[]
    def offline(scope,**kwargs):
        calls.append(scope)
        frames={p:{"status":"unavailable","label":"离线界面测试，无法判断","signals":[]} for p in ("daily","120","60","30","15")}
        rows=[{**r,"frames":frames,"combined":{"label":"数据未齐"},"data_complete":False} for r in scope]
        return {"rows":rows,"checked_at":"offline test","monitor_complete":False},{"schema":1,"stocks":{}}
    monkeypatch.setattr("tommy_capital.monitor.run_monitor",offline)
    app=AppTest.from_file(str(ROOT/"monitor_app.py"),default_timeout=15).run()
    assert not app.exception and app.radio[0].value=="最新报告"
    code=report["top10_tracking"]["rows"][0]["code"]
    button(app,"忽略 "+code).click().run()
    assert not app.exception and json.loads(controls.read_text())["ignored_codes"]==[code]
    button(app,"恢复观察").click().run()
    assert not app.exception and not json.loads(controls.read_text())["ignored_codes"]
    next(t for t in app.text_input if t.label=="手动观察代码（六位）").set_value("000001")
    button(app,"加入观察").click().run()
    assert not app.exception and json.loads(controls.read_text())["manual_watch"][0]["code"]=="000001"
    # Actual holdings are editable within this same report page too.
    next(t for t in app.text_input if t.label=="六位A股代码").set_value(code).run()
    button(app,"保存持仓").click().run()
    assert not app.exception and load_positions(holdings)["holdings"][0]["code"]==code
    button(app,"刷新观察与持仓监控").click().run()
    assert not app.exception and len(calls)==1 and len(calls[0])==2
    assert next(r for r in calls[0] if r["code"]==code)["sources"]==["Top10观察","持仓"]
    button(app,"移除观察 000001").click().run()
    assert not app.exception and not json.loads(controls.read_text())["manual_watch"]
    # Both the original holdings page and the integrated report can filter loaded results.
    app.radio[0].set_value("我的实际持仓").run()
    assert not app.exception
    next(c for c in app.checkbox if c.label=="只看出现警报的股票").check().run()
    assert not app.exception and any("暂无符合当前筛选条件" in i.value for i in app.info)
    app.radio[0].set_value("最新报告").run()
    assert not app.exception and any("暂无符合当前筛选条件" in i.value for i in app.info)
    again=AppTest.from_file(str(ROOT/"monitor_app.py"),default_timeout=15).run()
    assert not again.exception and again.radio[0].value=="最新报告"
