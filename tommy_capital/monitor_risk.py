"""Hierarchical risk observations, never order instructions or score changes."""
from .technical import indicators, aggregate


def resonance(frames):
    sides = {}
    for period, frame in frames.items():
        if frame.get("status") != "ok":
            continue
        sides[period] = {s["direction"] for s in frame.get("signals", []) if s["state"] in ("suspected","confirmed")}
    bearish = [p for p,v in sides.items() if "bearish" in v]
    bullish = [p for p,v in sides.items() if "bullish" in v]
    conflict = bool(bearish and bullish)
    direction = "bearish" if bearish and not bullish else "bullish" if bullish and not bearish else None
    periods = bearish if direction=="bearish" else bullish if direction=="bullish" else []
    higher = any(p in periods for p in ("120","60"))
    macro = "daily" in periods
    # 120/60 and 30/15 form correlated tiers; do not count each as independent evidence.
    if conflict:
        label, level = "周期信号分歧", "conflict"
    elif macro and higher:
        label, level = "日线与较大分钟周期共振", "macro_resonance"
    elif macro:
        label, level = "日线背离观察", "daily"
    elif higher and any(p in periods for p in ("30","15")):
        label, level = "分钟级共振，仍需日线结构确认", "intraday_resonance"
    elif periods:
        label, level = "局部周期背离观察", "local"
    elif any(f.get("signals") for f in frames.values()):
        label, level = "动能变化", "momentum_change"
    else:
        label, level = "正常" if all(f.get("status")=="ok" for f in frames.values()) else "数据未齐，无法完整判断", "normal"
    return {"label":label,"level":level,"direction":direction,"resonating_periods":periods,
            "conflicting_periods":{"bearish":bearish,"bullish":bullish} if conflict else {},
            "basis":"日线/120与60分钟/30与15分钟分层，邻近周期相关，不简单累加信号数"}


def price_structure(daily, cutoff, config):
    calc = indicators(daily)
    latest, previous = calc.iloc[-1], calc.iloc[-2]
    risk = config["risk"]
    history = calc.iloc[-risk["advance_lookback"]-1:-1]
    support_history = calc.iloc[-risk["support_lookback"]-1:-1]
    peak=int(history.high.to_numpy().argmax())
    floor = float(history.low.iloc[:peak].min()) if peak else float(history.low.iloc[0])
    advance = (float(history.high.iloc[peak])/floor-1)*100 if peak else 0.
    support = float(support_history.low.min())
    mean_volume = float(calc.volume.iloc[-21:-1].mean())
    ratio = float(latest.volume/mean_volume) if mean_volume>0 else None
    daily_return = (float(latest.close/previous.close)-1)*100
    breakout_level = float(calc.high.iloc[-22:-2].max())
    weekly = indicators(aggregate(daily,"W-FRI",cutoff))
    ma55 = float(weekly.ma55.iloc[-1]) if len(weekly)>=55 else None
    ma55_up = bool(weekly.ma55.iloc[-1] >= weekly.ma55.iloc[-4]) if len(weekly)>=58 else None
    from .macd_monitor import pivot_indices
    high_pivots=pivot_indices(calc,"bearish",3,complete=True)
    low_pivots=pivot_indices(calc,"bullish",3,complete=True)
    recent_low=low_pivots[-1] if low_pivots and len(calc)-1-low_pivots[-1]<=risk["support_lookback"] else None
    pullback=float(calc.low.iloc[recent_low]) if recent_low is not None else None
    important_support=max(support,pullback) if pullback is not None else support
    lower_high=bool(len(high_pivots)>=2 and calc.high.iloc[high_pivots[-1]]<calc.high.iloc[high_pivots[-2]])
    return {"as_of":daily.index[-1].isoformat(),"prior_window_advance_pct":advance,
        "material_advance":advance>=risk["material_advance_pct"],"support_qfq":support,
        "important_support_qfq":important_support,"confirmed_pullback_low_qfq":pullback,
        "confirmed_pullback_low_at":calc.index[recent_low].isoformat() if recent_low is not None else None,
        "short_support_lost":bool(latest.close<important_support),"daily_volume_ratio20":ratio,
        "high_volume_stagnation":bool(advance>=risk["material_advance_pct"] and latest.close>=history.high.max()*risk["high_zone_fraction"] and
            ratio is not None and ratio>=risk["high_volume_ratio"] and abs(daily_return)<=risk["stagnation_return_pct"]),
        "failed_breakout":bool(previous.close>breakout_level and latest.close<breakout_level),
        "daily_lower_high_confirmed":lower_high,
        "ma5_weak":bool(latest.close<latest.ma5 and latest.ma5<calc.ma5.iloc[-3]),
        "ma13_weak":bool(latest.close<latest.ma13 and latest.ma13<calc.ma13.iloc[-3]),
        "weekly_ma55_qfq":ma55,"weekly_ma55_rising":ma55_up,
        "weekly_ma55_status":"ok" if ma55 is not None and ma55_up is not None else "insufficient_bars",
        "below_weekly_ma55":bool(latest.close<ma55) if ma55 else None,
        "fundamental_risk":"未知；技术行情不能证明基本面恶化"}


def holding_risk(frames, structure, combined, config):
    def has(period, state=None, direction="bearish"):
        return any(s["direction"]==direction and (state is None or s["state"]==state)
                   for s in frames.get(period,{}).get("signals",[]))
    daily = has("daily","confirmed")
    larger = any(has(p,"confirmed") for p in ("120","60"))
    weaken = any(has(p) for p in ("daily","120","60"))
    support = structure.get("short_support_lost") or (structure.get("ma5_weak") and structure.get("ma13_weak")) or (structure.get("daily_lower_high_confirmed") and structure.get("failed_breakout"))
    reasons = [label for key,label in (("material_advance","已有明显阶段上涨"),("high_volume_stagnation","高位放量滞涨"),
        ("failed_breakout","突破后收回平台内"),("daily_lower_high_confirmed","日线更低高点已确认"),("short_support_lost","短期支撑失守"),("ma5_weak","日MA5走弱"),("ma13_weak","日MA13走弱")) if structure.get(key)]
    if structure.get("below_weekly_ma55") and structure.get("weekly_ma55_rising") is False and support:
        label="趋势结构失效"
    elif daily and support:
        label="价格结构确认转弱"
    elif daily and larger and not combined.get("conflicting_periods"):
        label="多周期风险共振"
    elif structure.get("material_advance") and (daily or weaken and (structure.get("high_volume_stagnation") or structure.get("failed_breakout"))):
        label="止盈预警"
    elif weaken:
        label="上涨动能减弱"
    else:
        label="正常持有观察"
    serious = label in ("止盈预警","多周期风险共振","价格结构确认转弱","趋势结构失效")
    message = ("建议关注是否需要按既定纪律部分减仓，约三分之一至一半仅供人工评估，保留底仓方案由本人决定；无自动卖单。" if serious else
               "保留底仓与局部做T需遵循原有持仓/趋势/支撑量价规则；本模块不授予交易资格。")
    if has("15") and not weaken:
        reasons.append("仅15分钟局部信号，不据此判断整轮上涨结束")
    missing=[p for p,f in frames.items() if f.get("status")!="ok"]
    if len(missing)==5:
        label="数据不足，无法完整判断"
    return {"label":label,"reasons":reasons,"message":message,"missing_periods":missing,
            "data_complete":not missing,"fundamental_risk":structure.get("fundamental_risk"),
            "partial_reduction_scenario": [config["risk"]["partial_reduction_low"],config["risk"]["partial_reduction_high"]] if serious else None,
            "orders_authorized":False}
