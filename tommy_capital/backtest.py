"""Frozen-decision experiments, benchmark observations and purged walk-forward splits.

No reconstructed universe or hindsight-adjusted financial statement is accepted
as proof of a full historical strategy. Missing outcomes remain in the denominator.
"""
import hashlib
import json
import math
from datetime import datetime
import pandas as pd
from .data import SHANGHAI, DataError
from .scoring import WEEKLY_WEIGHTS, ranking_eligible


def ablation_score(row, variant):
    weekly = row.get("score_breakdown", {}).get("technical", {}).get("weekly", {})
    if weekly.get("component_maxima") != WEEKLY_WEIGHTS:
        raise ValueError("消融需要v9原始分项及上限，不能补造旧版评分输入")
    if variant == "v9":
        return row["fundamental_score"] + row["technical_score"]
    maxima, parts = dict(WEEKLY_WEIGHTS), weekly["parts"]
    if variant == "ma55_12":
        for key in list(maxima):
            if key.startswith("weekly_ma55_"):
                maxima[key] *= .75
        maxima["volume_above_previous20_mean"] += 2
        maxima["base_or_higher_low_or_contraction_or_breakout"] += 2
    elif variant == "without_weekly_histogram":
        maxima["macd_histogram_state"] = 0
        maxima["volume_above_previous20_mean"] += 3
        maxima["base_or_higher_low_or_contraction_or_breakout"] += 2
    elif variant == "fundamental_ma55_only":
        ma55 = sum(parts.get(k, 0) for k in maxima if k.startswith("weekly_ma55_"))
        return row["fundamental_score"] + max(0., ma55 + parts.get("weekly_ma55_lost_support_penalty", 0)) / 16 * 50
    elif variant != "v9":
        raise ValueError("未知消融版本")
    assert abs(sum(maxima.values()) - 30) < 1e-9
    positive = sum(parts.get(key, 0) / WEEKLY_WEIGHTS[key] * maximum for key, maximum in maxima.items())
    penalties = sum(value for key, value in parts.items() if key.endswith("penalty"))
    weekly_score = min(30, max(0., positive + penalties))
    return row["fundamental_score"] + row["score_breakdown"]["technical"]["monthly"]["score"] + weekly_score


