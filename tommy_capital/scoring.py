"""Bounded research scores, independent of eligibility and execution rights."""
import math

SCORING_VERSION = "balanced_50_50_v1"
DISPLAY_LIMIT = 20
FUNDAMENTAL_WEIGHTS = {"revenue": 10, "earnings_or_leading": 10,
                       "cash_quality": 10, "valuation": 15, "improvement": 5}
TECHNICAL_WEIGHTS = {"monthly": 12, "weekly": 10, "daily": 10,
                     "60": 8, "30": 5, "15": 5}
SCORE_WEIGHTS = {"fundamental": FUNDAMENTAL_WEIGHTS, "technical": TECHNICAL_WEIGHTS}


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def scale(value, maximum, points):
    value = finite(value)
    return min(1., max(0., value / maximum)) * points if value is not None else 0.


def macd_points(observation, momentum_points, bottom_points):
    macd = observation.get("macd", {})
    dif, dea, hist, previous = (finite(macd.get(k)) for k in
                                ["dif", "dea", "histogram", "previous_histogram"])
    momentum = momentum_points / 2 * int(dif is not None and dea is not None and dif > dea)
    momentum += momentum_points / 2 * int(hist is not None and previous is not None and hist > previous)
    signals = observation.get("signals", [])
    bottom = bottom_points if any(s.get("indicator") == "MACD_DIF" and
                                 s.get("direction") == "bullish" for s in signals) else 0.
    # KDJ remains visible, but only confirmed MACD bottoms earn this component.
    top = any(s.get("direction") == "bearish" for s in signals)
    penalty = bottom_points if top else 0.
    return momentum, bottom, penalty


