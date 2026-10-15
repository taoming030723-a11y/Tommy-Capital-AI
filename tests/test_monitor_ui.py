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