def freeze_ablation(report, now=None):
    now = now or datetime.now(SHANGHAI)
    rows = [r for r in report.get("rankings", []) if ranking_eligible(r)]
    trials = {}
    for variant in ("v9", "ma55_12", "without_weekly_histogram", "fundamental_ma55_only"):
        ranked = sorted(({"code": r["code"], "name": r.get("name"), "score": ablation_score(r, variant)} for r in rows), key=lambda r: (-r["score"], r["code"]))
        trials[variant] = ranked[:20]
    body = {"schema": 1, "decision_at": now.isoformat(), "data_session": report["session"],
            "production_version": report["scoring_version"], "source_commit": report.get("source_commit"),
            "source_decision_at": report["finished_at"], "universe_scope": "verified_candidate_pool_only",
            "point_in_time_universe_complete": False, "candidate_count": len(rows), "trials": trials,
            "precision_basis": "变体按已冻结分项精度重分配；基准直接保留原始生产总分，不以四舍五入分项反推",
            "status": "prospective_experiment_not_validated", "production_weights_changed": False}
    body["sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return body


def walk_forward_splits(sessions, training_sessions=120, test_sessions=20, purge_sessions=60):
    dates = sorted(set(pd.Timestamp(x).date() for x in sessions))
    if min(training_sessions, test_sessions) < 1 or purge_sessions < 60:
        raise ValueError("训练/测试窗口需为正；60日结果需要至少60交易日隔离，不能让训练标签进入测试期")
    result = []
    end = training_sessions
    while end + purge_sessions + test_sessions <= len(dates):
        start = end + purge_sessions
        result.append({"training_start": dates[end-training_sessions].isoformat(), "training_end": dates[end-1].isoformat(),
                       "test_start": dates[start].isoformat(), "test_end": dates[start+test_sessions-1].isoformat(),
                       "purge_sessions": purge_sessions})
        end += test_sessions
    return result


def price_window_return(history, expected_days):
    columns = ["日期", "开盘", "收盘", "最高", "最低"]
    if not set(columns) <= set(history):
        raise DataError("基准K线字段不足")
    frame = history[columns].copy()
    frame["日期"] = pd.to_datetime(frame["日期"]).dt.date
    if frame["日期"].duplicated().any():
        raise DataError("基准交易日重复")
    frame = frame.set_index("日期")
    if any(d not in frame.index for d in expected_days):
        raise DataError("基准或股票评价窗口缺失真实交易日，不跳过日期")
    window = frame.loc[expected_days]
    for _, row in window.iterrows():
        if any(not math.isfinite(float(x)) or x <= 0 for x in row):
            raise DataError("评价价格无效")
        if row["最低"] > min(row["开盘"], row["收盘"]) or row["最高"] < max(row["开盘"], row["收盘"]):
            raise DataError("评价价格范围无效")
    entry, exit_price = float(window.iloc[0]["开盘"]), float(window.iloc[-1]["收盘"])
    curve = [entry, *window["收盘"].astype(float)]
    peak, drawdown = curve[0], 0.
    for value in curve:
        peak = max(peak, value)
        drawdown = min(drawdown, value/peak-1)
    return {"price_return_pct": (exit_price/entry-1)*100, "price_max_drawdown_pct": drawdown*100}


def attach_benchmarks(checks, trading_days, benchmark_history_for, benchmark_codes=("000300", "000905")):
    days = sorted(set(pd.Timestamp(x).date() for x in trading_days))
    output = []
    for check in checks:
        row = dict(check)
        row["benchmarks"] = []
        if check["status"] == "observed":
            expected = [d for d in days if check["entry_session"] <= d.isoformat() <= check["exit_session"]]
            for code in benchmark_codes:
                item = {"code": code, "status": "missing"}
                try:
                    history = benchmark_history_for(code, expected[0], expected[-1])
                    value = price_window_return(history, expected)
                    item.update(status="observed", **value,
                                hypothetical_net_excess_pct=check["hypothetical_net_return_pct"]-value["price_return_pct"],
                                basis="股票前复权价格与价格指数的观察比较，指数未含现金分红；非完整总回报alpha")
                except (DataError, ValueError, KeyError, IndexError, TypeError) as exc:
                    item["reason"] = str(exc)
                row["benchmarks"].append(item)
        output.append(row)
    return output


def summarize_checks(checks):
    result = {"requested": len(checks), "observed": sum(c["status"] == "observed" for c in checks),
              "missing": sum(c["status"] == "missing" for c in checks), "pending": sum(c["status"] == "pending" for c in checks),
              "validation_status": "unvalidated_out_of_sample", "survivorship_bias_resolved": False,
              "overlapping_windows_are_independent": False, "horizons": {}}
    for horizon in (1, 5, 20, 60):
        values = [c["hypothetical_net_return_pct"] for c in checks if c["status"] == "observed" and c["horizon_sessions"] == horizon]
        result["horizons"][str(horizon)] = {"samples": len(values), "mean_hypothetical_net_pct": sum(values)/len(values) if values else None,
            "win_fraction": sum(v>0 for v in values)/len(values) if values else None,
            "profit_factor": sum(v for v in values if v>0)/abs(sum(v for v in values if v<0)) if any(v<0 for v in values) else None,
            "worst_stock_close_drawdown_pct": min((c["stock_close_max_drawdown_pct"] for c in checks if c["status"]=="observed" and c["horizon_sessions"]==horizon and c.get("stock_close_max_drawdown_pct") is not None), default=None)}
    return result


def snapshot_turnover(snapshots):
    rows, previous = [], {}
    for item in sorted(snapshots, key=lambda x: x["decision_at"]):
        version = item["model_version"]
        codes = {r["code"] for r in item["top20"]}
        old = previous.get(version)
        if old and old["data_session"] != item["data_session"]:
            old_codes = {r["code"] for r in old["top20"]}
            rows.append({"model_version": version, "from_session": old["data_session"], "to_session": item["data_session"],
                         "name_turnover": 1-len(codes&old_codes)/max(len(codes),len(old_codes)) if codes or old_codes else 0.,
                         "basis": "同版本已冻结名单更替率，非实际成交金额周转率；跨版本不混比"})
        previous[version] = item
    return rows


def market_regime(history, as_of):
    frame = history.copy()
    try:
        frame["日期"] = pd.to_datetime(frame["日期"])
        frame = frame.loc[frame["日期"] <= pd.Timestamp(as_of)].sort_values("日期")
        if len(frame)<120 or frame["日期"].duplicated().any() or frame.iloc[-1]["日期"].date().isoformat()!=as_of:
            raise ValueError("指数历史不足、重复或未覆盖指定已收盘日")
        close = frame["收盘"].astype(float)
        if not close.map(lambda x: math.isfinite(x) and x>0).all():
            raise ValueError("指数价格不完整")
        ma = close.rolling(55).mean();returns=close.pct_change(fill_method=None).dropna()
        vol20,vol120=returns.tail(20).std(),returns.tail(120).std()
        trend=bool(close.iloc[-1]>ma.iloc[-1] and ma.iloc[-1]>=ma.iloc[-5])
        return {"status":"ok","as_of":as_of,"price_above_ma55":bool(close.iloc[-1]>ma.iloc[-1]),
                "trend_healthy":trend,"annualized_volatility_20":float(vol20*math.sqrt(252)),
                "volatility_elevated":bool(vol120>0 and vol20>vol120*1.5),
                "label":"指数趋势支持" if trend else "指数趋势需谨慎观察", "influences_ranking":False,
                "basis":"仅使用指定收盘日之前指数记录；市场风险背景，不机械剔除个股或自动改变仓位"}
    except (ValueError,KeyError,TypeError,IndexError) as exc:
        return {"status":"unavailable","reason":str(exc),"influences_ranking":False}
