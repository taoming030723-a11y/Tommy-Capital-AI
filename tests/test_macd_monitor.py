"""Explicit offline algorithm fixtures plus frozen, received historical OHLC replay."""
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tommy_capital.macd_monitor import load_monitor_config,replay_frame,candidates,waves
from tommy_capital.technical import indicators,confirmed_divergences

CONFIG=load_monitor_config()


def controlled(direction="bearish",new_extreme=True,histogram=True,classic=True):
    size=85
    close=np.ones(size)*100.
    frame=pd.DataFrame({"open":close,"close":close,"high":close+.2,"low":close-.2,"volume":1000.,
        "dif":np.ones(size),"dea":np.ones(size),"macd":-np.ones(size)*.1,"atr14":np.ones(size)},
        index=pd.bdate_range("2025-01-02",periods=size))
    frame.loc[frame.index[40:50],"macd"]=[.2,.5,1.,2.,3.,4.,3.,2.,1.,.5]
    frame.loc[frame.index[60:70],"macd"]=[.2,.4,.8,1.,1.5,2.,1.5,1.,.7,.4] if histogram else [.2,1.,2.,3.,4.,5.,4.,3.,2.,1.]
    frame.loc[frame.index[45],["high","close","dif"]]=[110.,109.,4.]
    frame.loc[frame.index[65],["high","close","dif"]]=[115. if new_extreme else 108.,113. if new_extreme else 107.,2. if classic else 4.]
    if direction=="bullish":
        old=frame.copy()
        frame["high"],frame["low"]=200-old.low,200-old.high
        frame["open"],frame["close"]=200-old.open,200-old.close
        frame["dif"],frame["dea"],frame["macd"]=-old.dif,-old.dea,-old.macd
    return frame


@pytest.mark.parametrize("direction",["bearish","bullish"])
def test_price_new_extreme_and_wave_peak_decay_has_pending_then_confirmed_evidence(direction):
    data=controlled(direction)
    early,state=replay_frame(data.iloc[:67],"600000","daily",CONFIG,calculated=True)
    pending=[s for s in early["signals"] if s["state"]=="suspected"]
    assert pending and not any(s["state"]=="confirmed" for s in early["signals"])
    later,state=replay_frame(data.iloc[:72],"600000","daily",CONFIG,state,calculated=True)
    confirmed=[s for s in later["signals"] if s["state"]=="confirmed"]
    assert len(confirmed)==1 and set(confirmed[0]["confirmed_methods"])=={"DIF","histogram"}
    assert "双重动能背离" in confirmed[0]["label"]
    assert confirmed[0]["confirmed_at"]==data.index[68].isoformat()
    assert confirmed[0]["additional_confirmations"][0]["occurred_at"]==data.index[70].isoformat()
    assert abs(confirmed[0]["histogram_peaks"][1])<abs(confirmed[0]["histogram_peaks"][0])


@pytest.mark.parametrize("direction",["bearish","bullish"])
def test_shorter_bars_without_price_extension_are_only_momentum_change(direction):
    result,_=replay_frame(controlled(direction,new_extreme=False),"600000","60",CONFIG,calculated=True)
    assert any(s["state"]=="momentum_change" for s in result["signals"])
    assert not any(s["state"]=="confirmed" for s in result["signals"])


def test_histogram_alone_waits_for_wave_to_finish_and_dif_cannot_be_faked():
    data=controlled(classic=False)
    result,state=replay_frame(data.iloc[:69],"600000","120",CONFIG,calculated=True)
    assert any(s["state"]=="suspected" for s in result["signals"])
    assert not any(s["state"]=="confirmed" for s in result["signals"])
    result,_=replay_frame(data.iloc[:72],"600000","120",CONFIG,state,calculated=True)
    assert any(s["confirmed_methods"]==["histogram"] for s in result["signals"] if s["state"]=="confirmed")


def test_arbitrary_two_peaks_inside_one_positive_wave_are_not_two_wave_divergence():
    data=controlled(classic=False)
    data.loc[data.index[40:70],"macd"]=np.maximum(data.macd.iloc[40:70],.05)
    assert len(waves(data,1))==1
    assert not candidates(data,"600000","15",CONFIG)


