"""Strategic research scores with separately gated execution observations."""
import math
from .patterns import PROFILE_VERSION, WEEKLY_MA55_VERSION

SCORING_VERSION = "weekly_ma55_macd_50_50_v9"
RANKING_POLICY = "monthly_research_pool_macro_gated_execution"
DISPLAY_LIMIT = 20
FUNDAMENTAL_WEIGHTS = {"revenue": 4, "earnings_quality_or_leading": 14,
                       "cash_quality": 14, "valuation": 14, "improvement": 4}
TECHNICAL_WEIGHTS = {"monthly": 20, "weekly": 30}
WEEKLY_MA55_WEIGHTS = {"weekly_ma55_current_above": 5., "weekly_ma55_direction": 4.,
                       "weekly_ma55_recent_hold": 4., "weekly_ma55_retest_or_reclaim": 3.}
WEEKLY_WEIGHTS = {**WEEKLY_MA55_WEIGHTS,
                  "base_or_higher_low_or_contraction_or_breakout": 2.,
                  "ma5_13_20_structure": .5, "kdj_recovery": .5,
                  "volume_above_previous20_mean": 4., "macd_momentum": 1.,
                  "macd_histogram_state": 5., "confirmed_macd_bottom": 1.}
# Retain the existing 0.02 ATR significance floor; raw histogram size never
# rewards expensive stocks or the 2x display convention by itself.
WEEKLY_MACD_MIN_CHANGE_ATR = .02
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


def qualified_macd(signal, direction):
    return (signal.get("indicator") == "MACD_DIF" and signal.get("direction") == direction
            and signal.get("quality_passed") is True)


def financial_sector(row):
    # Published industry labels, never stock codes or company names.
    industry = str(row.get("industry", ""))
    return any(word in industry for word in ("银行", "证券", "保险", "多元金融", "金融服务"))


def confirmation_state(technical, route):
    """Observation eligibility and entry confirmation have different purposes."""
    recovery = technical.get("monthly_recovery", {})
    current = finite(technical.get("monthly_j"))
    previous = finite(recovery.get("previous_j"))
    monthly = current is not None and previous is not None and current > previous
    weekly = (technical.get("weekly_structure", {}).get("confirmed") is True
              and technical.get("weekly_trend") is True)
    volume = technical.get("first_volume_breakout", {}).get("active") is True
    volume = volume or (route == "A" and technical.get("daily_volume", {}).get("abnormal") is True)
    daily = technical.get("daily_trend") is True and volume
    if not monthly:
        stage, label = "monthly_observation", "月线低位观察，等待J拐头"
    elif not weekly:
        stage, label = "wait_weekly", "等待周线确认"
    elif not daily:
        stage, label = "wait_daily", "等待日线量价确认"
    else:
        stage, label = "confirmed", "周日量价已确认"
    return {"stage": stage, "label": label, "monthly_rising": monthly,
            "weekly_confirmed": weekly, "daily_confirmed": daily,
            "execution_score_enabled": monthly and weekly and daily}


def macd_points(observation, momentum_points, bottom_points):
    macd = observation.get("macd", {})
    dif, dea, hist, previous, atr = (finite(macd.get(k)) for k in
                                    ["dif", "dea", "histogram", "previous_histogram", "atr14"])
    # Tiny floating-point differences cannot earn the momentum allocation.
    valid = atr is not None and atr > 0
    momentum = momentum_points / 2 * int(valid and dif is not None and dea is not None and (dif - dea) / atr >= .02)
    momentum += momentum_points / 2 * int(valid and hist is not None and previous is not None and (hist - previous) / atr >= .02)
    signals = observation.get("signals", [])
    bottom = bottom_points if any(qualified_macd(s, "bullish") for s in signals) else 0.
    top = any(qualified_macd(s, "bearish") or (s.get("indicator") == "KDJ_J" and s.get("direction") == "bearish") for s in signals)
    return momentum, bottom, bottom_points if top else 0.


