import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from .data import DataError, require, daily_history


def bars(raw, timestamp="日期"):
    mapping = {timestamp: "date", "开盘": "open", "收盘": "close", "最高": "high",
               "最低": "low", "成交量": "volume"}
    require(raw, mapping, "K线")
    if "成交额" in raw:
        mapping["成交额"] = "amount"
    df = raw.rename(columns=mapping)[list(mapping.values())].copy()
    df.date = pd.to_datetime(df.date, errors="coerce")
    for col in ["open", "close", "high", "low", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    core = df[["date", "open", "close", "high", "low", "volume"]]
    if core.isna().any().any() or not np.isfinite(core.select_dtypes("number")).all().all():
        raise DataError("K线含缺失或无穷数值")
    if ((df.high < df.low) | (df.high < df.close) | (df.low > df.close) |
        (df.volume < 0) | (df.close <= 0)).any():
        raise DataError("K线含异常价格/成交量")
    return df.sort_values("date").drop_duplicates("date").set_index("date")


def aggregate(daily, frequency, session):
    grouped = daily.resample(frequency).agg({"open": "first", "close": "last", "high": "max",
                                            "low": "min", "volume": "sum"}).dropna()
    # Labels describe period end, so unfinished week/month is excluded.
    return grouped[grouped.index.normalize() <= pd.Timestamp(session)]


def indicators(df):
    result = df.copy()
    close = df.close
    result["ma20"] = close.rolling(20).mean()
    result["ma60"] = close.rolling(60).mean()
    result["dif"] = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    result["dea"] = result.dif.ewm(span=9, adjust=False).mean()
    result["macd"] = 2 * (result.dif - result.dea)
    true_range = pd.concat([df.high - df.low, (df.high - close.shift()).abs(),
                            (df.low - close.shift()).abs()], axis=1).max(axis=1)
    result["atr14"] = true_range.rolling(14).mean()
    low, high = df.low.rolling(9).min(), df.high.rolling(9).max()
    rsv = 100 * (close - low) / (high - low).replace(0, np.nan)
    # Seed at 50; carry 50 on an entirely flat window.
    k, d = 50.0, 50.0
    ks, ds = [], []
    for position, value in enumerate(rsv):
        if position < 8:
            ks.append(np.nan); ds.append(np.nan)
            continue
        if pd.isna(value):
            value = 50.0
        k = 2 / 3 * k + value / 3
        d = 2 / 3 * d + k / 3
        ks.append(k); ds.append(d)
    result["k"], result["d"] = ks, ds
    result["j"] = 3 * result.k - 2 * result.d
    return result


def daily_volume_observation(daily, threshold=2.0):
    """Latest completed day divided by the *preceding* 20 trading bars."""
    if len(daily) < 21:
        raise DataError("日线放量检查缺少前20个完整交易日")
    baseline = daily.volume.iloc[-21:-1]
    if not np.isfinite(baseline).all() or baseline.mean() <= 0 or not np.isfinite(daily.volume.iloc[-1]):
        raise DataError("日线前20个交易日平均成交量缺失/非正，无法判断放量")
    ratio = float(daily.volume.iloc[-1] / baseline.mean())
    return {"status": "ok", "abnormal": ratio >= threshold, "ratio20": ratio,
            "threshold": threshold, "volume": float(daily.volume.iloc[-1]),
            "previous20_mean_volume": float(baseline.mean()), "baseline_bars": 20,
            "baseline_start": baseline.index[0].date().isoformat(),
            "baseline_end": baseline.index[-1].date().isoformat(),
            "confirmed_at": daily.index[-1].date().isoformat(),
            "basis": "最新完整日线成交量 / 此前20个交易日平均成交量；不含检测当日"}


def weekly_base_observation(weekly, daily, max_range_pct=20.0, low_tolerance_pct=3.0,
                            max_volume_ratio=1.1):
    """Observable six-week base proxy; no claim about hidden investor intent."""
    recent = weekly.tail(6)
    if len(recent) < 6:
        raise DataError("周线筑底观察不足6个完整周")
    previous, current = recent.iloc[:3], recent.iloc[3:]
    range_pct = float((recent.high.max() / recent.low.min() - 1) * 100)
    low_change_pct = float((current.low.min() / previous.low.min() - 1) * 100)
    # Holiday weeks have fewer sessions: compare daily average volume within
    # each completed week instead of interpreting fewer days as quiet trading.
    days = daily.close.resample("W-FRI").count().reindex(recent.index)
    daily_mean_volume = recent.volume / days
    if days.isna().any() or (days <= 0).any() or daily_mean_volume.iloc[:3].mean() <= 0:
        raise DataError("周线筑底的交易日数/成交量基准缺失")
    volume_ratio = float(daily_mean_volume.iloc[3:].mean() / daily_mean_volume.iloc[:3].mean())
    conditions = {"range_compressed": range_pct <= max_range_pct,
                  "lows_stable": low_change_pct >= -low_tolerance_pct,
                  "close_holds_base": bool(recent.close.iloc[-1] >= recent.close.mean()),
                  "volume_quiet": volume_ratio <= max_volume_ratio}
    last_trading = daily.loc[daily.index <= recent.index[-1]].index[-1].date().isoformat()
    return {"status": "ok", "detected": all(conditions.values()), "conditions": conditions,
            "window_weeks": 6, "range_pct": range_pct, "low_change_pct": low_change_pct,
            "daily_average_volume_ratio": volume_ratio,
            "max_range_pct": max_range_pct, "low_tolerance_pct": low_tolerance_pct,
            "max_volume_ratio": max_volume_ratio,
            "period_end": recent.index[-1].date().isoformat(), "last_trading_day": last_trading,
            "basis": "6个完整周窄幅、低点稳定、收盘守住区间均值、日均量平稳；仅为筑底蓄力观察代理"}


def monthly_recovery_observation(monthly):
    """Use completed monthly bars only; recovery is a path, not J<30/40."""
    values = monthly.j.tail(6)
    if len(values) < 6 or not np.isfinite(values).all():
        raise DataError("月J恢复路径缺少6个完整月份的有限J值")
    current, previous = float(values.iloc[-1]), float(values.iloc[-2])
    rising = current > previous
    current_low = current < 20
    recovery3 = bool(values.tail(3).min() < 20 and rising)
    recovery6 = bool(values.min() < 20 and rising)
    return {"status": "ok", "current_j": current, "previous_j": previous,
            "min_j_3_months": float(values.tail(3).min()), "min_j_6_months": float(values.min()),
            "current_oversold": current_low, "rising": rising,
            "route_a_path": current_low or recovery3, "route_b_path": recovery6,
            "recovering_3_months": recovery3, "recovering_6_months": recovery6,
            "history": [{"period_end": index.date().isoformat(), "j": float(value)}
                        for index, value in values.items()],
            "basis": "A：当前J<20或最近3个完整月曾J<20且当前J上升；B：最近6个完整月曾J<20且当前J上升"}


def weekly_structure_observation(weekly, base, max_range_pct=20.0):
    recent = weekly.tail(7)
    if len(recent) < 7:
        raise DataError("周线结构少于7个完整周")
    previous, current = recent.iloc[-6:-3], recent.iloc[-3:]
    prior_range = float(previous.high.max() / previous.low.min() - 1)
    current_range = float(current.high.max() / current.low.min() - 1)
    stable_close = bool(current.close.iloc[-1] >= current.close.mean())
    higher_low = bool(current.low.min() > previous.low.min() and stable_close)
    contraction = bool(prior_range > 0 and current_range < prior_range and
                       current_range * 100 <= max_range_pct and stable_close and
                       current.low.min() >= previous.low.min())
    platform = float(recent.high.iloc[:-1].max())
    breakout = bool(recent.close.iloc[-1] > platform)
    conditions = {"base": base["detected"], "higher_low": higher_low,
                  "volatility_contraction": contraction, "platform_breakout": breakout}
    return {"status": "ok", "confirmed": any(conditions.values()), "conditions": conditions,
            "previous3_range_pct": prior_range * 100, "recent3_range_pct": current_range * 100,
            "platform_high_qfq": platform, "period_end": recent.index[-1].date().isoformat(),
            "basis": "完整周线：原筑底代理/后3周低点抬高且守住均值/不创新低的波动收敛/突破此前6周高点，至少一项；不推断主力吸筹"}


def first_volume_breakout(daily, rvol_threshold=1.5, close_location_min=0.65,
                          lookback=20, max_platform_range_pct=20.0):
    """Causal event detection: every baseline excludes its tested bar.

    A new alert requires a quiet 20-session price platform. A continuing rally
    does not repeatedly become a 'first' breakout. Retain the first event and
    test whether its platform is still held with the latest completed close.
    """
    if len(daily) < 21:
        raise DataError("首次放量突破缺少此前20个完整交易日")
    events = []
    last_event_position = -1000
    start = max(20, len(daily) - int(lookback))
    for position in range(start, len(daily)):
        bar = daily.iloc[position]
        prior = daily.iloc[position - 20:position]
        mean = float(prior.volume.mean())
        if mean <= 0 or not np.isfinite(mean):
            continue
        rvol = float(bar.volume / mean)
        width = float((prior.high.max() / prior.low.min() - 1) * 100)
        location = float((bar.close - bar.low) / (bar.high - bar.low)) if bar.high > bar.low else 0.0
        high = float(prior.high.max())
        if (position - last_event_position >= 20 and rvol >= rvol_threshold and
                bar.close > high and location >= close_location_min and width <= max_platform_range_pct):
            events.append({"confirmed_at": daily.index[position].date().isoformat(), "ratio20": rvol,
                           "close_location": location, "platform_high_qfq": high, "platform_range_pct": width,
                           "close_qfq": float(bar.close), "baseline_start": prior.index[0].date().isoformat(),
                           "baseline_end": prior.index[-1].date().isoformat()})
            last_event_position = position
    first = events[0] if events else None
    held = bool(first and daily.close.iloc[-1] >= first["platform_high_qfq"])
    return {"status": "ok", "detected": bool(first), "active": held, "first_event": first,
            "events": events, "event_count": len(events), "lookback_sessions": int(lookback),
            "rvol_threshold": rvol_threshold, "close_location_min": close_location_min,
            "max_platform_range_pct": max_platform_range_pct,
            "latest_close_qfq": float(daily.close.iloc[-1]),
            "state": "holds_platform" if held else "failed_platform" if first else "not_detected",
            "basis": "检测当日量/此前20日均量，收盘突破此前20日高点且接近当日上沿，20日平台窄幅；仅用事件发生时已完成K线"}


def compression_observation(daily, threshold_pct=20.0):
    recent = daily.tail(126)
    running_peak = recent.high.cummax().shift(1)
    drawdowns = (1 - recent.low / running_peak) * 100
    maximum = float(drawdowns.max())
    return {"status": "ok", "experienced": maximum >= threshold_pct,
            "max_drawdown_pct": maximum, "threshold_pct": threshold_pct,
            "window_sessions": len(recent), "start": recent.index[0].date().isoformat(),
            "end": recent.index[-1].date().isoformat(), "basis": "近126个交易日从此前峰值到其后低点的价格压缩代理，前复权；不等同估值压缩"}


def extended_monthly_history(provider, code, daily, period_cutoff, anchor_date):
    """Pair genuine native monthly prices with all complete overlapping daily periods."""
    raw = provider.fetch("monthly_tx_qfq", ttl=86400, symbol=code,
                         end_date=period_cutoff.isoformat(), anchor_date=anchor_date.isoformat())
    monthly = bars(raw)
    monthly = monthly[monthly.index.normalize() <= pd.Timestamp(period_cutoff)]
    reference = aggregate(daily, "ME", period_cutoff)
    if len(reference) < 3 or len(monthly) < 24 or monthly.index[-1] != reference.index[-1]:
        raise DataError("独立长历史月线不足或未覆盖最新完整月份")
    # The first daily-aggregate month can be truncated by the daily source's cap.
    # Every subsequent overlapping month must share the same qfq OHLC prices.
    matched = reference.iloc[1:]
    if not matched.index.isin(monthly.index).all():
        raise DataError("独立月线缺少日线已覆盖的完整月份")
    paired = monthly.reindex(matched.index)
    for column in ["open", "close", "high", "low"]:
        if ((paired[column] - matched[column]).abs() > 0.011).any():
            raise DataError(f"独立月线{column}与日线前复权聚合价格不一致")
    monthly.attrs["price_validation"] = {
        "matched_complete_months": len(matched), "columns": ["open", "close", "high", "low"],
        "tolerance_cny": 0.011, "latest_complete_period": reference.index[-1].date().isoformat(),
        "anchor_date": anchor_date.isoformat(), "forming_period_excluded": True}
    return monthly


def strategic_technical(daily, session, period_cutoff=None, config=None, monthly_history=None):
    if len(daily) < 250 or daily.index[-1].date() != session:
        raise DataError("日线不足250根，或最新K线未覆盖已收盘交易日（可能停牌/数据滞后）")
    d = indicators(daily)
    # A holiday week/month can end after the last trading session. Use the
    # known calendar cutoff while still restricting actual bars to session.
    cutoff = period_cutoff or session
    w = indicators(aggregate(daily, "W-FRI", cutoff))
    m = indicators(monthly_history if monthly_history is not None else aggregate(daily, "ME", cutoff))
    if len(w) < 60 or len(m) < 24:
        raise DataError("周/月线不足（至少60周、24个完整月份）")
    last_d, last_w, last_m = d.iloc[-1], w.iloc[-1], m.iloc[-1]
    daily_ok = bool(last_d.close > last_d.ma60 and last_d.ma20 > d.ma20.iloc[-6])
    weekly_ok = bool(last_w.close > last_w.ma20 and last_w.ma20 >= w.ma20.iloc[-4])
    monthly_ok = bool(last_m.close > last_m.ma20)
    config = config or {}
    volume = daily_volume_observation(daily, config.get("daily_volume_abnormal_ratio", 2.0))
    weekly_base = weekly_base_observation(w, daily,
        config.get("weekly_base_max_range_pct", 20.0),
        config.get("weekly_base_low_tolerance_pct", 3.0),
        config.get("weekly_base_max_volume_ratio", 1.1))
    monthly_recovery = monthly_recovery_observation(m)
    weekly_structure = weekly_structure_observation(w, weekly_base,
        config.get("weekly_base_max_range_pct", 20.0))
    breakout = first_volume_breakout(daily, config.get("daily_breakout_rvol", 1.5),
        config.get("daily_breakout_close_location", 0.65), config.get("daily_breakout_lookback", 20),
        config.get("daily_breakout_platform_range_pct", 20.0))
    return {"daily_trend": daily_ok, "weekly_trend": weekly_ok, "monthly_trend": monthly_ok,
            "average_turnover20_cny": float(pd.to_numeric(daily.amount, errors="coerce").tail(20).mean())
                                      if "amount" in daily and daily.amount.tail(20).notna().all() else None,
            "monthly_j": float(last_m.j), "monthly_bar_date": m.index[-1].date().isoformat(),
            "monthly_history_source": "validated_native_qfqmonth" if monthly_history is not None else "daily_aggregate",
            "monthly_history_validation": monthly_history.attrs.get("price_validation") if monthly_history is not None else None,
            "weekly_bar_date": w.index[-1].date().isoformat(),
            "daily_close_qfq": float(last_d.close), "daily_ma60_qfq": float(last_d.ma60),
            "daily_bar_date": daily.index[-1].date().isoformat(),
            "daily_divergences": confirmed_divergences(daily, window=120, config=config),
            "weekly_base": weekly_base, "daily_volume": volume,
            "monthly_recovery": monthly_recovery, "weekly_structure": weekly_structure,
            "first_volume_breakout": breakout,
            "timeframes": {"monthly": timeframe_observation(m, config), "weekly": timeframe_observation(w, config),
                           "daily": timeframe_observation(d, config)},
            "price_compression": compression_observation(daily, config.get("leading_price_compression_pct", 20.0)),
            "volume_ratio20": volume["ratio20"]}


def confirmed_divergences(frame, window=60, radius=3, config=None):
    """Two confirmed price pivots; no future bars beyond current input.

    A pivot requires `radius` subsequent bars. MACD uses DIF; KDJ uses J.
    Divergence is a watch flag, never an order or a standalone buy point.
    """
    data = indicators(frame).iloc[-window:]
    if len(data) < 35:
        return {"status": "insufficient_bars", "signals": []}
    config = config or {}
    found, weak = [], []
    for direction, price_col in [("bullish", "low"), ("bearish", "high")]:
        points = []
        values = data[price_col].to_numpy()
        for i in range(radius, len(data) - radius):
            neighbors = np.concatenate([values[i - radius:i], values[i + 1:i + radius + 1]])
            if (direction == "bullish" and values[i] < neighbors.min()) or \
               (direction == "bearish" and values[i] > neighbors.max()):
                points.append(i)
        if len(points) < 2:
            continue
        previous, current = points[-2:]
        if len(data) - 1 - current > 12:
            continue
        a, b = data.iloc[previous], data.iloc[current]
        for label, oscillator in [("MACD_DIF", "dif"), ("KDJ_J", "j")]:
            ok = ((b[price_col] < a[price_col] and b[oscillator] > a[oscillator]) if direction == "bullish"
                  else (b[price_col] > a[price_col] and b[oscillator] < a[oscillator]))
            if ok:
                signal = {"direction": direction, "indicator": label,
                              "previous_pivot": data.index[previous].isoformat(),
                              "current_pivot": data.index[current].isoformat(),
                              "confirmed_at": data.index[current + radius].isoformat(),
                              "price_previous": float(a[price_col]), "price_current": float(b[price_col]),
                              "indicator_previous": float(a[oscillator]), "indicator_current": float(b[oscillator])}
                if label == "MACD_DIF":
                    atr = float(b.atr14)
                    price_move = abs(float(b[price_col] - a[price_col]))
                    dif_move = abs(float(b.dif - a.dif))
                    confirmation = data.iloc[current + radius]
                    sign = 1 if direction == "bullish" else -1
                    rebound = sign * float(confirmation.close - b.close)
                    conditions = {
                        "pivot_spacing": current - previous >= config.get("macd_min_pivot_spacing", 5),
                        "price_extension": price_move / float(a[price_col]) * 100 >= config.get("macd_min_price_extension_pct", .2),
                        "price_extension_atr": atr > 0 and price_move / atr >= config.get("macd_min_price_extension_atr", .15),
                        "dif_improvement_atr": atr > 0 and dif_move / atr >= config.get("macd_min_dif_improvement_atr", .1),
                        "same_zero_region": bool(a.dif < 0 and b.dif < 0) if direction == "bullish" else bool(a.dif > 0 and b.dif > 0),
                        "confirmation_rebound": atr > 0 and rebound / atr >= config.get("macd_min_confirmation_rebound_atr", .3),
                        "not_invalidated": bool(data.low.iloc[current + radius:].min() >= b.low) if direction == "bullish" else bool(data.high.iloc[current + radius:].max() <= b.high),
                    }
                    signal["strength"] = {"conditions": conditions, "pivot_spacing_bars": current - previous,
                        "price_extension_pct": price_move / float(a[price_col]) * 100,
                        "price_extension_atr": price_move / atr if atr > 0 else None,
                        "dif_improvement_atr": dif_move / atr if atr > 0 else None,
                        "confirmation_rebound_atr": rebound / atr if atr > 0 else None,
                        "atr14": atr}
                    signal["quality_passed"] = all(conditions.values())
                    if not signal["quality_passed"]:
                        weak.append(signal)
                        continue
                found.append(signal)
    return {"status": "ok", "signals": found, "weak_signals": weak,
            "macd_quality_version": "atr_pivot_confirmation_v1"}


def timeframe_observation(frame, config=None):
    """Auditable indicators and confirmed pivots from already complete bars."""
    calculated = indicators(frame)
    latest, previous = calculated.iloc[-1], calculated.iloc[-2]
    baseline = frame.volume.iloc[-21:-1]
    ratio = float(latest.volume / baseline.mean()) if len(baseline) == 20 and baseline.mean() > 0 else None
    divergence = confirmed_divergences(frame, window=120, config=config)
    structure = {"close_qfq": float(latest.close), "low_qfq": float(latest.low),
                 "high_qfq": float(latest.high), "previous_close_qfq": float(previous.close),
                 "previous_high_qfq": float(previous.high), "ma20_qfq": float(latest.ma20),
                 "ma20_rising": bool(latest.ma20 > calculated.ma20.iloc[-6]) if len(calculated) >= 25 else False,
                 "support_qfq": float(frame.low.tail(20).min()),
                 "resistance_qfq": float(frame.high.iloc[-21:-1].max()), "ratio20": ratio,
                 "golden_cross": bool((latest.dif > latest.dea and previous.dif <= previous.dea) or
                                      (latest.k > latest.d and previous.k <= previous.d)),
                 "overheated": bool(latest.j > 100 and latest.close > latest.ma20 * 1.03),
                 "pullback_rebound": bool(previous.close >= previous.ma20 and latest.low <= latest.ma20 and
                                          latest.close > latest.ma20 and latest.close > previous.close),
                 "price_reclaimed": bool(latest.close > previous.high or latest.close > latest.ma20)}
    return {"status": "ok", "signals": divergence["signals"], "weak_signals": divergence.get("weak_signals", []),
            "macd_quality_version": divergence.get("macd_quality_version"), "divergence_status": divergence["status"],
            "last_bar": frame.index[-1].isoformat(), "bar_count": len(frame),
            "above_ma20": bool(latest.close > latest.ma20), "structure": structure,
            "macd": {"dif": float(latest.dif), "dea": float(latest.dea), "atr14": float(latest.atr14),
                     "histogram": float(latest.macd), "previous_histogram": float(previous.macd)}}


def closed_minute_cutoff(calendar, now, period):
    """Exchange bar-end labels; lunch and overnight are not trading bars."""
    trading = sorted(pd.to_datetime(calendar.trade_date).dt.date)
    candidates = []
    for day in [d for d in trading if d <= now.date()][-2:]:
        for start_hour, start_minute in [(9, 30), (13, 0)]:
            start = datetime.combine(day, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
            for offset in range(period, 121, period):
                end = start + timedelta(minutes=offset)
                if end <= now.replace(tzinfo=None) - timedelta(seconds=30):
                    candidates.append(end)
    if not candidates:
        raise DataError("没有可用完整分时K线")
    return pd.Timestamp(max(candidates))


def minute_frame(provider, code, period, cutoff, daily_qfq):
    """Sina minute prices are adjusted with independently matched daily bars.

    AKShare's Sina minute helper can silently return raw prices when its daily
    adjustment fails, so this implementation does not rely on that fallback.
    """
    raw = provider.fetch("minute_sina_raw", ttl=60, symbol=code, period=str(period))
    data = bars(raw, "时间")
    data = data[data.index <= cutoff].tail(120)
    if data.empty or data.index[-1] != cutoff:
        raise DataError(f"分时最新完整K线不是 {cutoff}，可能停牌/数据滞后")
    # Ratios come from full raw vs adjusted daily closes, not a partial
    # intraday close. Today's latest adjusted prices have factor 1.
    last_complete_day = daily_qfq.index[-1].date()
    same_complete_session = last_complete_day == cutoff.date()
    current_close = same_complete_session and last_complete_day == pd.Timestamp.now(tz="Asia/Shanghai").date()
    raw_daily, _ = daily_history(provider, code, None,
                                last_complete_day.strftime("%Y%m%d"), cutoff.strftime("%Y%m%d"), adjust="",
                                required_session=last_complete_day if same_complete_session else None,
                                expected_close=float(daily_qfq.close.iloc[-1]) if current_close else None)
    raw_daily = bars(raw_daily)
    aligned = daily_qfq.close / raw_daily.close
    for day in set(data.index.date):
        if day > last_complete_day:
            factor = 1.0
        else:
            key = pd.Timestamp(day)
            if key not in aligned.index or not np.isfinite(aligned.loc[key]) or aligned.loc[key] <= 0:
                raise DataError("分时复权因子缺失，不跨除权跳空计算背离")
            factor = float(aligned.loc[key])
        data.loc[data.index.date == day, ["open", "high", "low", "close"]] *= factor
    return data


def minute_execution_status(observation):
    """Describe a completed 15m observation, without granting execution rights."""
    status = observation.get("status")
    if status != "ok":
        return {"unavailable": "数据缺失", "insufficient_bars": "K线不足，未能判断",
                "not_requested": "未请求"}.get(status, "未检查")
    directions = {s.get("direction") for s in observation.get("signals", [])}
    labels = [label for direction, label in [("bullish", "底背离"), ("bearish", "顶背离")]
              if direction in directions]
    structure = observation.get("structure", {})
    for key, label in [("overheated", "过热"), ("golden_cross", "金叉"), ("pullback_rebound", "回踩反弹")]:
        if structure.get(key):
            labels.append(label)
    return "／".join(labels) or "无信号"


def minute_observation(provider, code, cutoff, daily_qfq, period=15, config=None):
    try:
        data = minute_frame(provider, code, period, cutoff, daily_qfq)
        minimum = 35 if period == 15 else 60
        if len(data) < minimum:
            return {"status": "insufficient_bars", "signals": [], "last_bar": data.index[-1].isoformat(),
                    "purpose": f"{period}分钟K线不足{minimum}根，不解释为无背离"}
        observation = {**timeframe_observation(data, config), "purpose": "观察与评分；不改变入池资格，不作为新开仓理由"}
        observation["execution_status"] = minute_execution_status(observation)
        return observation
    except DataError as exc:
        return {"status": "unavailable", "signals": [], "error": str(exc)}


def execution_observation(provider, code, trend_confirmed, now, calendar, daily_qfq, minute15, config=None, minute_frames=None):
    """Execution study for qualified securities, before any holding eligibility."""
    config = config or {}
    if not trend_confirmed:
        return {"status": "strategy_not_passed", "entry_watch": False,
                "message": "基本面/估值/月J路径/周日量价及原日周月均线尚未全部确认"}
    if minute15.get("status") != "ok":
        return {"status": "unavailable", "entry_watch": False, "error": minute15.get("error", "15分钟K线不足/未请求")}
    signals = minute15.get("signals", [])
    directions = {s["direction"] for s in signals}
    structure = minute15.get("structure", {})
    label = minute_execution_status(minute15)
    result = {"status": "no_signal", "entry_watch": False, "execution_status": label,
              "signals": signals, "frames": {"15": minute15}, "reduce_watch": "bearish" in directions,
              "message": "执行观察不构成新开仓/下单授权；持仓资格单独核对"}
    if not signals:
        return result
    for period in [60, 30]:
        if minute_frames is not None and str(period) in minute_frames:
            result["frames"][str(period)] = minute_frames[str(period)]
            continue
        try:
            cutoff = closed_minute_cutoff(calendar, now, period)
            data = minute_frame(provider, code, period, cutoff, daily_qfq)
            if len(data) < 60:
                raise DataError(f"{period}分钟K线不足60根")
            calculated = indicators(data)
            result["frames"][str(period)] = {**confirmed_divergences(data, window=120, config=config),
                "last_bar": data.index[-1].isoformat(),
                "above_ma20": bool(calculated.close.iloc[-1] > calculated.ma20.iloc[-1]),
                "support_qfq": float(data.low.tail(20).min()), "resistance_qfq": float(data.high.tail(20).max())}
        except DataError as exc:
            result["frames"][str(period)] = {"status": "unavailable", "error": str(exc)}
    bottoms = [s for s in signals if s["direction"] == "bullish" and s.get("indicator") == "MACD_DIF" and s.get("quality_passed") is True]
    support = min((s["price_current"] for s in bottoms), default=structure.get("support_qfq"))
    low, close = structure.get("low_qfq"), structure.get("close_qfq")
    near = support is not None and support > 0 and low is not None and 0 <= (low / support - 1) * 100 <= config.get("execution_support_distance_pct", 3.0)
    rebound = close is not None and close > structure.get("previous_close_qfq", close)
    frame60, frame30 = result["frames"]["60"], result["frames"]["30"]
    fresh60 = frame60.get("last_bar", "").startswith(now.date().isoformat())
    no30top = frame30.get("status") == "ok" and not any(s["direction"] == "bearish" for s in frame30.get("signals", []))
    conditions = {"bottom_divergence": bool(bottoms), "no_15m_top_conflict": "bearish" not in directions,
                  "not_overheated": not structure.get("overheated", False),
                  "near_confirmed_support_and_rebound": bool(near and rebound),
                  "price_structure_reclaimed": bool(structure.get("price_reclaimed")),
                  "volume_confirmation": structure.get("ratio20") is not None and structure["ratio20"] >= config.get("execution_volume_ratio", 1.2),
                  "same_session_60m_structure": frame60.get("status") == "ok" and bool(frame60.get("above_ma20")) and fresh60,
                  "30m_without_top_conflict": no30top}
    result.update(conditions=conditions, support_qfq=support, entry_watch=all(conditions.values()),
                  status="confirmed_execution_watch" if all(conditions.values()) else "wait_for_confirmation")
    return result


def tactical(provider, code, eligible, held, session, now=None, calendar=None, daily_qfq=None, minute15=None, execution=None):
    if not held:
        return {"status": "not_held", "signals": [], "message": "非持仓，不计算小周期交易提示"}
    if not eligible:
        return {"status": "strategy_not_passed", "signals": [], "message": "未通过本次基本面/估值/大周期，不生成战术提示"}
    if execution is not None:
        return {"status": "watch_only", "confirmed_holding": True,
                "entry_eligible": bool(execution.get("entry_watch")), "reduce_watch": bool(execution.get("reduce_watch")),
                "frames": execution.get("frames", {}), "conditions": execution.get("conditions", {}),
                "message": "仅已确认持仓的局部做T观察；A股当日新买股份不可当日卖出，无下单授权"}
    output = {"status": "watch_only", "message": "仍需支撑阻力、量价与价格结构人工确认；A股当日新买股份不可当日卖出", "frames": {}}
    for period in ["60", "30", "15"]:
        try:
            if period == "15" and minute15 is not None:
                output["frames"][period] = minute15
                continue
            if now is None or calendar is None or daily_qfq is None:
                raise DataError("缺少已确认分时截点或复权日线")
            cutoff = closed_minute_cutoff(calendar, now, int(period))
            data = minute_frame(provider, code, int(period), cutoff, daily_qfq)
            if period == "60":
                calculated = indicators(data)
                if len(data) < 60:
                    raise DataError("60分钟K线不足60根")
                recent = data.iloc[-20:]
                output["frames"][period] = {"status": "ok", "last_bar": data.index[-1].isoformat(),
                    "above_ma20": bool(calculated.close.iloc[-1] > calculated.ma20.iloc[-1]),
                    "support_qfq": float(recent.low.min()), "resistance_qfq": float(recent.high.max())}
            else:
                output["frames"][period] = {**confirmed_divergences(data), "last_bar": data.index[-1].isoformat()}
        except DataError as exc:
            output["frames"][period] = {"status": "unavailable", "error": str(exc)}
    return output
