"""Completed-bar K/D descriptions; no score, ranking, entry or T permissions."""
import json
import math
from datetime import datetime
from pathlib import Path

PERIODS = ("monthly", "weekly", "daily", "120", "60", "30", "15")
PERIOD_NAMES = dict(zip(PERIODS, ("月线", "周线", "日线", "120分钟", "60分钟", "30分钟", "15分钟")))
VERSION = "kd_state_observer_v1"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "kd-state-config.json"
LABELS = {"low_weak": "低位弱势", "early_turn": "早期转折", "low_golden_cross": "低位金叉",
          "golden_cross": "金叉（非低位）", "bullish_continuation": "偏多延续", "momentum_weakening": "动能减弱",
          "bearish_turn": "偏空转折", "bearish_continuation": "偏空延续", "mixed": "方向未一致", "flat": "中性整理"}
DIRECTIONS = {"low_weak": "bearish", "early_turn": "recovering", "low_golden_cross": "bullish",
              "golden_cross": "bullish", "bullish_continuation": "bullish", "momentum_weakening": "weakening",
              "bearish_turn": "bearish", "bearish_continuation": "bearish", "mixed": "mixed", "flat": "mixed"}


def validate_policy(policy):
    if type(policy.get("schema")) is not int or policy["schema"] != 1 or type(policy.get("history_bars")) is not int or not 3 <= policy["history_bars"] <= 20:
        raise ValueError("K/D配置版本或轨迹长度无效")
    for key in ("low_level", "high_level", "slope_min_points", "gap_change_min_points", "weak_cross_gap_points"):
        value = policy.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("K/D阈值必须为有限数值")
    if not 0 <= policy["low_level"] < policy["high_level"] <= 100:
        raise ValueError("K/D高低位阈值无效")
    if any(policy[k] <= 0 for k in ("slope_min_points", "gap_change_min_points", "weak_cross_gap_points")):
        raise ValueError("K/D变化阈值须为正")
    minimum = policy.get("minimum_bars", {})
    floors = {"monthly": 24, "weekly": 60, "daily": 250, "120": 60, "60": 60, "30": 60, "15": 60}
    if set(minimum) != set(PERIODS) or any(type(minimum[p]) is not int or minimum[p] < max(n, policy["history_bars"]) for p, n in floors.items()):
        raise ValueError("K/D观察不得降低已有周期历史要求")
    return policy


def load_policy(path=DEFAULT_CONFIG):
    return validate_policy(json.loads(Path(path).read_text()))


def unavailable(period, source=None, reason="数据不足，无法判断"):
    return {"version": VERSION, "period": period, "source": source, "status": "unavailable", "state": "unavailable",
            "label": "数据不足，无法判断", "error": reason, "history": [], "influences_ranking": False,
            "trade_confirmation": False}


def timestamp(value):
    # Source adapters normalize completed period-end labels to local naive ISO.
    if not isinstance(value, str):
        raise ValueError("K/D缺少完整K线时间")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        from .data import SHANGHAI
        parsed = parsed.astimezone(SHANGHAI).replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds")


def classify_kd(rows, period, source, complete_through, policy=None):
    policy = validate_policy(policy) if policy is not None else load_policy()
    if period not in PERIODS:
        raise ValueError("未知K/D观察周期")
    try:
        cutoff = timestamp(complete_through)
        selected = [{**r, "date": timestamp(r.get("date"))} for r in rows if timestamp(r.get("date")) <= cutoff]
        dates = [r["date"] for r in selected]
        if dates != sorted(set(dates)):
            raise ValueError("K/D源时间重复或乱序")
        if len(selected) < policy["minimum_bars"][period]:
            raise ValueError("完整周期历史不足，不能仅凭最新K/D判断")
        history = []
        for r in selected[-policy["history_bars"]:]:
            k, d = r.get("k"), r.get("d")
            if any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 100 for x in (k, d)):
                raise ValueError("最近K/D轨迹缺失或超出有效范围")
            j = 3*k-2*d
            if r.get("j") is not None and (not isinstance(r["j"], (int, float)) or not math.isfinite(r["j"]) or abs(r["j"]-j) > 1e-8):
                raise ValueError("J与3K−2D口径不一致")
            history.append({"date": r["date"], "k": k, "d": d, "j": j, "gap": k-d})
    except (ValueError, KeyError, TypeError) as exc:
        return unavailable(period, source, str(exc))
    a, b, c = history[-3:]
    dk, dd, gap_change = c["k"]-b["k"], c["d"]-b["d"], c["gap"]-b["gap"]
    slope, change = policy["slope_min_points"], policy["gap_change_min_points"]
    cross_up, cross_down = b["gap"] <= 0 < c["gap"], b["gap"] >= 0 > c["gap"]
    low = max(c["k"], c["d"]) <= policy["low_level"]
    high = min(c["k"], c["d"]) >= policy["high_level"]
    conditions = {"k_above_d": c["gap"] > 0, "k_rising": dk >= slope, "d_rising": dd >= slope,
                  "k_falling": dk <= -slope, "d_falling": dd <= -slope, "cross_up": cross_up,
                  "cross_down": cross_down, "gap_narrowing": b["gap"] > c["gap"] >= 0 and gap_change <= -change}
    if cross_up:
        state = "low_golden_cross" if max(b["k"], b["d"]) <= policy["low_level"] else "golden_cross"
    elif cross_down:
        state = "bearish_turn"
    elif low and c["gap"] < 0 and dk <= -slope and dd <= -slope:
        state = "low_weak"
    elif dk >= slope and dd < slope:
        state = "early_turn"
    elif c["gap"] > 0 and dk >= slope and dd >= slope:
        state = "bullish_continuation"
    elif dk <= -slope and conditions["gap_narrowing"]:
        state = "momentum_weakening"
    elif c["gap"] < 0 and dk <= -slope and dd <= -slope:
        state = "bearish_continuation"
    elif abs(dk) < slope and abs(dd) < slope:
        state = "flat"
    else:
        state = "mixed"
    transitions = []
    for previous, current in zip(history, history[1:]):
        if previous["gap"] <= 0 < current["gap"] or previous["gap"] >= 0 > current["gap"]:
            transitions.append({"date": current["date"], "type": "golden_cross" if current["gap"] > 0 else "dead_cross",
                                "gap": current["gap"], "weak_separation": abs(current["gap"]) < policy["weak_cross_gap_points"]})
    return {"version": VERSION, "period": period, "source": source, "status": "ok", "state": state,
            "label": LABELS[state], "direction": DIRECTIONS[state], "zone": "low" if low else "high" if high else "middle",
            "last_bar": c["date"], "complete_through": cutoff, "source_bar_count": len(selected),
            "current": c, "previous": b, "k_change": dk, "d_change": dd, "gap_change": gap_change,
            "new_k_upturn": dk >= slope and b["k"]-a["k"] < slope, "conditions": conditions,
            "history": history, "last_cross_in_history": transitions[-1] if transitions else None,
            "weak_cross": (cross_up or cross_down) and abs(c["gap"]) < policy["weak_cross_gap_points"],
            "influences_ranking": False, "trade_confirmation": False,
            "basis": "仅完整K线的K/D状态；J=3K−2D，不作为独立第二因子加分；指标交叉不等于价格或买点确认"}