def weekly_ma55_parts(reference, daily_close):
    """Sixteen points for the user's weekly reference; entry gates stay separate."""
    daily_close, line = finite(daily_close), finite(reference.get("ma55_qfq"))
    parts = dict.fromkeys(WEEKLY_MA55_WEIGHTS, 0.)
    parts["weekly_ma55_lost_support_penalty"] = 0.
    valid = (reference.get("status") == "ok" and reference.get("version") == WEEKLY_MA55_VERSION and
             line is not None and line > 0 and daily_close is not None and daily_close > 0 and
             reference.get("direction") in ("rising", "flat", "falling") and
             reference.get("window") == 6 and isinstance(reference.get("above_count"), int) and
             0 <= reference["above_count"] <= 6 and isinstance(reference.get("consecutive_above"), int) and
             0 <= reference["consecutive_above"] <= reference["above_count"])
    if not valid:
        return parts, False
    above = reference.get("latest_week_above") is True and daily_close > line
    if not above:
        parts["weekly_ma55_lost_support_penalty"] = -2.
        return parts, True
    parts["weekly_ma55_current_above"] = WEEKLY_MA55_WEIGHTS["weekly_ma55_current_above"]
    parts["weekly_ma55_direction"] = {"rising": 1., "flat": .5, "falling": 0.}[reference["direction"]] * WEEKLY_MA55_WEIGHTS["weekly_ma55_direction"]
    count, consecutive = reference["above_count"], reference["consecutive_above"]
    held = 1. if count >= 4 and consecutive >= 3 else .5 if count >= 3 and consecutive >= 2 else 0.
    parts["weekly_ma55_recent_hold"] = held * WEEKLY_MA55_WEIGHTS["weekly_ma55_recent_hold"]
    if reference["direction"] != "falling" and (reference.get("retest") or reference.get("reclaim")):
        parts["weekly_ma55_retest_or_reclaim"] = WEEKLY_MA55_WEIGHTS["weekly_ma55_retest_or_reclaim"]
    return parts, True


def weekly_macd_state(observation):
    """Last two completed weeks only: momentum state is not a divergence signal."""
    macd = observation.get("macd", {})
    dif, dea, hist, previous, atr = (finite(macd.get(k)) for k in
                                    ("dif", "dea", "histogram", "previous_histogram", "atr14"))
    parts = {"macd_histogram_state": 0., "macd_momentum": 0.}
    result = {"status": "unavailable", "state": "unknown", "label": "周线MACD数据不足，无法判断",
              "parts": parts, "last_bar": observation.get("last_bar"),
              "minimum_change_atr": WEEKLY_MACD_MIN_CHANGE_ATR,
              "basis": "最近两个完整周；MACD柱=2×(DIF−DEA)，按ATR归一化；柱体状态不等于背离确认或买点"}
    if observation.get("status") != "ok" or any(x is None for x in (dif, dea, hist, previous, atr)) or atr <= 0:
        return result
    size, change = hist / atr, (hist - previous) / atr
    floor = WEEKLY_MACD_MIN_CHANGE_ATR
    if size > floor:
        if change >= floor:
            state, label, fraction = ("turning_red", "绿转红增强", 1.) if previous <= 0 else ("red_expanding", "红柱增强", 1.)
        elif change <= -floor:
            state, label, fraction = "red_contracting", "红柱衰减", .3
        else:
            state, label, fraction = "red_flat", "红柱变化不显著", .6
    elif size < -floor:
        if previous >= 0:
            state, label, fraction = "turning_green", "红转绿走弱", 0.
        elif change >= floor:
            state, label, fraction = "green_contracting", "绿柱收敛，等待转强", .6
        elif change <= -floor:
            state, label, fraction = "green_expanding", "绿柱扩张", 0.
        else:
            state, label, fraction = "green_flat", "绿柱变化不显著", 0.
    else:
        state, label, fraction = "near_zero", "零轴附近，动能待确认", 0.
    parts["macd_histogram_state"] = fraction * WEEKLY_WEIGHTS["macd_histogram_state"]
    parts["macd_momentum"] = WEEKLY_WEIGHTS["macd_momentum"] * int((dif - dea) / atr >= floor)
    return {**result, "status": "ok", "state": state, "label": label,
            "histogram": hist, "previous_histogram": previous,
            "histogram_atr": size, "histogram_change_atr": change}


