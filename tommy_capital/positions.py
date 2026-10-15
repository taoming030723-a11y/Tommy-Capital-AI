"""Local-only current positions with legacy holdings.json compatibility."""
import contextlib
import json
import math
import os
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path


def code_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value.strip()):
        raise ValueError("股票代码须为六位数字字符串，保留前导0")
    return value.strip()


def validate_position(row):
    if isinstance(row, str):
        row = {"code": row}
    if not isinstance(row, dict):
        raise ValueError("持仓记录应为代码或对象")
    result = {"code": code_value(row.get("code")), "name": str(row.get("name") or ""),
              "note": str(row.get("note") or ""), "cost": None, "shares": None, "opened_on": row.get("opened_on") or None}
    for key in ("cost", "shares"):
        value = row.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool):
            raise ValueError(key + "需正数")
        number = float(value)
        if not math.isfinite(number) or number <= 0 or (key == "shares" and int(number) != number):
            raise ValueError(key + "需正数；股数为整数")
        result[key] = int(number) if key == "shares" else number
    if result["opened_on"]:
        day = date.fromisoformat(result["opened_on"])
        if day > date.today():
            raise ValueError("建仓日期不能在未来")
    if len(result["note"]) > 4000:
        raise ValueError("备注过长")
    return result


def load_positions(path="holdings.json"):
    file = Path(path)
    if not file.exists():
        return {"schema": 2, "revision": None, "holdings": []}
    raw = json.loads(file.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or not isinstance(raw.get("holdings"), list):
        raise ValueError('持仓格式应为 {"holdings": [...]}')
    positions = [validate_position(row) for row in raw["holdings"]]
    if len({p["code"] for p in positions}) != len(positions):
        raise ValueError("持仓代码重复")
    return {"schema": 2, "revision": raw.get("revision"), "holdings": positions}


@contextlib.contextmanager
def locked(path):
    lock = Path(str(path) + ".lock")
    for _ in range(100):
        try:
            lock.mkdir(); break
        except FileExistsError:
            time.sleep(.02)
    else:
        raise ValueError("持仓文件正在被另一窗口修改，请稍后重试；未覆盖原文件")
    try:
        yield
    finally:
        lock.rmdir()


def save_position(path, row=None, delete_code=None, expected_revision=None):
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with locked(file):
        current = load_positions(file)
        if current["revision"] != expected_revision:
            raise ValueError("持仓已在其他窗口修改，请重新加载后再保存")
        values = {p["code"]: p for p in current["holdings"]}
        if delete_code:
            values.pop(code_value(delete_code), None)
        if row:
            validated = validate_position(row)
            values[validated["code"]] = validated
        result = {"schema": 2, "revision": uuid.uuid4().hex, "holdings": [values[k] for k in sorted(values)]}
        temporary = file.with_name(file.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(file)
        return result


def update_profit_observation(previous, position, quote, now):
    """Observed raw-price P/L only; never infer an unobserved historical maximum."""
    cost = position.get("cost")
    if cost is None or not quote or quote.get("price") is None:
        return {"status": "not_available", "basis": "成本可不填；不影响MACD监控"}
    price = float(quote["price"])
    key = {k: position.get(k) for k in ("cost", "shares", "opened_on")}
    same_basis = previous and previous.get("position_basis") == key
    maximum = max(price, previous["maximum_observed_price"]) if same_basis else price
    started = previous["observation_started_at"] if same_basis else now.isoformat()
    profit = (price / cost - 1) * 100
    return {"status": "ok", "position_basis": key, "observation_started_at": started,
        "last_quote_at": quote.get("quote_at"), "price_basis": "实际未复权报价；未计手续费、现金分红、送转和仓位变化；成本由用户维护",
        "profit_pct": profit, "maximum_observed_price": maximum,
        "maximum_observed_profit_pct": (maximum / cost - 1) * 100,
        "observed_profit_drawdown_percentage_points": (maximum-price)/cost*100,
        "observed_price_drawdown_pct": (1-price/maximum)*100,
        "unrealized_profit_cny": (price-cost)*position["shares"] if position.get("shares") else None,
        "warning": "仅从首次监控记录起的已观察最大值，不是历史真实最高浮盈；除权分红需用户校正成本或重置观察基准"}