def from_frame(frame, period, source, complete_through, policy=None):
    from .technical import indicators
    import pandas as pd
    calculated = indicators(frame) if not {"k", "d"} <= set(frame) else frame
    rows = [{"date": t.isoformat(), "k": None if pd.isna(r.k) else float(r.k),
             "d": None if pd.isna(r.d) else float(r.d),
             "j": None if pd.isna(r.k) or pd.isna(r.d) else float(3*r.k-2*r.d)} for t, r in calculated.iterrows()]
    return classify_kd(rows, period, source, complete_through, policy)


def combine_kd(states):
    frames = {p: states.get(p) or unavailable(p) for p in PERIODS}
    groups = []
    for key, periods, label in (("monthly", ["monthly"], "月线战略"), ("weekly", ["weekly"], "周线趋势"),
                                ("daily", ["daily"], "日线确认"), ("structure", ["120", "60"], "120/60分钟结构组"),
                                ("execution", ["30", "15"], "30/15分钟执行组")):
        available = [p for p in periods if frames[p]["status"] == "ok"]
        directions = {frames[p]["direction"] for p in available}
        direction = ("conflict" if {"bullish", "bearish"} <= directions else
                     "bullish" if directions == {"bullish"} else "bearish" if directions == {"bearish"} else
                     "mixed" if available else "unavailable")
        groups.append({"group": key, "label": label, "periods": periods, "available_periods": available,
                       "direction": direction, "correlated": len(periods) > 1})
    bulls = [p for p in PERIODS if frames[p].get("direction") == "bullish"]
    bears = [p for p in PERIODS if frames[p].get("direction") == "bearish"]
    conflict = bool(bulls and bears)
    macro = all(frames[p].get("direction") == "bullish" for p in ("monthly", "weekly"))
    return {"version": VERSION, "frames": frames, "groups": groups, "data_complete": all(f["status"] == "ok" for f in frames.values()),
            "missing_periods": [p for p in PERIODS if frames[p]["status"] != "ok"], "bullish_periods": bulls, "bearish_periods": bears,
            "conflict": conflict, "label": "周期K/D分歧" if conflict else "月周K/D偏多，仍需原买点条件" if macro else "K/D状态观察，不独立确认买点",
            "influences_ranking": False, "orders_authorized": False, "trade_confirmation": False,
            "basis": "月周定方向、日线确认、分钟执行；120/60同源且相关，30/15亦相关，按组展示不按信号个数累加；J与K−D不重复计分"}


def observe_acquired(acquired, completed_day, reused=None, policy=None):
    """Reuse validated monitor bars; no market request and no change to MACD events."""
    from .technical import aggregate
    from .data import DataError
    policy = policy or load_policy()
    states = {}
    for p in PERIODS:
        source = acquired.get(p, {})
        try:
            if p in ("monthly", "weekly"):
                daily = acquired.get("daily", {})
                if daily.get("status") != "ok":
                    raise DataError("完整日线缺失，无法生成周/月K/D观察")
                frame = aggregate(daily["frame"], "ME" if p == "monthly" else "W-FRI", completed_day)
                source_name = "validated_qfq_daily_completed_period_aggregation"
                cutoff = str(completed_day)[:10]+"T23:59:59"
            else:
                if source.get("status") != "ok":
                    raise DataError(source.get("error") or "完整周期行情不可用")
                frame = (reused or {}).get(p, source["frame"])
                source_name, cutoff = source["source"], source["cutoff"]
            states[p] = from_frame(frame, p, source_name, cutoff, policy)
        except (DataError, ValueError, KeyError, IndexError) as exc:
            states[p] = unavailable(p, source.get("source"), str(exc))
    return combine_kd(states)
