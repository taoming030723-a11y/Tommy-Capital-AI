"""Offline replay of a frozen received daily series; no synthetic/live minute signals."""
import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from .macd_monitor import PERIODS, load_monitor_config, replay_frame
from .monitor import atomic_json
from .monitor_risk import resonance
from .monitor_reporting import render_monitor_section


def offline_example(output):
    root=Path(__file__).resolve().parents[1]/"tests/fixtures/monitor"
    meta=json.loads((root/"source.json").read_text(encoding="utf-8"))
    path=root/"603127-daily.csv"
    if hashlib.sha256(path.read_bytes()).hexdigest()!=meta["sha256"]:
        raise ValueError("冻结历史文件校验失败")
    data=pd.read_csv(path,index_col=0,parse_dates=True)
    daily,state=replay_frame(data,meta["code"],"daily",load_monitor_config())
    daily.update(source=meta["source"],source_last_bar=data.index[-1].isoformat())
    frames={p:{"status":"unavailable","label":"离线样例未附分钟数据，无法判断","error":"未使用模拟分钟行情","signals":[]} for p in PERIODS}
    frames["daily"]=daily
    row={"code":meta["code"],"name":meta["name"],"sources":["冻结历史演示"],"frames":frames,
         "combined":resonance(frames),"data_complete":False}
    result={"schema":1,"public":False,"offline":True,"status":"partial","monitor_complete":False,
            "checked_at":"离线历史逐根重放；非实时检查","data_as_of":meta["as_of_session"],"fixture":meta,
            "rows":[row],"scope":[],"orders_authorized":False}
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    atomic_json(output/"monitor.json",result)
    atomic_json(output/"event-state.json",state)
    (output/"monitor.md").write_text("# 冻结真实历史离线演示\n\n不代表当前持仓或实时信号；不计入Top10历史。\n\n"+
                                   "\n".join(render_monitor_section(result)),encoding="utf-8")
    return {"output":str(output),"historical_daily_bars":len(data),"events":len(state["transitions"]),
            "missing_periods":["120","60","30","15"],"monitor_complete":False}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--out",default=".tommy/offline-example")
    print(json.dumps(offline_example(parser.parse_args().out),ensure_ascii=False))


if __name__=="__main__":
    main()
