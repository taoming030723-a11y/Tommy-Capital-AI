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
    params = {"reportName": "RPT_LICO_FN_CPD", "columns": ",".join([*FIN_FIELDS, "REPORTDATE", "SECUCODE", "UPDATE_DATE"]), "pageSize": 500,
              "sortColumns": "SECURITY_CODE,UPDATE_DATE", "sortTypes": "1,-1",
              "filter": f"(REPORTDATE='{period}')"}
    first = get_json(url, {**params, "pageNumber": 1})
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
        item = get_json(url, {**params, "pageNumber": number})["result"]
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
               "pb": "市净率", "per": "市盈率-动态", "mktcap": "总市值"}
    if not set(mapping).issubset(raw.columns):
        raise ValueError("新浪行情字段变化")
    frame = raw.rename(columns=mapping)[list(mapping.values())].copy()
    # Sina uses CNY 10,000 for mktcap; amount is already CNY.
    frame["总市值"] = pd.to_numeric(frame["总市值"], errors="coerce") * 10000
    frame["行情来源"] = "sina_public"
    return frame


def daily_tx(symbol, end_date, adjust="qfq", start_date=None, anchor_date=None, **unused):
    market = "sh" if symbol.startswith("6") else "bj" if symbol.startswith(("4", "8", "92")) else "sz"
    symbol = symbol if symbol.startswith(("sh", "sz", "bj")) else market + symbol
    endpoint = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    anchor = pd.Timestamp(anchor_date or end_date).strftime('%Y-%m-%d')
    data = get_json(endpoint, {"param": f"{symbol},day,,{anchor},640,{adjust}"})
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
