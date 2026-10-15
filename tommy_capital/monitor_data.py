"""Strict completed-bar acquisition for a small, explicit monitored union."""
from datetime import datetime, timedelta

import pandas as pd

from .data import DataError, completed_session, daily_history
from .technical import bars, closed_minute_cutoff, minute_frame


def aggregate_120(frame, calendar, cutoff):
    """Require exactly two real 60m bars within each A-share half-session."""
    trading = set(pd.to_datetime(calendar.trade_date).dt.date)
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise DataError("60分钟源重复或乱序")
    output, gaps = [], []
    grouped = {}
    for timestamp, row in frame.iterrows():
        if timestamp.date() not in trading:
            raise DataError("60分钟含非交易日记录")
        clock = timestamp.strftime("%H:%M")
        if clock not in ("10:30", "11:30", "14:00", "15:00"):
            raise DataError("60分钟时间戳不符合A股半场结束标签")
        end = timestamp.normalize() + pd.Timedelta(hours=11, minutes=30) if clock in ("10:30", "11:30") else timestamp.normalize()+pd.Timedelta(hours=15)
        grouped.setdefault(end, []).append((timestamp, row))
    for end, values in sorted(grouped.items()):
        if end > cutoff:
            continue
        expected = ([end-pd.Timedelta(hours=1), end] if end.hour==11 else [end-pd.Timedelta(hours=1), end])
        if [t for t,_ in values] != expected:
            gaps.append(end.isoformat()); continue
        first, second = values[0][1], values[1][1]
        output.append({"date": end, "open": first.open, "close": second.close,
                       "high": max(first.high,second.high), "low": min(first.low,second.low),
                       "volume": first.volume+second.volume})
    if gaps:
        # A source-capped initial half-session can be discarded, internal gaps cannot.
        internal = [g for g in gaps if g != min(grouped).isoformat()]
        if internal:
            raise DataError("120分钟半场缺少完整60分钟组成：" + ",".join(internal))
    result = pd.DataFrame(output).set_index("date") if output else frame.iloc[:0].copy()
    if result.empty or result.index[-1] != cutoff:
        raise DataError("120分钟最新完整半场缺失，无法判断")
    return result


def acquire_frames(provider, code, now, calendar, daily_reuse=None):
    session = completed_session(calendar, now)
    if daily_reuse is None:
        raw,_ = daily_history(provider,code,f"{session.year-8}0101",session.strftime("%Y%m%d"),
                              now.strftime("%Y%m%d"),required_session=session)
        daily = bars(raw)
    else:
        daily = daily_reuse.copy()
    daily = daily[daily.index.date <= session]
    if daily.empty or daily.index[-1].date()!=session:
        raise DataError("监控日线未覆盖最新完整交易日")
    if len(daily)<250:
        raise DataError("监控日线历史不足250根，保留原数据长度要求")
    result = {"daily": {"status": "ok", "frame": daily, "source": "validated_qfq_daily", "cutoff": daily.index[-1].isoformat()}}
    for period in (60,30,15):
        try:
            cutoff = closed_minute_cutoff(calendar,now,period)
            frame = minute_frame(provider,code,period,cutoff,daily)
            # Reject off-session, internal missing, or out-of-calendar minute bars.
            days = set(pd.to_datetime(calendar.trade_date).dt.date)
            allowed = {f"{hour:02}:{minute:02}" for hour,minute in []}
            for start in (pd.Timestamp("2000-01-01 09:30"),pd.Timestamp("2000-01-01 13:00")):
                allowed.update((start+pd.Timedelta(minutes=k)).strftime("%H:%M") for k in range(period,121,period))
            if any(t.date() not in days or t.strftime("%H:%M") not in allowed for t in frame.index):
                raise DataError("分钟数据含非交易日或非标准时段标签")
            expected=[]
            for day in sorted(days):
                if not frame.index[0].date() <= day <= cutoff.date():
                    continue
                for start in (datetime.combine(day,datetime.min.time()).replace(hour=9,minute=30),
                              datetime.combine(day,datetime.min.time()).replace(hour=13)):
                    expected.extend(pd.Timestamp(start+timedelta(minutes=k)) for k in range(period,121,period)
                                    if frame.index[0] <= pd.Timestamp(start+timedelta(minutes=k)) <= cutoff)
            if list(frame.index)!=expected:
                raise DataError("分钟历史内部缺根/交易时段不连续，无法可靠判断")
            result[str(period)]={"status":"ok","frame":frame,"source":"sina_raw + matched_daily_qfq_factors","cutoff":cutoff.isoformat()}
        except DataError as exc:
            result[str(period)]={"status":"unavailable","error":str(exc)}
    try:
        if result["60"]["status"]!="ok":
            raise DataError("60分钟缺失，不能合成120分钟")
        cutoff=closed_minute_cutoff(calendar,now,120)
        frame=aggregate_120(result["60"]["frame"],calendar,cutoff)
        result["120"]={"status":"ok","frame":frame,"source":"two_complete_60m_bars_per_half_session","cutoff":cutoff.isoformat()}
    except DataError as exc:
        result["120"]={"status":"unavailable","error":str(exc)}
    return result
