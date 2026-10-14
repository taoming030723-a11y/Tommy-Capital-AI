"""Causal launch/pullback observations from completed OHLC bars; no order signals."""
import math

PROFILE_VERSION = "macro_launch_pullback_v1"
MA_PERIODS = (5, 13, 20, 55, 144, 233)


def numeric(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def macro_profile(calculated, period="daily", config=None):
    """Each tested support/breakout is known by the last bar, never a future low."""
    config = config or {}
    latest, previous = calculated.iloc[-1], calculated.iloc[-2]
    reference = calculated.iloc[-3]
    atr, close = numeric(latest.atr14), numeric(latest.close)
    averages = {str(p): numeric(latest.get(f"ma{p}")) for p in MA_PERIODS}
    slopes = {str(p): ((numeric(latest.get(f"ma{p}")) - numeric(reference.get(f"ma{p}"))) / atr
                      if atr is not None and atr > 0 and numeric(latest.get(f"ma{p}")) is not None and
                      numeric(reference.get(f"ma{p}")) is not None else None) for p in MA_PERIODS}
    missing = ["ma" + str(p) for p in (5, 13, 20) if averages[str(p)] is None or slopes[str(p)] is None]
    if any(numeric(latest[k]) is None or numeric(previous[k]) is None for k in ("k", "d", "j")):
        missing.append("kdj")
    if atr is None or atr <= 0:
        missing.append("atr14")
    if missing:
        return {"version": PROFILE_VERSION, "status": "unavailable", "missing": missing,
                "averages_qfq": averages, "slopes_atr": slopes, "period": period}
    j_rising = bool(latest.j > previous.j)
    cross = bool(latest.k > latest.d and previous.k <= previous.d)
    recent = calculated.iloc[-6:]
    recent_cross = bool(((recent.k > recent.d) & (recent.k.shift() <= recent.d.shift())).iloc[-3:].any())
    prior_low_j = float(calculated.j.iloc[-6:-1].min())
    kdj = {"k": float(latest.k), "d": float(latest.d), "j": float(latest.j),
           "previous_k": float(previous.k), "previous_d": float(previous.d), "previous_j": float(previous.j),
           "prior5_min_j": prior_low_j, "j_rising": j_rising,
           "k_above_d_and_rising": bool(latest.k > latest.d and latest.k > previous.k),
           "golden_cross": cross, "recent3_golden_cross": recent_cross,
           "low_zone_recovery": bool(prior_low_j < 20 and j_rising and latest.k > latest.d)}
    ma_conditions = {
        "fast_recovery": bool(close > averages["5"] and slopes["5"] >= .01),
        "short_alignment": bool(close > averages["13"] and averages["5"] > averages["13"] and slopes["13"] >= .01),
        "medium_alignment": bool(close > averages["20"] and averages["13"] > averages["20"] and slopes["20"] >= .01)}
    # A longer-term cost band can be reclaimed before every long MA rises.
    old_band = {str(p): numeric(previous.get(f"ma{p}")) for p in (20, 55, 144, 233)}
    old_band = {p: v for p, v in old_band.items() if v is not None and v > 0}
    if all(str(p) in old_band for p in (55, 144, 233)):
        old_band = {str(p): old_band[str(p)] for p in (55, 144, 233)}
    width = (max(old_band.values()) / min(old_band.values()) - 1) * 100 if len(old_band) >= 3 else None
    cluster_breakout = bool(width is not None and width <= 10 and previous.close <= max(old_band.values()) and
                            close > max(averages[p] for p in old_band) and ma_conditions["fast_recovery"])
    base = calculated.iloc[-7:-1]
    stable_base = bool(len(base) == 6 and base.low.iloc[-3:].min() >= base.low.iloc[:3].min() and
                       latest.low >= base.low.iloc[-3:].min())
    prior = calculated.iloc[-21:-1]
    platform_high = float(prior.high.max())
    platform_range = float((prior.high.max() / prior.low.min() - 1) * 100)
    location = float((latest.close - latest.low) / (latest.high - latest.low)) if latest.high > latest.low else 0.
    price_breakout = bool(len(prior) == 20 and close > platform_high and
                          platform_range <= config.get("daily_breakout_platform_range_pct", 20.) and
                          location >= config.get("daily_breakout_close_location", .65))
    supports = []
    for p in (13, 20, 55):
        line, old_line = averages[str(p)], numeric(previous.get(f"ma{p}"))
        if line is None or old_line is None or slopes[str(p)] is None:
            continue
        if (slopes[str(p)] >= 0 and previous.close >= old_line and
                line - .5 * atr <= latest.low <= line and
                close > line and close > previous.close + .02 * atr):
            supports.append(p)
    deviation = (close / averages["13"] - 1) * 100
    extension_atr = (close - averages["13"]) / atr
    threshold = {"monthly": 20., "weekly": 15., "daily": 12.}.get(period, 12.)
    extended = deviation >= threshold and extension_atr >= 2.
    fading = bool(latest.macd < previous.macd - .02 * atr and latest.j < previous.j and
                  calculated.j.iloc[-6:-1].max() > 80)
    broken = bool(close < calculated.low.iloc[-6:-1].min() and close < averages["13"])
    risk = bool(broken or extended and fading)
    label = ("支撑破坏" if broken else "扩张且动能转弱" if risk else "支撑回踩收回" if supports else
             "长期均线收敛区向上突破" if cluster_breakout else
             "窄平台价格突破" if price_breakout else "趋势扩张" if extended else
             "短中期均线转强" if ma_conditions["short_alignment"] else "等待均线结构确认")
    return {"version": PROFILE_VERSION, "status": "ok", "period": period,
        "last_bar": calculated.index[-1].isoformat(), "bar_count": len(calculated),
        "averages_qfq": averages, "slopes_atr": slopes, "ma_conditions": ma_conditions,
        "ma_cluster_breakout": cluster_breakout, "prior_long_ma_band_qfq": old_band,
        "prior_long_ma_band_width_pct": width, "base_lows_stabilizing": stable_base,
        "optional_long_ma_missing": [p for p in (55, 144, 233) if averages[str(p)] is None],
        "kdj": kdj, "price_platform_breakout": price_breakout,
        "platform_high_qfq": platform_high, "platform_range_pct": platform_range,
        "close_location": location, "pullback_reclaimed": bool(supports), "reclaimed_ma_periods": supports,
        "distance_ma13_pct": deviation, "distance_ma13_atr": extension_atr,
        "extension_threshold_pct": threshold, "extended": bool(extended),
        "momentum_fading": fading, "support_broken": broken, "chase_risk": risk, "label": label,
        "basis": "只用完整K线；MA5/13/20结构、可得MA55支撑、KDJ恢复、前20根窄平台价格突破或支撑回踩收回；日量不计分；长均线缺失不补造"}