def balanced_score(row, technical, route, leading=None, minute_frames=None):
    """No missing-field rescaling, negative-profit exemption, or ticker-specific rule."""
    leading, minute_frames = leading or {}, minute_frames or {}
    missing = []
    def value(field):
        result = finite(row.get(field))
        if result is None:
            missing.append("fundamental." + field)
        return result

    revenue = scale(value("revenue_yoy"), 40, 4)
    profit_ttm, revenue_ttm = value("profit_ttm"), value("revenue_ttm")
    core_ttm, core_ytd = value("core_profit_ttm"), value("core_profit_ytd")
    prior_core, prior_profit = value("previous_same_core_profit_ytd"), value("previous_same_profit_ytd")
    prior_revenue, current_revenue = value("previous_same_revenue_ytd"), value("revenue_ytd")
    roe = value("roe_ytd")
    prior_margin = prior_profit / prior_revenue if prior_profit is not None and prior_revenue is not None and prior_revenue > 0 else None
    low_base = prior_margin is not None and prior_margin < .01
    core_share = core_ttm / profit_ttm if core_ttm is not None and profit_ttm is not None and profit_ttm > 0 else None
    core_margin = core_ttm / revenue_ttm if core_ttm is not None and revenue_ttm is not None and revenue_ttm > 0 else None
    current_core_margin = core_ytd / current_revenue if core_ytd is not None and current_revenue is not None and current_revenue > 0 else None
    prior_core_margin = prior_core / prior_revenue if prior_core is not None and prior_revenue is not None and prior_revenue > 0 else None
    core_improving = (current_core_margin is not None and prior_core_margin is not None and
                      current_core_margin >= prior_core_margin and core_ytd >= prior_core)
    recovering_loss = route=="B" and leading.get("variant")=="loss_recovery" and leading.get("loss_recovery",{}).get("passed") is True
    if recovering_loss:
        reduction=leading["loss_recovery"].get("min_reduction_fraction")
        earnings=scale(reduction,.5,12)+2.*int(core_improving)
    elif route == "B":
        earnings = 8. * int(leading.get("leading_indicator_confirmed") is True)
        earnings += 3. * int(leading.get("core_improving") is True) + 3. * int(core_improving)
    else:
        earnings = scale(core_share, 1, 8) + scale(core_margin, .10, 2) + scale(roe, 15, 2) + 2. * int(core_improving)
    sector_specific = financial_sector(row)
    cfo_ytd, cfo_ttm = value("cfo_ytd"), value("cfo_ttm")
    previous_cash = value("previous_same_cfo_ytd")
    stable = cfo_ytd is not None and previous_cash is not None and cfo_ytd >= previous_cash
    positive = cfo_ytd is not None and cfo_ytd > 0
    cash_conversion = cfo_ttm / profit_ttm if cfo_ttm is not None and profit_ttm is not None and profit_ttm > 0 else None
    cash_quality = 2. * int(positive) + 2. * int(positive and stable) + 2. * int(cfo_ttm is not None and cfo_ttm > 0)
    if route == "B" and profit_ttm is not None and profit_ttm <= 0:
        # Loss-route cash generation is measured against actual revenue, not negative profit.
        cash_quality += scale(cfo_ttm / revenue_ttm if cfo_ttm is not None and revenue_ttm is not None and revenue_ttm > 0 else None, .10, 8)
    else:
        cash_quality += scale(cash_conversion, 1, 8)
    if sector_specific:
        # Deposits/trading inventory/provisions make ordinary CFO/net-income
        # conversion unsuitable. Do not manufacture a bank safety score or
        # redistribute its weight when regulatory/risk evidence is absent.
        cash_quality = 0.
        missing.append("fundamental.financial_sector_risk_review")
    if recovering_loss:
        ps=finite(leading["loss_recovery"].get("ps_ttm"));pb=value("pb")
        valuation=max(0.,1-ps/leading["loss_recovery"]["max_ps"])*9 if ps is not None and ps>0 else 0.
        valuation+=max(0.,1-pb/leading["loss_recovery"]["max_pb"])*5 if pb is not None and pb>0 else 0.
        improvement=4.*int(leading["loss_recovery"].get("passed") is True)
    elif route == "B":
        valuation_data = leading.get("valuation", {})
        if valuation_data.get("passed") is True:
            margin, downside = finite(valuation_data.get("base_margin_pct")), finite(valuation_data.get("bear_downside_pct"))
            valuation = scale(margin, 50, 9) + (max(0., 1 - downside / 30) * 5 if downside is not None else 0.)
        else:
            valuation = 0.
            missing.append("fundamental.reviewed_b_valuation")
        improvement = 2. * int(leading.get("core_improving") is True) + 2. * int(leading.get("future_catalyst_confirmed") is True)
    else:
        pe, pb, ratio = value("pe_ttm"), value("pb"), value("industry_pe_ratio")
        valuation = (max(0., 1 - pe / 60) * 5 if pe is not None and pe > 0 else 0.)
        valuation += max(0., 1 - pb / 8) * 3 if pb is not None and pb > 0 else 0.
        valuation += max(0., min(1., (1.5 - ratio) / 1.5)) * 6 if ratio is not None and ratio > 0 else 0.
        # Absolute same-period core earnings and margins precede the percentage rate.
        core_growth = (core_ytd / prior_core - 1) * 100 if core_ytd is not None and prior_core is not None and prior_core > 0 else None
        improvement = 2. * int(core_improving and core_ytd > 0)
        improvement += scale(core_growth, 50, 1 if low_base else 2)
    fundamentals = dict(zip(FUNDAMENTAL_WEIGHTS, [revenue, earnings, cash_quality, valuation, improvement]))
    quality = {"cash_conversion_ttm": cash_conversion, "core_profit_share_ttm": core_share,
               "core_profit_margin_ttm": core_margin, "previous_same_profit_margin": prior_margin,
               "low_base_flag": low_base, "core_margin_improving": bool(core_improving),
               "financial_sector": sector_specific,
               "cash_conversion_applicable": not sector_specific,
               "cash_quality_status": "金融行业专门风险指标待核验，不套用通用现金流评分" if sector_specific else "通用报表总额口径",
               "basis": "实际经营现金流总额/归母利润总额；同报告期扣非盈利与同期利润率，不使用现金流/股除以EPS"}

    observations = technical.get("timeframes", {})
    recovery = technical.get("monthly_recovery", {})
    current, previous = finite(technical.get("monthly_j")), finite(recovery.get("previous_j"))
    minimum = finite(recovery.get("min_j_6_months" if route == "B" else "min_j_3_months"))
    rising = current is not None and previous is not None and current > previous
    path = current is not None and ((route == "A" and current < 20) or (minimum is not None and minimum < 20 and rising))
    confirmation = confirmation_state(technical, route)
    periods = {}
    for period, maximum in TECHNICAL_WEIGHTS.items():
        observation = observations.get(period, {})  # Ranking reads only completed monthly/weekly inputs.
        parts, weekly_macd = {}, None
        if observation.get("status") != "ok":
            missing.append("technical." + period)
        else:
            if observation.get("divergence_status") == "insufficient_bars":
                missing.append("technical." + period + ".macd_divergence_history")
            if any(finite(observation.get("macd", {}).get(k)) is None for k in ["dif", "dea", "histogram", "previous_histogram", "atr14"]):
                missing.append("technical." + period + ".macd")
            profile = observation.get("macro_profile", {})
            if period in ("monthly", "weekly", "daily"):
                if profile.get("status") != "ok" or profile.get("version") != PROFILE_VERSION:
                    missing.append("technical." + period + ".macro_profile")
                    profile = {}
                conditions = profile.get("ma_conditions", {})
                ma = (1. * int(conditions.get("fast_recovery") is True) +
                      2. * int(conditions.get("short_alignment") is True) +
                      1. * int(conditions.get("medium_alignment") is True or profile.get("ma_cluster_breakout") is True))
                kdj = profile.get("kdj", {})
                recovered = (kdj.get("low_zone_recovery") is True or
                             kdj.get("recent3_golden_cross") is True and kdj.get("j_rising") is True)
                kdj_points = (1. * int(kdj.get("j_rising") is True) +
                              1. * int(kdj.get("k_above_d_and_rising") is True) +
                              1. * int(recovered and kdj.get("k_above_d_and_rising") is True))
            if period == "monthly":
                parts = {"oversold_or_recovery_path": 3. * int(path),
                         "base_lows_stabilizing": 1. * int(profile.get("base_lows_stabilizing") is True), "ma5_13_20_structure": ma,
                         "kdj_recovery": kdj_points * 2 / 3}
                momentum_max, bottom_max = 1, 1
            elif period == "weekly":
                parts, ma55_valid = weekly_ma55_parts(profile.get("weekly_ma55", {}), technical.get("daily_close_qfq"))
                if not ma55_valid:
                    missing.append("technical.weekly.ma55_reference")
                parts.update(base_or_higher_low_or_contraction_or_breakout=WEEKLY_WEIGHTS["base_or_higher_low_or_contraction_or_breakout"] * int(technical.get("weekly_structure", {}).get("confirmed") is True),
                             ma5_13_20_structure=ma / 4 * WEEKLY_WEIGHTS["ma5_13_20_structure"],
                             kdj_recovery=kdj_points / 3 * WEEKLY_WEIGHTS["kdj_recovery"])
                momentum_max, bottom_max = 0, WEEKLY_WEIGHTS["confirmed_macd_bottom"]
            if period in ("monthly", "weekly"):
                volume = observation.get("volume_ma20")
                # Verified older artifacts already retain this same causal
                # ratio. Reuse it during a model review without inventing bars.
                ratio = finite(volume.get("ratio20")) if volume is not None else finite(observation.get("structure", {}).get("ratio20"))
                if ratio is None or ratio < 0 or (volume is not None and volume.get("status") != "ok"):
                    missing.append("technical." + period + ".volume_ma20")
                volume_max = 4 if period == "monthly" else WEEKLY_WEIGHTS["volume_above_previous20_mean"]
                parts["volume_above_previous20_mean"] = scale(ratio - 1 if ratio is not None else None, 1, volume_max)
            parts["extension_or_support_risk_penalty"] = -3. * int(profile.get("chase_risk") is True)
            momentum, bottom, penalty = macd_points(observation, momentum_max, bottom_max)
            parts.update(macd_momentum=momentum, confirmed_macd_bottom=bottom, top_divergence_penalty=-penalty)
            if period=="monthly":
                parts={k:v*1.25 if v>0 else v for k,v in parts.items()}
            elif period=="weekly":
                weekly_macd = weekly_macd_state(observation)
                parts.update(weekly_macd["parts"])
                if weekly_macd["status"] != "ok" and "technical.weekly.macd" not in missing:
                    missing.append("technical.weekly.macd")
        total = min(float(maximum), max(0., sum(parts.values())))
        raw_total = total
        enabled = True  # Ranking contains only completed monthly/weekly inputs.
        if not enabled:
            total = 0.
        periods[period] = {"score": round(total, 2), "max": maximum, "parts": {k: round(v, 2) for k, v in parts.items()},
                           "raw_score": round(raw_total, 2), "counted_for_rank": enabled,
                           "status": observation.get("status", "not_requested"), "last_bar": observation.get("last_bar")}
        if period == "weekly":
            periods[period]["component_maxima"] = dict(WEEKLY_WEIGHTS)
            periods[period]["macd_state"] = {k: v for k, v in (weekly_macd or weekly_macd_state({})).items() if k != "parts"}
    fundamentals = {k: round(min(FUNDAMENTAL_WEIGHTS[k], max(0., v)), 2) for k, v in fundamentals.items()}
    fundamental_score = round(sum(fundamentals.values()), 2)
    technical_score = round(sum(v["score"] for v in periods.values()), 2)
    return {"score": round(fundamental_score + technical_score, 2), "fundamental_score": fundamental_score,
            "technical_score": technical_score, "scoring_version": SCORING_VERSION,
            "score_inputs_complete": not missing, "score_missing_inputs": missing,
            "confirmation_state": confirmation,
            "ranking_purpose": "strategic_research_not_entry",
            "fundamental_quality": quality, "score_breakdown": {"fundamental": fundamentals, "technical": periods},
            "profit_status": "TTM亏损，盈利尚未兑现" if profit_ttm is not None and profit_ttm < 0 else
                             "TTM零利润，盈利尚未兑现" if profit_ttm == 0 else "TTM盈利" if profit_ttm is not None else "TTM缺失"}


def ranking_eligible(row):
    """Research eligibility never confers entry/T eligibility, nor fills missing data."""
    return (row.get("eligible") is True and row.get("score_inputs_complete") is True
            and finite(row.get("score")) is not None)


def confirmed_ranking_eligible(row):
    technical = row.get("technical", {})
    return (ranking_eligible(row) and row.get("strategic_eligible") is True
            and confirmation_state(technical, row.get("route"))["execution_score_enabled"])


def ranked_top20(rows):
    return sorted((r for r in rows if ranking_eligible(r)), key=lambda r: (-float(r["score"]), r["code"]))[:DISPLAY_LIMIT]