def test_duplicate_refresh_does_not_repeat_events_and_confirmed_facts_do_not_repaint():
    data=controlled()
    first,state=replay_frame(data.iloc[:69],"600000","30",CONFIG,calculated=True)
    event=next(s for s in first["signals"] if s["state"]=="confirmed")
    frozen=copy.deepcopy(event["first_confirmed_snapshot"])
    later,state=replay_frame(data,"600000","30",CONFIG,state,calculated=True)
    assert state["events"][event["id"]]["first_confirmed_snapshot"]==frozen
    repeated,again=replay_frame(data,"600000","30",CONFIG,state,calculated=True)
    assert repeated["events"]==[] and again==state
    broken=data.copy();broken.loc[broken.index[-1],"high"]=120.
    # A revision of an already observed bar is a data revision, not a rewritten event.
    revision,new=replay_frame(broken,"600000","30",CONFIG,state,calculated=True)
    assert revision["status"]=="data_revision" and new==state
    future=pd.concat([data,data.iloc[-1:].set_axis([data.index[-1]+pd.offsets.BDay()])])
    future.loc[future.index[-1],"high"]=120.
    invalid,updated=replay_frame(future,"600000","30",CONFIG,state,calculated=True)
    assert updated["events"][event["id"]]["state"]=="invalidated"
    assert any(e["state"]=="invalidated" for e in invalid["events"])


def test_period_identities_never_merge_and_missing_data_is_not_no_signal():
    data=controlled()
    ids=[]
    for period in ("daily","120","60","30","15"):
        result,_=replay_frame(data,"600000",period,CONFIG,calculated=True)
        ids.append(next(s["id"] for s in result["signals"] if s["state"]=="confirmed"))
    assert len(set(ids))==5
    result,_=replay_frame(data.iloc[:20],"600000","daily",CONFIG,calculated=True)
    assert result["status"]=="insufficient_bars" and "无法判断" in result["label"]


def test_native_classic_confirmation_and_monitor_share_strict_rules_and_macd_formula():
    close=np.r_[np.ones(30)*100,np.linspace(101,140,20),np.linspace(138,115,10),np.linspace(115.4,143,20),np.linspace(141,130,10)]
    frame=pd.DataFrame({"open":close-.1,"close":close,"high":close+.2,"low":close-.2,"volume":1000.},index=pd.bdate_range("2025-01-01",periods=len(close)))
    calc=indicators(frame)
    assert np.allclose(calc.macd,2*(calc.dif-calc.dea))
    native=next(s for s in confirmed_divergences(frame)["signals"] if s["indicator"]=="MACD_DIF")
    result,_=replay_frame(frame,"600000","daily",CONFIG)
    signal=next(s for s in result["signals"] if s["state"]=="confirmed")
    assert signal["previous_pivot"]==native["previous_pivot"] and signal["current_pivot"]==native["current_pivot"]
    assert signal["confirmed_at"]==native["confirmed_at"]
    assert all(signal["conditions"][k]==v for k,v in native["strength"]["conditions"].items())


def test_frozen_received_history_full_replay_equals_incremental_prefix_replay():
    root=Path(__file__).parent/"fixtures/monitor"
    meta=json.loads((root/"source.json").read_text())
    path=root/"603127-daily.csv"
    assert hashlib.sha256(path.read_bytes()).hexdigest()==meta["sha256"]
    frame=pd.read_csv(path,index_col=0,parse_dates=True)
    full,expected=replay_frame(frame,"603127","daily",CONFIG)
    state=None
    for end in range(CONFIG["min_bars"],len(frame)+1):
        actual,state=replay_frame(frame.iloc[:end],"603127","daily",CONFIG,state)
        for event in state["events"].values():
            assert event["first_seen_at"]<=frame.index[end-1].isoformat()
            assert event.get("confirmed_at",frame.index[end-1].isoformat())<=frame.index[end-1].isoformat()
    assert state==expected and actual["signals"]==full["signals"]
