"""Small bounded clients for the public endpoints used by AKShare.

Named financial fields avoid the upstream positional-column assumption.
These functions only return received market records, never synthetic data.
"""
import json
import hashlib
import math
import re
import threading
import time
import os
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests


_sessions = threading.local()


def request(url, params):
    if not hasattr(_sessions, "client"):
        _sessions.client = requests.Session()
    for attempt in range(2):
        try:
            response = _sessions.client.get(url, params=params, timeout=(8, 20))
            response.raise_for_status()
            return response
        except requests.RequestException:
            if attempt:
                raise
            time.sleep(1)


def get_json(url, params):
    return request(url, params).json()


def financial_page(url, params):
    """Retain successful pages briefly so a retry repairs only missing pages."""
    key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
    root = Path(os.environ.get("TOMMY_PAGE_CACHE", ".cache/tommy/finance-pages"))
    root.mkdir(parents=True, exist_ok=True)
    path = root / (key + ".json")
    if path.exists():
        try:
            cached = json.loads(path.read_text())
            if 0 <= time.time() - cached["fetched_epoch"] < 600:
                return cached["data"]
        except (ValueError, KeyError):
            pass
    item = get_json(url, params)
    if item.get("code") == 0 and item.get("result") is not None:
        temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps({"fetched_epoch": time.time(), "data": item}), encoding="utf-8")
        temporary.replace(path)
    return item


FIN_FIELDS = {
    "SECURITY_CODE": "股票代码", "SECURITY_NAME_ABBR": "股票简称", "BASIC_EPS": "每股收益",
    "TOTAL_OPERATE_INCOME": "营业总收入-营业总收入", "YSTZ": "营业总收入-同比增长",
    "PARENT_NETPROFIT": "净利润-净利润", "SJLTZ": "净利润-同比增长", "WEIGHTAVG_ROE": "净资产收益率",
    "MGJYXJJE": "每股经营现金流量", "XSMLL": "销售毛利率", "PUBLISHNAME": "所处行业",
    "NOTICE_DATE": "最新公告日期",
}


def finance(date):
    period = pd.Timestamp(date).strftime("%Y-%m-%d")
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {"reportName": "RPT_LICO_FN_CPD", "columns": "ALL", "pageSize": 500,
              "sortColumns": "UPDATE_DATE,SECURITY_CODE", "sortTypes": "-1,-1", "source": "WEB", "client": "WEB",
              "filter": f"(REPORTDATE='{period}')"}
    first = financial_page(url, {**params, "pageNumber": 1})
    result = first.get("result")
    if result is None:
        # This API explicitly distinguishes a valid query with no records.
        if first.get("code") == 9201:
            return pd.DataFrame(columns=FIN_FIELDS.values())
        raise ValueError(f"财报API未返回结果：{first.get('code')} {first.get('message')}")
    pages = int(result["pages"])
    if not 1 <= pages <= 100:
        raise ValueError("财报分页数量异常")
    rows = list(result["data"])
    seen = {hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}
    def page(number):
        item = financial_page(url, {**params, "pageNumber": number})["result"]
        if int(item["pages"]) != pages:
            raise ValueError("财报数据在分页中变化，请重试")
        return item["data"]
    with ThreadPoolExecutor(max_workers=4) as executor:
        for items in executor.map(page, range(2, pages + 1)):
            digest = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
            if not items or digest in seen:
                raise ValueError("财报分页为空或重复，不能声称全市场覆盖")
            seen.add(digest)
            rows.extend(items)
    if result.get("count") is not None and len(rows) != int(result["count"]):
        raise ValueError("财报分页总行数与API声明不符")
    if not rows:
        raise ValueError("非空财报分页返回空数据")
    raw = pd.DataFrame(rows)
    if not set(FIN_FIELDS).issubset(raw.columns) or "REPORTDATE" not in raw:
        raise ValueError("财报字段变化")
    actual = pd.to_datetime(raw.REPORTDATE).dt.strftime("%Y-%m-%d")
    if (actual != period).any():
        raise ValueError("财报API忽略了报告期过滤，不使用错期数据")
    if "SECUCODE" in raw:
        raw = raw[raw.SECUCODE.astype(str).str.endswith((".SH", ".SZ", ".BJ"))]
    return raw.rename(columns=FIN_FIELDS)[list(FIN_FIELDS.values())]


