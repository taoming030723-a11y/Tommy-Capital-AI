"""Causal MACD wave/pivot monitoring. Independent of research scores and T rights."""
import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .technical import indicators

PERIODS = ("daily", "120", "60", "30", "15")
LABELS = {"normal": "正常", "momentum_change": "动能变化", "suspected": "疑似背离",
          "confirmed": "确认背离", "invalidated": "已失效", "expired": "已过期"}
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "monitor-config.json"


def load_monitor_config(path=DEFAULT_CONFIG):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config.get("schema") != 1:
        raise ValueError("监控配置schema无效")
    for key in ("min_bars", "history_bars", "pivot_radius", "wave_min_bars", "wave_price_match_bars", "signal_expiry_bars"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError("监控配置需正整数：" + key)
    if config["min_bars"] < 35 or config["pivot_radius"] < 3 or config["history_bars"] < config["min_bars"]:
        raise ValueError("监控不能降低原MACD历史与右侧确认要求")
    for key in ("hist_peak_reduction_pct", "hist_peak_difference_atr"):
        value=config.get(key)
        if type(value) not in (int,float) or not math.isfinite(value) or not 0 < value < 100:
            raise ValueError("柱峰阈值无效：" + key)
    original = json.loads((DEFAULT_CONFIG.parent / "config.json").read_text())
    for key in ("macd_min_pivot_spacing", "macd_min_price_extension_pct", "macd_min_price_extension_atr",
                "macd_min_dif_improvement_atr", "macd_min_confirmation_rebound_atr"):
        value=config.get("strict",{}).get(key)
        if type(value) not in (int,float) or not math.isfinite(value) or value < original[key]:
            raise ValueError("监控严格条件不得弱于原规则：" + key)
    risk=config.get("risk",{})
    for key in ("advance_lookback","support_lookback"):
        if type(risk.get(key)) is not int or risk[key]<5:
            raise ValueError("风险观察窗口需至少5根："+key)
    for key in ("material_advance_pct","high_zone_fraction","high_volume_ratio","stagnation_return_pct","partial_reduction_low","partial_reduction_high"):
        if type(risk.get(key)) not in (int,float) or not math.isfinite(risk[key]) or risk[key]<=0:
            raise ValueError("风险观察参数需为有限正数："+key)
    if not 0<risk["partial_reduction_low"]<=risk["partial_reduction_high"]<1:
        raise ValueError("人工减仓观察区间必须介于0与1之间")
    if risk["high_zone_fraction"]>1:
        raise ValueError("高位区间比例不能超过1")
    return config


def identity(*items):
    return hashlib.sha256(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]


def waves(data, sign, minimum=2):
    result, current = [], None
    for i, value in enumerate(data.macd):
        if value * sign > 0:
            if current is None:
                current = {"start": i, "end": i, "peak": i, "closed": False}
            current["end"] = i
            if abs(value) > abs(data.macd.iloc[current["peak"]]):
                current["peak"] = i
        elif current is not None:
            current["closed"] = True
            if current["end"] - current["start"] + 1 >= minimum:
                result.append(current)
            current = None
    if current is not None and current["end"] - current["start"] + 1 >= minimum:
        result.append(current)
    return result


def pivot_indices(data, direction, radius, complete=True):
    values = data.high.to_numpy() if direction == "bearish" else data.low.to_numpy()
    indices = []
    end = len(values) - radius if complete else len(values)
    for i in range(radius, end):
        left = values[i-radius:i]
        right = values[i+1:min(len(values), i+radius+1)]
        neighbors = np.concatenate([left, right])
        if (values[i] > neighbors.max() if direction == "bearish" else values[i] < neighbors.min()):
            indices.append(i)
    return indices


def matched_wave(groups, pivot, tolerance):
    matches = [w for w in groups if w["start"] <= pivot <= w["end"] + tolerance]
    return matches[-1] if matches else None


def strict_pivot_conditions(data, a, b, direction, config):
    """Same bounds and DIF zero-side filters as the existing strict detector."""
    row_a, row_b = data.iloc[a], data.iloc[b]
    field, sign = ("high", -1) if direction == "bearish" else ("low", 1)
    atr = float(row_b.atr14)
    extension = abs(float(row_b[field] - row_a[field]))
    strict = config["strict"]
    radius = config["pivot_radius"]
    right = b + radius < len(data)
    rebound = sign * float(data.close.iloc[b+radius] - row_b.close) if right else 0.
    return {
        "pivot_spacing": b-a >= strict["macd_min_pivot_spacing"],
        "price_extension": extension / float(row_a[field]) * 100 >= strict["macd_min_price_extension_pct"],
        "price_extension_atr": atr > 0 and extension / atr >= strict["macd_min_price_extension_atr"],
        "dif_improvement_atr": atr > 0 and abs(float(row_b.dif-row_a.dif)) / atr >= strict["macd_min_dif_improvement_atr"],
        "same_zero_region": bool(row_a.dif > 0 and row_b.dif > 0) if direction == "bearish" else bool(row_a.dif < 0 and row_b.dif < 0),
        "right_complete": right,
        "confirmation_rebound": atr > 0 and rebound / atr >= strict["macd_min_confirmation_rebound_atr"],
        "not_invalidated": (bool((data.high.iloc[b+1:] <= row_b.high).all()) if direction == "bearish" else
                            bool((data.low.iloc[b+1:] >= row_b.low).all()))}


def candidates(data, code, period, config):
    radius, output = config["pivot_radius"], []
    for direction, sign, field in (("bearish", 1, "high"), ("bullish", -1, "low")):
        groups = waves(data, sign, config["wave_min_bars"])
        # Whole contiguous same-sign waves, never two arbitrarily picked bars.
        if len(groups) >= 2:
            a, b = groups[-2:]
            first, second = abs(float(data.macd.iloc[a["peak"]])), abs(float(data.macd.iloc[b["peak"]]))
            atr = float(data.atr14.iloc[b["peak"]])
            lower = first > 0 and (first-second)/first*100 >= config["hist_peak_reduction_pct"] and first-second >= config["hist_peak_difference_atr"]*atr
            price_a = float(data[field].iloc[a["start"]:a["end"]+1].max() if sign > 0 else data[field].iloc[a["start"]:a["end"]+1].min())
            price_b = float(data[field].iloc[b["start"]:b["end"]+1].max() if sign > 0 else data[field].iloc[b["start"]:b["end"]+1].min())
            extended = price_b > price_a if sign > 0 else price_b < price_a
            if lower and not extended:
                pa=data[field].iloc[a["start"]:a["end"]+1]
                pb=data[field].iloc[b["start"]:b["end"]+1]
                da,db=(pa.idxmax(),pb.idxmax()) if sign>0 else (pa.idxmin(),pb.idxmin())
                output.append({"id": identity(code, period, direction, "wave", str(data.index[a["start"]]), str(data.index[b["start"]])),
                    "direction": direction, "state": "momentum_change", "kind": "histogram_decay",
                    "label": "上涨动能减弱" if sign > 0 else "下跌动能减弱", "methods": ["histogram"],
                    "histogram_peaks": [float(data.macd.iloc[a["peak"]]), float(data.macd.iloc[b["peak"]])],
                    "peak_dates": [data.index[a["peak"]].isoformat(), data.index[b["peak"]].isoformat()],
                    "wave_starts": [data.index[a["start"]].isoformat(), data.index[b["start"]].isoformat()],
                    "price_previous": price_a, "price_current": price_b,
                    "price_extreme_dates":[da.isoformat(),db.isoformat()],
                    "dif_previous":float(data.loc[da,"dif"]),"dif_current":float(data.loc[db,"dif"]),
                    "current_pivot": data.index[b["end"]].isoformat(), "current_wave_complete": b["closed"],
                    "reason": "同方向两段连续柱体峰值衰减，但价格未形成更高高点/更低低点；不认定严格背离"})
        indices = pivot_indices(data, direction, radius, complete=False)
        if len(indices) < 2:
            continue
        a, b = indices[-2:]
        if len(data)-1-b > config["signal_expiry_bars"]:
            continue
        row_a, row_b = data.iloc[a], data.iloc[b]
        extended = row_b[field] > row_a[field] if sign > 0 else row_b[field] < row_a[field]
        if not extended:
            continue
        wa, wb = (matched_wave(groups, p, config["wave_price_match_bars"]) for p in (a, b))
        first = float(data.macd.iloc[wa["peak"]]) if wa else None
        second = float(data.macd.iloc[wb["peak"]]) if wb else None
        histogram = bool(wa and wb and wa["start"] != wb["start"] and abs(first) > 0 and
            (abs(first)-abs(second))/abs(first)*100 >= config["hist_peak_reduction_pct"] and
            abs(first)-abs(second) >= config["hist_peak_difference_atr"]*float(row_b.atr14))
        classic = bool(row_b.dif < row_a.dif if sign > 0 else row_b.dif > row_a.dif)
        conditions = strict_pivot_conditions(data, a, b, direction, config)
        classic = classic and conditions["same_zero_region"] and conditions["dif_improvement_atr"]
        if not classic and not histogram:
            continue
        methods = (["DIF"] if classic else []) + (["histogram"] if histogram else [])
        price_conditions = all(v for k, v in conditions.items() if k != "dif_improvement_atr")
        confirmed_classic = classic and all(conditions.values())
        confirmed_histogram = histogram and price_conditions and wa["closed"] and wb["closed"]
        state = "confirmed" if confirmed_classic or confirmed_histogram else "suspected"
        side = "顶" if direction == "bearish" else "底"
        dual_confirmed=confirmed_classic and confirmed_histogram
        output.append({"id": identity(code, period, direction, "pivot", data.index[a].isoformat(), data.index[b].isoformat()),
            "direction": direction, "state": state, "kind": "double_divergence" if len(methods)==2 else "dif_divergence" if classic else "histogram_divergence",
            "label": ("确认" if state=="confirmed" else "疑似") + side + "背离" + ("（双重动能背离）" if dual_confirmed or state=="suspected" and len(methods)==2 else "（DIF确认，柱体待确认）" if confirmed_classic and histogram else ""),
            "methods": methods, "previous_pivot": data.index[a].isoformat(), "current_pivot": data.index[b].isoformat(),
            "price_previous": float(row_a[field]), "price_current": float(row_b[field]),
            "dif_previous": float(row_a.dif), "dif_current": float(row_b.dif), "atr14": float(row_b.atr14),
            "histogram_peaks": [first, second],
            "peak_dates": [data.index[w["peak"]].isoformat() if w else None for w in (wa, wb)],
            "wave_starts": [data.index[w["start"]].isoformat() if w else None for w in (wa, wb)],
            "current_wave_complete": bool(wb and wb["closed"]), "conditions": conditions,
            "confirmed_methods": (["DIF"] if confirmed_classic else []) + (["histogram"] if confirmed_histogram else []),
            "reason": "价格枢轴与对应同向柱波/DIF比较；右侧完整K线、ATR及零轴过滤逐项核验",
            "next_conditions": ([k for k,v in conditions.items() if not v] + (["等待当前同向柱波结束"] if histogram and not wb["closed"] else [])) if state!="confirmed" else []})
    return output


def replay_frame(frame, code, period, config, previous=None, calculated=False):
    """Append-only transitions on prefixes; confirmed facts are frozen at confirmation."""
    if period not in PERIODS:
        raise ValueError("未知监控周期")
    state = copy.deepcopy(previous or {"schema": 1, "events": {}, "transitions": [], "bars": {}})
    data = frame if calculated else indicators(frame)
    if len(data) < config["min_bars"]:
        return {"status": "insufficient_bars", "label": "数据不足，无法判断", "period": period,
                "bar_count": len(data), "signals": [], "events": []}, state
    if not data.index.is_monotonic_increasing or data.index.has_duplicates:
        raise ValueError("监控K线日期乱序或重复")
    # Never silently recompute a previously observed prefix after a price/adjustment revision.
    for timestamp, row in data.iterrows():
        key = timestamp.isoformat()
        values = [round(float(row[k]), 10) for k in ("open", "high", "low", "close", "volume")]
        if key in state["bars"] and state["bars"][key] != values:
            return {"status": "data_revision", "label": "历史行情/复权发生变化，已冻结旧事件；需核对后重建独立状态",
                    "period": period, "signals": [], "events": [], "revised_at": key}, state
    last = state.get("last_bar")
    if last and data.index[-1].isoformat() < last:
        return {"status": "stale", "label": "行情早于上次检查，未回滚事件", "period": period, "signals": [], "events": []}, state
    new = []
    start = config["min_bars"]-1
    if last:
        matches = np.flatnonzero(data.index.astype(str) == last.replace("T", " "))
        if not matches.size and last not in [t.isoformat() for t in data.index]:
            return {"status": "history_gap", "label": "未覆盖上次已完成K线，不能跳接历史", "period": period, "signals": [], "events": []}, state
    for end in range(start, len(data)):
        prefix = data.iloc[:end+1]
        stamp = prefix.index[-1].isoformat()
        if last and stamp <= last:
            continue
        found = {s["id"]: s for s in candidates(prefix.iloc[-config["history_bars"]:], code, period, config)}
        for identifier, event in list(state["events"].items()):
            if event["state"] in ("invalidated", "expired"):
                continue
            pivot = event["current_pivot"]
            later = prefix[prefix.index > pivot]
            boundary=event["price_previous"] if event["state"]=="momentum_change" else event["price_current"]
            broken = (not later.empty and ((later.high > boundary).any() if event["direction"]=="bearish" else (later.low < boundary).any()))
            age = sum(t.isoformat() > pivot for t in prefix.index)
            disappeared = event["state"] == "momentum_change" and identifier not in found
            state_name = "invalidated" if broken or disappeared else "expired" if age > config["signal_expiry_bars"] else None
            if state_name:
                event["state"] = state_name
                event["state_changed_at"] = stamp
                transition = {"id": identifier, "state": state_name, "occurred_at": stamp,
                              "reason": "后续价格突破枢轴/动能衰减消退" if state_name=="invalidated" else "超过有效观察窗口"}
                state["transitions"].append(transition); new.append(transition)
        for identifier, signal in found.items():
            old = state["events"].get(identifier)
            if old and old["state"] in ("invalidated", "expired"):
                continue
            if old and old["state"] == "confirmed":
                added=set(signal.get("confirmed_methods",[]))-set(old.get("confirmed_methods",[]))
                if signal["state"]=="confirmed" and added:
                    update={"occurred_at":stamp,"added_methods":sorted(added),"facts":copy.deepcopy(signal)}
                    old.setdefault("additional_confirmations",[]).append(update)
                    old["confirmed_methods"]=sorted(set(old.get("confirmed_methods",[]))|added)
                    old["label"]=signal["label"]
                    for key in ("histogram_peaks","peak_dates","wave_starts","current_wave_complete","next_conditions"):
                        old[key]=copy.deepcopy(signal[key])
                    old["evidence_updated_at"]=stamp
                    transition={"id":identifier,"state":"evidence_confirmed","occurred_at":stamp,"methods":sorted(added)}
                    state["transitions"].append(transition);new.append(transition)
                continue
            event = {**signal, "first_seen_at": old["first_seen_at"] if old else stamp,
                     "state_changed_at": old["state_changed_at"] if old and old["state"]==signal["state"] else stamp}
            if old and old.get("first_observed_at"):
                event["first_observed_at"]=old["first_observed_at"]
            if signal["state"]=="confirmed":
                event["confirmed_at"] = stamp
                event["first_confirmed_snapshot"] = copy.deepcopy(signal)
            state["events"][identifier] = event
            if old is None or old["state"] != event["state"]:
                transition = {"id": identifier, "state": event["state"], "occurred_at": stamp}
                state["transitions"].append(transition); new.append(transition)
        row = prefix.iloc[-1]
        state["bars"][stamp] = [round(float(row[k]), 10) for k in ("open", "high", "low", "close", "volume")]
    # Store the warm-up prefix too, so a later data revision is visible.
    for timestamp,row in data.iloc[:start].iterrows():
        state["bars"].setdefault(timestamp.isoformat(), [round(float(row[k]),10) for k in ("open","high","low","close","volume")])
    state["last_bar"] = data.index[-1].isoformat()
    active = [v for v in state["events"].values() if v["state"] not in ("invalidated", "expired")]
    priority = {"confirmed": 3, "suspected": 2, "momentum_change": 1}
    active.sort(key=lambda s: (priority[s["state"]],s["state_changed_at"]), reverse=True)
    return {"status": "ok", "period": period, "last_bar": state["last_bar"], "bar_count": len(data),
            "macd_basis": "EMA12/EMA26，DEA9；柱值=2×(DIF−DEA)",
            "label": active[0]["label"] if active else "无有效背离/显著动能衰减", "signals": active,
            "events": new, "event_history":state["transitions"][-200:],
            "inactive_signals":[v for v in state["events"].values() if v["state"] in ("invalidated","expired")][-20:],
            "latest_macd": {k: float(data.iloc[-1][k]) for k in ("dif","dea","macd")}}, state
