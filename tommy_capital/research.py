"""Freeze decisions and observe subsequent real prices; this is not a trade simulator."""
import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

from .cli import clean, make_summary, write_outputs
from .data import Provider, DataError, daily_history, SHANGHAI
from .scoring import ranked_top20

HORIZONS = (1, 5, 20)
MAX_SNAPSHOTS = 20


def freeze_snapshot(report):
    """A timestamped forecast cannot become a retroactive historical selection."""
    if report.get("scan_phase") not in ("closing", "model_review") or report["status"] not in ("complete", "partial"):
        return None
    rows = ranked_top20(report["rankings"])
    body = {"schema": 1, "decision_at": report["finished_at"], "data_session": report["session"],
        "model_version": report["scoring_version"], "source_commit": report.get("source_commit"),
        "ranking_purpose": "strategic_research_not_entry", "ranking_complete": report["ranking_complete"],
        "source_review": report.get("model_review"), "config": report["config"], "weights": report["score_weights"],
        "top20": [{k: r.get(k) for k in ("code", "name", "score", "fundamental_score", "technical_score", "confirmation_state")} for r in rows],
        "evaluation_basis": "决策完成之后首个交易日开盘至第1/5/20个交易日收盘；价格观察，不是下单或已验证策略收益"}
    canonical = json.dumps(clean(body), sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    body["snapshot_sha256"] = hashlib.sha256(canonical).hexdigest()
    stamp = datetime.fromisoformat(body["decision_at"]).astimezone(SHANGHAI)
    body["repository_path"] = f"research/snapshots/{stamp:%Y-%m-%d}/{stamp:%H%M%S}-{body['model_version']}-{body['snapshot_sha256'][:12]}.json"
    return clean(body)


def validate_snapshot(snapshot):
    content = {k: v for k, v in snapshot.items() if k not in ("snapshot_sha256", "repository_path")}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if snapshot.get("snapshot_sha256") != digest:
        raise ValueError("冻结研究名单已被改写，拒绝评价")
    stamp = datetime.fromisoformat(snapshot["decision_at"])
    if stamp.tzinfo is None or snapshot["data_session"] > stamp.astimezone(SHANGHAI).date().isoformat():
        raise ValueError("决策日期或数据截止日期无效")
    if len(snapshot["top20"]) > 20 or len({r["code"] for r in snapshot["top20"]}) != len(snapshot["top20"]):
        raise ValueError("冻结名单超过20只或含重复股票")


def evaluate_snapshot(snapshot, trading_days, as_of, history_for, cost_bps=20.):
    validate_snapshot(snapshot)
    if not 0 <= cost_bps <= 100:
        raise ValueError("每边假设成本须为0到100bp")
    decision_day = datetime.fromisoformat(snapshot["decision_at"]).astimezone(SHANGHAI).date()
    days = sorted(set(pd.Timestamp(d).date() for d in trading_days))
    if not days or days[-1] <= decision_day:
        raise DataError("真实交易日历没有覆盖未来评价窗口")
    following = [d for d in days if d > decision_day]
    rows = []
    for stock in snapshot["top20"]:
        for horizon in HORIZONS:
            item = {"snapshot_sha256": snapshot["snapshot_sha256"], "decision_day": decision_day.isoformat(),
                    "code": stock["code"], "name": stock["name"], "horizon_sessions": horizon,
                    "model_version": snapshot["model_version"], "status": "pending", "cost_bps_per_side": cost_bps}
            if len(following) < horizon:
                item["reason"] = "日历尚未覆盖完整窗口"
            elif following[horizon - 1] <= as_of:
                entry, exit_day = following[0], following[horizon - 1]
                item.update(entry_session=entry.isoformat(), exit_session=exit_day.isoformat())
                try:
                    history = history_for(stock["code"], entry, exit_day)
                    required = ["日期", "开盘", "收盘", "最高", "最低"]
                    if not set(required) <= set(history):
                        raise DataError("真实价格字段未齐")
                    prices = history.copy()
                    prices["日期"] = pd.to_datetime(prices["日期"]).dt.date
                    if prices["日期"].duplicated().any():
                        raise DataError("评价日线日期重复")
                    prices = prices.set_index("日期")
                    if any(day not in prices.index for day in following[:horizon]):
                        raise DataError("窗口缺少真实交易日，可能停牌，不跳过缺失日计算")
                    for day in following[:horizon]:
                        bar = {key: float(prices.loc[day, key]) for key in ("开盘", "收盘", "最高", "最低")}
                        if not all(math.isfinite(value) and value > 0 for value in bar.values()):
                            raise DataError("评价K线不是有限正数")
                        if bar["最低"] > min(bar["开盘"], bar["收盘"]) or max(bar["开盘"], bar["收盘"]) > bar["最高"]:
                            raise DataError("评价K线的高低价与开收盘价不一致")
                    opening = float(prices.loc[entry, "开盘"])
                    closing = float(prices.loc[exit_day, "收盘"])
                    if not pd.notna(opening) or not pd.notna(closing) or not 0 < opening < float("inf") or not 0 < closing < float("inf"):
                        raise DataError("评价价格不是有限正数")
                    if float(prices.loc[entry, "最高"]) == float(prices.loc[entry, "最低"]):
                        raise DataError("首日单价K线，未证明可按开盘价成交")
                    fee = cost_bps / 10000
                    item.update(status="observed", gross_price_return_pct=(closing/opening-1)*100,
                        hypothetical_net_return_pct=(closing*(1-fee)/(opening*(1+fee))-1)*100,
                        entry_open_qfq=opening, exit_close_qfq=closing,
                        price_basis="同次真实前复权价格；净值为每边成本假设，非实际成交收益")
                except (DataError, KeyError, ValueError, TypeError) as exc:
                    item.update(status="missing", reason=str(exc))
            else:
                item.update(entry_session=following[0].isoformat(), exit_session=following[horizon-1].isoformat())
            rows.append(item)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="reports/latest")
    parser.add_argument("--snapshot-directory", default="research/snapshots")
    args = parser.parse_args()
    root = Path(args.output)
    report = json.loads((root / "report.json").read_text())
    current = freeze_snapshot(report)
    if current is None:
        return
    files = sorted(Path(args.snapshot_directory).glob("*/*.json"))[-MAX_SNAPSHOTS:]
    snapshots = [json.loads(p.read_text()) for p in files]
    if current["snapshot_sha256"] not in {s["snapshot_sha256"] for s in snapshots}:
        snapshots.append(current)
    provider = Provider()
    calendar = provider.fetch("tool_trade_date_hist_sina", ttl=86400)
    days = pd.to_datetime(calendar.trade_date, errors="coerce").dropna().dt.date.tolist()
    as_of = pd.Timestamp(report["session"]).date()
    cache = {}
    def history_for(code, entry, end):
        # Same qfq response supplies entry and exit. No old selection-time qfq
        # price is mixed with a later adjusted-price anchor.
        if code not in cache:
            raw, _ = daily_history(provider, code, None, as_of.strftime("%Y%m%d"), as_of.strftime("%Y%m%d"), required_session=as_of)
            cache[code] = raw
        return cache[code]
    checks = []
    for snapshot in snapshots:
        checks.extend(evaluate_snapshot(snapshot, days, as_of, history_for))
    evaluation = {"evaluated_at": datetime.now(SHANGHAI).isoformat(), "as_of_session": report["session"],
        "snapshot_count": len(snapshots), "scope": f"最近{MAX_SNAPSHOTS}份冻结记录及当前记录；重叠窗口不是独立样本",
        "matured_checks": sum(r["status"] == "observed" for r in checks),
        "pending_checks": sum(r["status"] == "pending" for r in checks),
        "missing_checks": sum(r["status"] == "missing" for r in checks),
        "distinct_matured_decision_days": len({r["decision_day"] for r in checks if r["status"] == "observed"}),
        "validation_status": "unvalidated_out_of_sample", "automatic_weight_training": False,
        "benchmark_comparison": "not_implemented", "order_execution": False,
        "checks": checks, "lineage": provider.lineage}
    report["forward_evaluation"] = {k: v for k, v in evaluation.items() if k not in ("checks", "lineage")}
    (root / "research-snapshot.json").write_text(json.dumps(current, ensure_ascii=False, indent=2, allow_nan=False))
    (root / "forward-evaluation.json").write_text(json.dumps(evaluation, ensure_ascii=False, indent=2, allow_nan=False))
    write_outputs(root, report)
    (root / "summary.json").write_text(json.dumps(clean(make_summary(report)), ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps(report["forward_evaluation"], ensure_ascii=False))


if __name__ == "__main__":
    main()