def spot_sina():
    base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center."
    response = request(base + "getHQNodeStockCount", {"node": "hs_a"})
    count = int(re.search(r"\d+", response.text).group())
    if not 4000 <= count <= 10000:
        raise ValueError("全市场证券数量异常")
    params = {"num": 80, "sort": "symbol", "asc": 1, "node": "hs_a", "symbol": ""}
    def page(number):
        records = get_json(base + "getHQNodeData", {**params, "page": number})
        if not isinstance(records, list):
            raise ValueError("新浪行情分页异常")
        return records
    rows = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        for chunk in executor.map(page, range(1, math.ceil(count / 80) + 1)):
            rows.extend(chunk)
    raw = pd.DataFrame(rows).drop_duplicates("code")
    if len(raw) != count:
        raise ValueError(f"行情覆盖不全：{len(raw)}/{count}，请重试")
    mapping = {"code": "代码", "name": "名称", "trade": "最新价", "amount": "成交额",
               "pb": "市净率", "per": "市盈率-动态", "mktcap": "总市值", "ticktime": "行情时刻"}
    if not set(mapping).issubset(raw.columns):
        raise ValueError("新浪行情字段变化")
    frame = raw.rename(columns=mapping)[list(mapping.values())].copy()
    # Sina uses CNY 10,000 for mktcap; amount is already CNY.
    frame["总市值"] = pd.to_numeric(frame["总市值"], errors="coerce") * 10000
    frame["行情来源"] = "sina_public"
    return frame


def daily_tx(symbol, end_date, adjust="qfq", start_date=None, anchor_date=None, include_current_session=False, **unused):
    market = "sh" if symbol.startswith("6") else "bj" if symbol.startswith(("4", "8", "92")) else "sz"
    symbol = symbol if symbol.startswith(("sh", "sz", "bj")) else market + symbol
    endpoint = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    anchor = pd.Timestamp(anchor_date or end_date).strftime('%Y-%m-%d')
    # A dated historical request omits the provider's appended current session.
    # Use its latest-session response only for today's closing scan, then still
    # clip received bars to end_date. No unfinished/future bar is synthesized.
    if include_current_session and anchor != pd.Timestamp.now(tz="Asia/Shanghai").strftime('%Y-%m-%d'):
        raise ValueError("最新日线请求只能用于当前扫描日期，不能用于历史复权回放")
    request_end = "" if include_current_session else anchor
    data = get_json(endpoint, {"param": f"{symbol},day,,{request_end},640,{adjust}"})
    body = data["data"][symbol]
    # Never quietly use raw bars when adjusted bars were requested.
    key = "qfqday" if adjust == "qfq" else "hfqday" if adjust == "hfq" else "day"
    records = body.get(key)
    if not records:
        raise ValueError(f"腾讯未提供所请求的{key}价格")
    rows = [{"日期": r[0], "开盘": r[1], "收盘": r[2], "最高": r[3], "最低": r[4], "成交量": r[5],
             "成交额": float(r[8]) * 10000 if len(r) > 8 and r[8] not in (None, "") else float("nan")}
            for r in records]
    frame = pd.DataFrame(rows)
    return frame[pd.to_datetime(frame["日期"]) <= pd.Timestamp(end_date)]


