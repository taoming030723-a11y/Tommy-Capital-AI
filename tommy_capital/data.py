import hashlib
import io
import json
import logging
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

SHANGHAI = ZoneInfo("Asia/Shanghai")
LOG = logging.getLogger(__name__)


class DataError(RuntimeError):
    pass


class Provider:
    """AKShare/Eastmoney. Never substitute demo data or expired cache."""
    def __init__(self, cache=".cache/tommy", timeout=90, retries=2, refresh=False):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.timeout, self.retries, self.refresh = timeout, retries, refresh
        self.lineage = []

    def fetch(self, function, ttl=300, allow_empty=False, **kwargs):
        request = {"function": function, "kwargs": kwargs}
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        path = self.cache / f"{key}.json"
        cache_hit = False
        payload = None
        if path.exists() and not self.refresh:
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
                age = time.time() - candidate["fetched_epoch"]
                if 0 <= age < ttl:
                    payload, cache_hit = candidate, True
            except (ValueError, KeyError):
                pass
        if payload is None:
            last_error = ""
            for attempt in range(self.retries):
                LOG.info("抓取 %s %s (%s/%s)", function, kwargs, attempt + 1, self.retries)
                try:
                    proc = subprocess.run(
                        [sys.executable, "-m", "tommy_capital.worker"],
                        input=json.dumps(request), text=True, capture_output=True,
                        timeout=self.timeout, check=True,
                    )
                    frame = pd.read_json(io.StringIO(proc.stdout), orient="split", dtype=False)
                    if frame.empty and not allow_empty:
                        raise DataError("接口返回空表")
                    payload = {"fetched_epoch": time.time(), "data": proc.stdout}
                    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
                    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(path)
                    break
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
                        ValueError, DataError) as exc:
                    last_error = (exc.stderr[-600:] if isinstance(exc, subprocess.CalledProcessError)
                                  else str(exc))
                    if attempt + 1 < self.retries:
                        time.sleep(1 + attempt)
            if payload is None:
                raise DataError(f"{function} 失败（未使用模拟数据）：{last_error}")
        frame = pd.read_json(io.StringIO(payload["data"]), orient="split", dtype=False)
        if frame.empty and not allow_empty:
            raise DataError(f"{function} 缓存为空")
        self.lineage.append({**request, "rows": len(frame), "cache_hit": cache_hit,
                             "fetched_at": datetime.fromtimestamp(payload["fetched_epoch"], SHANGHAI).isoformat(),
                             "sha256": hashlib.sha256(payload["data"].encode()).hexdigest()})
        return frame


def require(frame, columns, name):
    missing = set(columns) - set(frame.columns)
    if missing:
        raise DataError(f"{name} 字段变化，缺少：{sorted(missing)}")


def codes(series):
    return series.astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)


def completed_session(calendar, now):
    require(calendar, ["trade_date"], "交易日历")
    dates = pd.to_datetime(calendar.trade_date, errors="coerce").dropna().dt.date
    # A stale calendar must not make an old price appear current.
    if dates.empty or max(dates).year < now.year:
        raise DataError("交易日历未覆盖当前年份，请更新 AKShare")
    cutoff = now.date() if (now.hour, now.minute) >= (15, 10) else now.date() - timedelta(days=1)
    eligible = dates[dates <= cutoff]
    if eligible.empty:
        raise DataError("无法确定已收盘交易日")
    return max(eligible)


def report_periods(today, count=8):
    all_periods = [pd.Timestamp(year, month, day).date()
                   for year in range(today.year - 3, today.year + 1)
                   for month, day in [(3, 31), (6, 30), (9, 30), (12, 31)]]
    return sorted((d for d in all_periods if d < today), reverse=True)[:count]


FINANCE_COLUMNS = {
    "股票代码": "code", "股票简称": "name", "每股收益": "eps_ytd",
    "营业总收入-营业总收入": "revenue_ytd", "营业总收入-同比增长": "revenue_yoy",
    "净利润-净利润": "profit_ytd", "净利润-同比增长": "profit_yoy",
    "净资产收益率": "roe_ytd", "每股经营现金流量": "cfo_per_share_ytd",
    "销售毛利率": "gross_margin", "所处行业": "industry", "最新公告日期": "announced_at",
}