def balanced_score(row, technical, route, leading=None, minute_frames=None):
    """Score eligible stocks only; unknown observations earn zero, never rescale.

    A retains actual earnings/PE; B uses independently verified business facts
    and reviewed valuation scenarios. Neither score grants eligibility.
    """
    leading, minute_frames = leading or {}, minute_frames or {}
    missing = []
    def value(field):
        result = finite(row.get(field))
        if result is None:
            missing.append("fundamental." + field)
        return result

    revenue = scale(value("revenue_yoy"), 40, 10)
    profit_growth, profit_ttm = value("profit_yoy"), value("profit_ttm")
    if route == "B":
        earnings = 8. * int(leading.get("leading_indicator_confirmed") is True)
        earnings += 2. * int(profit_ttm is not None and profit_ttm > 0 and
                            profit_growth is not None and profit_growth >= 0)
    else:
        earnings = scale(profit_growth, 60, 10) if profit_ttm is not None and profit_ttm > 0 else 0.
    cash, previous_cash = value("cfo_per_share_ytd"), value("previous_same_cfo_per_share_ytd")
    cash_quality = 5. * int(cash is not None and cash > 0)
    stable = cash is not None and previous_cash is not None and cash > 0 and cash >= previous_cash
    cash_quality += 3. * int(stable)
    if stable:
        cash_quality += 2. if previous_cash <= 0 else scale((cash / previous_cash - 1) * 100, 50, 2)
    if route == "B":
        valuation_data = leading.get("valuation", {})
        if valuation_data.get("passed") is True:
            margin, downside = finite(valuation_data.get("base_margin_pct")), finite(valuation_data.get("bear_downside_pct"))
            valuation = scale(margin, 50, 10) + (max(0., 1 - downside / 30) * 5 if downside is not None else 0.)
        else:
            valuation = 0.
            missing.append("fundamental.reviewed_b_valuation")
        improvement = 3. * int(leading.get("core_improving") is True) + 2. * int(leading.get("future_catalyst_confirmed") is True)
    else:
        pe, pb, ratio = value("pe_ttm"), value("pb"), value("industry_pe_ratio")
        valuation = (max(0., 1 - pe / 60) * 5 if pe is not None and pe > 0 else 0.)
        valuation += max(0., 1 - pb / 8) * 3 if pb is not None and pb > 0 else 0.
        valuation += max(0., min(1., (1.5 - ratio) / 1.5)) * 7 if ratio is not None and ratio > 0 else 0.
        improvement = 5. * int(row.get("fundamental_improving") is True)
    fundamentals = dict(zip(FUNDAMENTAL_WEIGHTS, [revenue, earnings, cash_quality, valuation, improvement]))

    observations = technical.get("timeframes", {})
    recovery = technical.get("monthly_recovery", {})
    current, previous = finite(technical.get("monthly_j")), finite(recovery.get("previous_j"))
    minimum = finite(recovery.get("min_j_6_months" if route == "B" else "min_j_3_months"))
    rising = current is not None and previous is not None and current > previous
    path = current is not None and ((route == "A" and current < 20) or
                                   (minimum is not None and minimum < 20 and rising))
    periods = {}
    for period, maximum in TECHNICAL_WEIGHTS.items():
        observation = observations.get(period, {}) if period in ["monthly", "weekly", "daily"] else minute_frames.get(period, {})
        parts = {}
        if observation.get("status") != "ok":
            missing.append("technical." + period)
        else:
            if observation.get("divergence_status") == "insufficient_bars":
                missing.append("technical." + period + ".macd_divergence_history")
            if any(finite(observation.get("macd", {}).get(k)) is None for k in
                   ["dif", "dea", "histogram", "previous_histogram"]):
                missing.append("technical." + period + ".macd")
            if period == "monthly":
                parts = {"oversold_or_recovery_path": 5. * int(path), "j_rising": 3. * int(rising),
                         "trend": 2. * int(technical.get("monthly_trend") is True)}
                momentum_max, bottom_max = 1, 1
            elif period == "weekly":
                parts = {"base_or_higher_low_or_contraction_or_breakout": 4. * int(technical.get("weekly_structure", {}).get("confirmed") is True),
                         "trend": 2. * int(technical.get("weekly_trend") is True)}
                momentum_max, bottom_max = 2, 2
            elif period == "daily":
                first, volume = technical.get("first_volume_breakout", {}), technical.get("daily_volume", {})
                volume_points = 4. if first.get("active") else 3. if volume.get("abnormal") else scale(volume.get("ratio20"), 2, 1.5)
                parts = {"volume_or_first_breakout": volume_points, "trend": 2. * int(technical.get("daily_trend") is True)}
                momentum_max, bottom_max = 2, 2
            else:
                structure_max = 3 if period == "60" else 2
                structure = observation.get("structure", {})
                parts = {"price_above_ma20": structure_max / 2 * int(observation.get("above_ma20") is True),
                         "ma20_rising": structure_max / 2 * int(structure.get("ma20_rising") is True)}
                momentum_max, bottom_max = (2, 3) if period == "60" else (1, 2)
            momentum, bottom, penalty = macd_points(observation, momentum_max, bottom_max)
            parts.update(macd_momentum=momentum, confirmed_macd_bottom=bottom, top_divergence_penalty=-penalty)
            if period in ["60", "30", "15"] and observation.get("structure", {}).get("overheated"):
                parts["overheat_penalty"] = -1.
        total = min(float(maximum), max(0., sum(parts.values())))
        periods[period] = {"score": round(total, 2), "max": maximum,
                           "parts": {k: round(v, 2) for k, v in parts.items()},
                           "status": observation.get("status", "not_requested"), "last_bar": observation.get("last_bar")}
    fundamentals = {k: round(v, 2) for k, v in fundamentals.items()}
    fundamental_score = round(sum(fundamentals.values()), 2)
    technical_score = round(sum(v["score"] for v in periods.values()), 2)
    return {"score": round(fundamental_score + technical_score, 2),
            "fundamental_score": fundamental_score, "technical_score": technical_score,
            "scoring_version": SCORING_VERSION, "score_inputs_complete": not missing,
            "score_missing_inputs": missing,
            "score_breakdown": {"fundamental": fundamentals, "technical": periods},
            "profit_status": "TTM亏损，盈利尚未兑现" if profit_ttm is not None and profit_ttm < 0 else
                             "TTM零利润，盈利尚未兑现" if profit_ttm == 0 else "TTM盈利" if profit_ttm is not None else "TTM缺失"}


def ranked_top20(rows):
    return sorted((r for r in rows if finite(r.get("score")) is not None),
                  key=lambda r: (-float(r["score"]), r["code"]))[:DISPLAY_LIMIT]