def daily_sina(symbol, end_date, adjust="qfq", start_date=None, anchor_date=None):
    """Decode received daily bars and apply the source's dated qfq factors.

    Unlike the upstream helper, this does not merge/forward-fill daily prices
    with share-count or factor event dates. Only actual received bars survive.
    Missing factors never imply an adjustment factor of one.
    """
    if adjust not in ("", "qfq"):
        raise ValueError("新浪备用日线仅支持原始或前复权价格")
    market = "sh" if symbol.startswith("6") else "bj" if symbol.startswith(("4", "8", "92")) else "sz"
    symbol = symbol if symbol.startswith(("sh", "sz", "bj")) else market + symbol
    base = f"https://finance.sina.com.cn/realstock/company/{symbol}/"
    response = request(base + "hisdata_klc2/klc_kl.js", {})
    encoded = re.search(r'=\s*"([^"\n]+)"\s*;', response.text)
    if not encoded:
        raise ValueError("新浪日线压缩数据不可用")
    # AKShare supplies the decoder for Sina's documented compressed format.
    from akshare.stock.cons import hk_js_decode
    from py_mini_racer import py_mini_racer
    decoder = py_mini_racer.MiniRacer()
    decoder.eval(hk_js_decode)
    raw = pd.DataFrame(decoder.call("d", encoded.group(1)))
    required = {"date", "open", "close", "high", "low", "volume", "amount"}
    if not required.issubset(raw.columns) or raw.empty:
        raise ValueError("新浪日线缺少真实价格或成交额字段")
    raw = raw[list(required)].copy()
    # The decoder serializes JS dates as UTC strings; these are exchange
    # session labels, matched to the timezone-free factor event dates.
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    if raw["date"].isna().any() or raw["date"].duplicated().any():
        raise ValueError("新浪日线日期缺失或重复")
    raw = raw.sort_values("date")
    for column in required - {"date"}:
        raw[column] = pd.to_numeric(raw[column], errors="coerce")
    if adjust == "qfq":
        response = request(base + "qfq.js", {})
        match = re.search(r'=\s*(\{.*\})\s*;?\s*$', response.text, re.DOTALL)
        if not match:
            raise ValueError("新浪未提供可验证的前复权因子")
        factors = json.loads(match.group(1)).get("data")
        if not isinstance(factors, list) or not factors or any(not isinstance(r, list) or len(r) != 2 for r in factors):
            raise ValueError("新浪前复权因子为空或格式变化")
        factors = pd.DataFrame(factors, columns=["date", "factor"])
        factors["date"] = pd.to_datetime(factors["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
        factors["factor"] = pd.to_numeric(factors["factor"], errors="coerce")
        anchor = pd.Timestamp(anchor_date or end_date).normalize()
        if factors.isna().any().any() or factors["date"].duplicated().any() or (factors["date"] > anchor).any():
            raise ValueError("新浪前复权因子日期缺失、重复或超出扫描日期")
        if not factors["factor"].map(math.isfinite).all() or (factors["factor"] <= 0).any():
            raise ValueError("新浪前复权因子不是有限正数")
        raw = pd.merge_asof(raw, factors.sort_values("date"), on="date", direction="backward")
        if raw["factor"].isna().any():
            raise ValueError("新浪前复权因子未覆盖实际日线，不能按未复权价格计算")
        raw[["open", "close", "high", "low"]] = raw[["open", "close", "high", "low"]].div(raw["factor"], axis=0)
    raw = raw[raw["date"] <= pd.Timestamp(end_date)]
    if start_date:
        raw = raw[raw["date"] >= pd.Timestamp(start_date)]
    return raw.rename(columns={"date": "日期", "open": "开盘", "close": "收盘", "high": "最高",
                               "low": "最低", "volume": "成交量", "amount": "成交额"})[
        ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]]


def minute_sina(symbol, period="15"):
    market = "sh" if symbol.startswith("6") else "bj" if symbol.startswith(("4", "8", "92")) else "sz"
    symbol = symbol if symbol.startswith(("sh", "sz", "bj")) else market + symbol
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData"
    response = request(url, {"symbol": symbol, "scale": period, "ma": "no", "datalen": 240})
    match = re.search(r"=\((\[.*?\])\);?", response.text, re.DOTALL)
    if not match:
        raise ValueError("新浪分时格式变化")
    raw = pd.DataFrame(json.loads(match.group(1)))
    mapping = {"day": "时间", "open": "开盘", "close": "收盘", "high": "最高", "low": "最低", "volume": "成交量"}
    if not set(mapping).issubset(raw.columns):
        raise ValueError("新浪分时字段变化")
    return raw.rename(columns=mapping)[list(mapping.values())]