def normalize_finance(frame, period, today):
    require(frame, FINANCE_COLUMNS, "业绩报表")
    result = frame.rename(columns=FINANCE_COLUMNS)[list(FINANCE_COLUMNS.values())].copy()
    result.code = codes(result.code)
    result["period"] = pd.Timestamp(period)
    result.announced_at = pd.to_datetime(result.announced_at, errors="coerce")
    for col in set(result.columns) - {"code", "name", "industry", "period", "announced_at"}:
        result[col] = pd.to_numeric(result[col], errors="coerce")
    return result[(result.announced_at.dt.date <= today) &
                  (result.announced_at >= result.period)].sort_values("announced_at").drop_duplicates("code", keep="last")


def daily_history(provider, code, start, end, asof_day=None):
    """Both providers return actual market data; record any source switch."""
    try:
        return provider.fetch("daily_tx_recent", ttl=86400, symbol=code, adjust="qfq",
                              start_date=start, end_date=end, anchor_date=asof_day or end), None
    except DataError as primary:
        LOG.warning("%s 腾讯近期日线失败，尝试东财日线", code)
        try:
            frame = provider.fetch("stock_zh_a_hist", ttl=86400, symbol=code, period="daily", adjust="qfq",
                                   start_date=start, end_date=end, timeout=30)
        except DataError as backup:
            raise DataError(f"腾讯与东财日线均不可用；腾讯：{primary}；东财：{backup}") from backup
        return frame, f"{code}: 腾讯近期日线失败后切换东财；{primary}"


def latest_finance(history):
    """TTM profit = prior full year + current YTD - prior same YTD.

    EPS is informational: changes in share count can distort the EPS bridge.
    Valuation uses total market cap / TTM net profit instead.
    """
    history = history.sort_values(["code", "period"])
    output = []
    for code, group in history.groupby("code", sort=False):
        indexed = group.set_index("period")
        row = group.iloc[-1].to_dict()
        period = row["period"]
        previous_period = period - pd.offsets.QuarterEnd()
        row["previous_profit_yoy"] = (float(indexed.loc[previous_period, "profit_yoy"])
                                      if previous_period in indexed.index else float("nan"))
        row["fundamental_improving"] = bool(
            pd.notna(row["profit_yoy"]) and pd.notna(row["previous_profit_yoy"]) and
            row["profit_yoy"] > 0 and row["profit_yoy"] > row["previous_profit_yoy"])
        row["profit_ttm"] = float("nan")
        row["eps_ttm_approx"] = float("nan")
        if period.month == 12:
            row["profit_ttm"] = row["profit_ytd"]
            row["eps_ttm_approx"] = row["eps_ytd"]
        else:
            annual = pd.Timestamp(period.year - 1, 12, 31)
            same = pd.Timestamp(period.year - 1, period.month, period.day)
            if annual in indexed.index and same in indexed.index:
                for source, dest in [("profit_ytd", "profit_ttm"), ("eps_ytd", "eps_ttm_approx")]:
                    row[dest] = indexed.loc[annual, source] + row[source] - indexed.loc[same, source]
        output.append(row)
    return pd.DataFrame(output)


SPOT_COLUMNS = {"代码": "code", "名称": "name", "最新价": "price", "总市值": "market_cap",
                "成交额": "turnover", "市净率": "pb", "市盈率-动态": "pe_dynamic"}


def normalize_spot(frame):
    require(frame, SPOT_COLUMNS, "A股行情")
    result = frame.rename(columns=SPOT_COLUMNS)[list(SPOT_COLUMNS.values())].copy()
    result["quote_clock_time"] = frame["行情时刻"] if "行情时刻" in frame else None
    result.code = codes(result.code)
    # Include Shanghai, Shenzhen and Beijing A shares; exclude B shares.
    result = result[result.code.str.match(r"^(60\d|68\d|00\d|30\d|43\d|83\d|87\d|88\d|92\d)\d{3}$")]
    for col in ["price", "market_cap", "turnover", "pb", "pe_dynamic"]:
        result[col] = pd.to_numeric(result[col], errors="coerce")
    return result.drop_duplicates("code")
