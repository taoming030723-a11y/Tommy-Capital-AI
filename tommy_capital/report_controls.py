"""Durable local controls for the interactive latest report; no research mutations."""
import json
import os
import uuid
from pathlib import Path

from .positions import code_value, locked
from .tracking import validate_preferences, load_preferences

BEGIN="<!-- tommy-report-controls:start -->"
END="<!-- tommy-report-controls:end -->"
REPORT_URL="http://127.0.0.1:8501/?page=report"


def empty_controls():
    return {"schema":1,"revision":None,"ignored_codes":[],"restored_codes":[],"manual_watch":[]}


def validate_controls(value):
    if not isinstance(value,dict) or type(value.get("schema")) is not int or value["schema"]!=1:
        raise ValueError("报告操作设置版本无效")
    ignored=validate_preferences({"schema":1,"ignored_codes":value.get("ignored_codes",[])})["ignored_codes"]
    restored=validate_preferences({"schema":1,"ignored_codes":value.get("restored_codes",[])})["ignored_codes"]
    if set(ignored)&set(restored):
        raise ValueError("同一股票不能同时忽略与恢复")
    rows=value.get("manual_watch",[])
    if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows) or len(rows)>6000:
        raise ValueError("手动观察名单格式错误")
    manual=[]
    for row in rows:
        code=code_value(row.get("code"));name=str(row.get("name") or "");note=str(row.get("note") or "")
        if len(name)>80 or len(note)>4000:
            raise ValueError("观察名称或备注过长")
        manual.append({"code":code,"name":name,"note":note})
    if len({r["code"] for r in manual})!=len(manual):
        raise ValueError("手动观察代码重复")
    return {"schema":1,"revision":value.get("revision"),"ignored_codes":ignored,"restored_codes":restored,
            "manual_watch":sorted(manual,key=lambda r:r["code"])}


def load_controls(path=".tommy/report-controls.json"):
    path=Path(path)
    return validate_controls(json.loads(path.read_text(encoding="utf-8-sig"))) if path.exists() else empty_controls()


def save_control(path,action,code,name="",note="",expected_revision=None):
    code=code_value(code);path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with locked(path):
        value=load_controls(path)
        if value["revision"]!=expected_revision:
            raise ValueError("报告设置已在其他窗口修改，请刷新后重试；未覆盖新设置")
        ignored,restored=set(value["ignored_codes"]),set(value["restored_codes"])
        manual={r["code"]:r for r in value["manual_watch"]}
        if action=="ignore":
            ignored.add(code);restored.discard(code)
        elif action=="restore":
            ignored.discard(code);restored.add(code)
        elif action=="add":
            if code in manual:
                raise ValueError("该代码已在手动观察名单中，未覆盖原记录")
            manual[code]={"code":code,"name":name,"note":note}
        elif action=="remove":
            manual.pop(code,None)
        else:
            raise ValueError("报告操作无效")
        value=validate_controls({**value,"revision":uuid.uuid4().hex,"ignored_codes":sorted(ignored),
            "restored_codes":sorted(restored),"manual_watch":list(manual.values())})
        temporary=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
        try:os.chmod(temporary,0o600)
        except OSError:pass
        temporary.replace(path)
    return value


def effective_preferences(public,controls):
    public=validate_preferences(public);controls=validate_controls(controls)
    ignored=(set(public["ignored_codes"])|set(controls["ignored_codes"]))-set(controls["restored_codes"])
    return {"schema":1,"ignored_codes":sorted(ignored)}


def public_preferences(directory):
    root=Path(directory)
    local=root/"watchlist-preferences.json"
    return load_preferences(local if local.exists() else root.parent/"operations/watchlist-preferences.json")


def strip_report_controls(markdown):
    if BEGIN not in markdown:
        return markdown
    before,_,tail=markdown.partition(BEGIN)
    _,found,after=tail.partition(END)
    if not found:
        raise ValueError("报告操作入口标记不完整")
    return before.rstrip()+"\n\n"+after.lstrip("\n")


def add_report_controls(markdown,repository="taoming030723-a11y/Tommy-Capital-AI"):
    import re
    if not re.fullmatch(r"[\w.-]+/[\w.-]+",repository):
        raise ValueError("报告仓库地址无效")
    markdown=strip_report_controls(markdown)
    title,separator,body=markdown.partition("\n")
    if not separator or not title.startswith("# Tommy Capital"):
        raise ValueError("报告缺少可核验标题，未改写原报告")
    controls=(f"{BEGIN}\n[打开交互版最新报告：忽略／恢复、手动添加观察、管理持仓]({REPORT_URL})"
        f" · [首次启动说明](https://github.com/{repository}/blob/main/docs/report-controls.md)"
        f" · [云端忽略／恢复](https://github.com/{repository}/actions/workflows/watchlist.yml)\n\n"
        "交互版需先在电脑启动 Start-Report.cmd；本机操作保存在本机，GitHub本文保留原扫描结果。\n"
        f"{END}\n\n")
    return title+"\n\n"+controls+body.lstrip("\n")


def load_latest_view(directory):
    from .scan_status import strip_banner
    root=Path(directory)
    summary=json.loads((root/"latest-summary.json").read_text(encoding="utf-8"))
    markdown=strip_report_controls(strip_banner((root/"latest-report.md").read_text(encoding="utf-8")))
    if not summary.get("session") or not markdown.startswith("# Tommy Capital · "+summary["session"]+" · "):
        raise ValueError("最新Markdown与机器报告日期不一致，保留文件并停止显示")
    before,marker,after=markdown.partition("## 综合前10累计至少3次")
    if not marker:
        raise ValueError("最新报告缺少累计观察章节")
    _,next_section,tail=after.partition("\n## ")
    return {"summary":summary,"before_watch":before.rstrip(),"after_watch":("## "+tail) if next_section else ""}
