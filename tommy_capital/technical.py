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


def strategic_technical(daily, session, period_cutoff=None, config=None):
    if len(daily) < 250 or daily.index[-1].date() != session:
        raise DataError("日线不足250根，或最新K线未覆盖已收盘交易日（可能停牌/数据滞后）")
    d = indicators(daily)
    # A holiday week/month can end after the last trading session. Use the
    # known calendar cutoff while still restricting actual bars to session.
    cutoff = period_cutoff or session
    w = indicators(aggregate(daily, "W-FRI", cutoff))
    m = indicators(aggregate(daily, "ME", cutoff))
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
    return {"daily_trend": daily_ok, "weekly_trend": weekly_ok, "monthly_trend": monthly_ok,
            "average_turnover20_cny": float(pd.to_numeric(daily.amount, errors="coerce").tail(20).mean())
                                      if "amount" in daily and daily.amount.tail(20).notna().all() else None,
            "monthly_j": float(last_m.j), "monthly_bar_date": m.index[-1].date().isoformat(),
            "weekly_bar_date": w.index[-1].date().isoformat(),
            "daily_close_qfq": float(last_d.close), "daily_ma60_qfq": float(last_d.ma60),
            "daily_bar_date": daily.index[-1].date().isoformat(),
            "daily_divergences": confirmed_divergences(daily, window=120),
            "weekly_base": weekly_base, "daily_volume": volume,
            "volume_ratio20": volume["ratio20"]}


def confirmed_divergences(frame, window=60, radius=3):
    """Two confirmed price pivots; no future bars beyond current input.

    A pivot requires `radius` subsequent bars. MACD uses DIF; KDJ uses J.
    Divergence is a watch flag, never an order or a standalone buy point.
    """
    data = indicators(frame).iloc[-window:]
    if len(data) < 35:
        return {"status": "insufficient_bars", "signals": []}
    found = []
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
                found.append({"direction": direction, "indicator": label,
                              "previous_pivot": data.index[previous].isoformat(),
                              "current_pivot": data.index[current].isoformat(),
                              "confirmed_at": data.index[current + radius].isoformat(),
                              "price_previous": float(a[price_col]), "price_current": float(b[price_col]),
                              "indicator_previous": float(a[oscillator]), "indicator_current": float(b[oscillator])})
    return {"status": "ok", "signals": found}


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
    raw_daily, _ = daily_history(provider, code, None,
                                last_complete_day.strftime("%Y%m%d"), cutoff.strftime("%Y%m%d"), adjust="")
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


def minute_observation(provider, code, cutoff, daily_qfq):
    try:
        data = minute_frame(provider, code, 15, cutoff, daily_qfq)
        return {**confirmed_divergences(data, window=120), "last_bar": data.index[-1].isoformat(),
                "purpose": "观察标记；不改变战略资格，不作为新开仓理由"}
    except DataError as exc:
        return {"status": "unavailable", "signals": [], "error": str(exc)}


def tactical(provider, code, eligible, held, session, now=None, calendar=None, daily_qfq=None, minute15=None):
    if not held:
        return {"status": "not_held", "signals": [], "message": "非持仓，不计算小周期交易提示"}
    if not eligible:
        return {"status": "strategy_not_passed", "signals": [], "message": "未通过本次基本面/估值/大周期，不生成战术提示"}
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
