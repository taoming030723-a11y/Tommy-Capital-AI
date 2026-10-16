"""Report controls change local views/scopes, never the formal market ledger."""
import copy
import json
from pathlib import Path

import pytest

from tommy_capital.report_controls import (add_report_controls,strip_report_controls,load_controls,save_control,
    effective_preferences,validate_controls,load_latest_view)
from tommy_capital.monitor import monitor_scope,validate_public_monitor,run_monitor
from test_volume_tracking_report import accumulated


def test_local_ignore_restore_and_manual_list_persist_with_compare_and_swap(tmp_path):
    path=tmp_path/"controls.json"
    first=save_control(path,"ignore","000001")
    assert load_controls(path)==first
    with pytest.raises(ValueError,match="其他窗口"):
        save_control(path,"restore","000001",expected_revision=None)
    second=save_control(path,"restore","000001",expected_revision=first["revision"])
    assert effective_preferences({"schema":1,"ignored_codes":["000001","600000"]},second)["ignored_codes"]==["600000"]
    third=save_control(path,"add","000001","本机观察","不是持仓",second["revision"])
    assert load_controls(path)["manual_watch"][0]["code"]=="000001"
    with pytest.raises(ValueError,match="已在"):
        save_control(path,"add","000001",expected_revision=third["revision"])
    removed=save_control(path,"remove","000001",expected_revision=third["revision"])
    assert not removed["manual_watch"]


def test_manual_observation_is_independent_deduplicated_and_never_a_confirmed_holding():
    history=accumulated();before=copy.deepcopy(history)
    code=history["sessions"][0]["top10"][0]["code"]
    report={"session":"2026-10-12","top10_history":history}
    scope=monitor_scope(report=report,preferences={"schema":1,"ignored_codes":[]},
        manual_watch=[{"code":code},{"code":"000001","name":"独立观察"}],positions=[{"code":code}])
    assert len(scope)==2
    assert next(r for r in scope if r["code"]==code)["sources"]==["Top10观察","手动观察","持仓"]
    assert next(r for r in scope if r["code"]=="000001")["sources"]==["手动观察"]
    ignored=monitor_scope(report=report,preferences={"schema":1,"ignored_codes":[code]},manual_watch=[{"code":code}])
    assert ignored[0]["sources"]==["手动观察"]
    assert history==before
    with pytest.raises(ValueError,match="个人"):
        run_monitor(ignored,public=True)
    with pytest.raises(ValueError,match="个人"):
        validate_public_monitor({"public":True,"scope":ignored})


def test_report_entry_is_idempotent_and_preserves_original_market_text():
    original="# Tommy Capital · 2026-10-15 · 收盘\n\n| 数量 | 123 |\n"
    updated=add_report_controls(original)
    assert updated.count("tommy-report-controls:start")==1
    assert add_report_controls(updated)==updated
    assert strip_report_controls(updated)==original
    assert "https://tommy-capital-report.stableokapi18.chatgpt.site" in updated and "Start-Report.cmd" in updated
    assert "](http://127.0.0.1" not in updated
    with pytest.raises(ValueError):
        add_report_controls("不是市场报告")


def test_latest_view_retains_market_date_and_top20_without_rescoring(tmp_path):
    markdown="# Tommy Capital · 2026-10-15 · 收盘\n\n综合前20原始数字\n\n## 综合前10累计至少3次\n\n旧观察表\n\n## 前20入选理由\n\n原始理由\n"
    (tmp_path/"latest-summary.json").write_text(json.dumps({"session":"2026-10-15","scoring_version":"old-real-version"}))
    (tmp_path/"latest-report.md").write_text(add_report_controls(markdown))
    view=load_latest_view(tmp_path)
    assert "综合前20原始数字" in view["before_watch"] and "旧观察表" not in view["after_watch"]
    assert "原始理由" in view["after_watch"] and view["summary"]["scoring_version"]=="old-real-version"
    (tmp_path/"latest-summary.json").write_text('{"session":"2026-10-16"}')
    with pytest.raises(ValueError,match="日期不一致"):
        load_latest_view(tmp_path)


@pytest.mark.parametrize("bad",[{"schema":True},{"schema":1,"manual_watch":["600000"]},
    {"schema":1,"ignored_codes":["600000"],"restored_codes":["600000"]}])
def test_invalid_settings_cannot_silently_restore_ignored_stocks(bad):
    with pytest.raises(ValueError):validate_controls(bad)


def test_private_manual_event_state_is_not_publishable_even_without_cost_or_notes():
    private={"schema":1,"visibility":"local_private","stocks":{"000001":{"frames":{}}}}
    with pytest.raises(ValueError,match="个人"):
        run_monitor([],state=private,public=True)
    with pytest.raises(ValueError,match="个人"):
        validate_public_monitor({"public":True,"scope":[],"rows":[]},private)


@pytest.mark.parametrize("mismatch",[False,True])
def test_public_sync_pins_markdown_and_json_to_same_commit_without_touching_local_controls(tmp_path,monkeypatch,mismatch):
    from tommy_capital.monitor_sync import sync_public_inputs
    from tommy_capital.tracking import history_with_report
    from tommy_capital.cli import make_summary,clean
    from tommy_capital.reporting import render_markdown
    from tommy_capital.data import DataError
    from test_watchlist_preferences import report_with_watch
    report=report_with_watch();history=history_with_report(report["top10_history"],report)
    summary=clean(make_summary(report));markdown=render_markdown(report)
    if mismatch:markdown=markdown.replace("2026-10-13","2026-10-14")
    controls=tmp_path/"report-controls.json";save_control(controls,"ignore","000001")
    previous=controls.read_bytes();calls=[]
    class Response:
        def __init__(self,value):self.value=value;self.text=value if isinstance(value,str) else json.dumps(value)
        def json(self):return self.value
        def raise_for_status(self):pass
    def get(url,**kwargs):
        calls.append(url)
        if url.endswith("/git/ref/heads/main"):return Response({"object":{"sha":"a"*40}})
        assert "/"+"a"*40+"/" in url
        if url.endswith("top10-history.json"):return Response(history)
        if url.endswith("watchlist-preferences.json"):return Response({"schema":1,"ignored_codes":[]})
        if url.endswith("latest-summary.json"):return Response(summary)
        if url.endswith("latest-report.md"):return Response(markdown)
        pytest.fail(url)
    monkeypatch.setattr("tommy_capital.monitor_sync.requests.get",get)
    if mismatch:
        with pytest.raises(DataError,match="日期不一致"):
            sync_public_inputs(tmp_path/"public")
    else:
        root,commit=sync_public_inputs(tmp_path/"public")
        assert commit=="a"*40 and (root/"latest-report.md").read_text()==markdown
        assert load_latest_view(root)["summary"]==summary
    assert controls.read_bytes()==previous and len(calls)==5
