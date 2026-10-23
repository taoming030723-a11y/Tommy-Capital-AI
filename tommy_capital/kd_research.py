"""Freeze focused K/D cohorts and compare future outcomes without changing weights."""
import copy
import hashlib
import json
from datetime import datetime

from .data import SHANGHAI
from .kd_state import PERIODS, VERSION, load_policy


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def freeze_kd(report, now=None):
    focused = report.get("technical_reviews") or {}
    if not focused.get("public") or not any(r.get("kd_monitor") for r in focused.get("rows", [])):
        return None
    from .monitor import validate_public_monitor
    validate_public_monitor(focused)
    stamp = now or datetime.now(SHANGHAI)
    if stamp.tzinfo is None or stamp < datetime.fromisoformat(report["finished_at"]):
        raise ValueError("K/D观察决策不得回填到实际扫描完成之前")
    ignored = set(report.get("top10_tracking", {}).get("preferences", {}).get("ignored_codes", []))
    allowed = {r["code"] for r in report.get("top10_tracking", {}).get("rows", [])
               if r["appearance_count"] >= 3 and r["code"] not in ignored}
    rows = []
    for row in focused["rows"]:
        if row["code"] not in allowed:
            continue
        if row.get("session") != report["session"]:
            raise ValueError("K/D观察不可混用其他交易日")
        monitor = row.get("kd_monitor")
        if monitor:
            for frame in monitor["frames"].values():
                if frame.get("status") == "ok" and datetime.fromisoformat(frame["last_bar"]).replace(tzinfo=SHANGHAI) > stamp:
                    raise ValueError("K/D含未来或未完成K线")
                if frame.get("status") == "ok" and frame["period"] in ("daily", "120", "60", "30", "15") and frame["last_bar"][:10] != report["session"]:
                    raise ValueError("K/D最新日线或分钟数据未覆盖观察交易日")
        rows.append({"code": row["code"], "name": row.get("name"), "kd_monitor": copy.deepcopy(monitor)})
    if len({r["code"] for r in rows}) != len(rows):
        raise ValueError("K/D观察快照含重复股票")
    body = {"schema": 1, "version": VERSION, "decision_at": stamp.isoformat(), "data_session": report["session"],
            "source_scan_finished_at": report["finished_at"], "source_run_url": report.get("workflow_run_url"),
            "production_version": report["scoring_version"], "policy": load_policy(), "stocks": sorted(rows, key=lambda r: r["code"]),
            "scope": "未忽略且累计三个不同正式收盘日Top10的公开观察股；不是全市场随机样本",
            "influences_ranking": False, "validation_status": "unvalidated_out_of_sample",
            "basis": "按实际生成时间冻结；下一真实交易日开盘起观察；120/60及30/15相关组不以数量叠加"}
    body["sha256"] = digest(body)
    return body


def validate_kd_snapshot(snapshot):
    body = {k: v for k, v in snapshot.items() if k != "sha256"}
    if digest(body) != snapshot.get("sha256"):
        raise ValueError("K/D冻结观察已被改写")
    stamp = datetime.fromisoformat(snapshot["decision_at"])
    if stamp.tzinfo is None or snapshot["data_session"] > stamp.astimezone(SHANGHAI).date().isoformat():
        raise ValueError("K/D观察日期无效")
    if len({r["code"] for r in snapshot["stocks"]}) != len(snapshot["stocks"]):
        raise ValueError("K/D观察名单重复")
    return snapshot


def evaluate_kd(snapshots, trading_days, as_of, history_for, cost_bps=20.):
    from .research import evaluate_snapshot
    checks = []
    for snapshot in snapshots:
        validate_kd_snapshot(snapshot)
        for stock in snapshot["stocks"]:
            # Adapter to the existing date/price/cost evaluator, one observed stock
            # at a time. This is not a fabricated production Top20 decision.
            adapter = {"schema": 1, "decision_at": snapshot["decision_at"], "data_session": snapshot["data_session"],
                       "model_version": snapshot["version"], "ranking_purpose": "independent_kd_observation_cohort",
                       "top20": [{"code": stock["code"], "name": stock["name"]}]}
            adapter["snapshot_sha256"] = digest(adapter)
            states = (stock.get("kd_monitor") or {}).get("frames", {})
            for item in evaluate_snapshot(adapter, trading_days, as_of, history_for, cost_bps, horizons=(1, 5, 20, 60)):
                item["kd_snapshot_sha256"] = snapshot["sha256"]
                item["kd_policy_sha256"] = digest(snapshot["policy"])
                item["production_version"] = snapshot["production_version"]
                item["states"] = {p: states.get(p, {}).get("state", "unavailable") for p in PERIODS}
                checks.append(item)
    for item in checks:
        peers = [c for c in checks if c["kd_snapshot_sha256"] == item["kd_snapshot_sha256"] and c["horizon_sessions"] == item["horizon_sessions"] and c["status"] == "observed"]
        item["same_decision_pool_mean_pct"] = sum(c["hypothetical_net_return_pct"] for c in peers)/len(peers) if peers else None
    buckets = []
    for policy_id, production_version in sorted({(c["kd_policy_sha256"], c["production_version"]) for c in checks}):
        for horizon in (1, 5, 20, 60):
            all_checks = [c for c in checks if c["horizon_sessions"] == horizon and c["kd_policy_sha256"] == policy_id and c["production_version"] == production_version]
            for p in PERIODS:
                for state in sorted({c["states"][p] for c in all_checks}):
                    cohort = [c for c in all_checks if c["states"][p] == state]
                    observed = [c for c in cohort if c["status"] == "observed"]
                    mean = sum(c["hypothetical_net_return_pct"] for c in observed)/len(observed) if observed else None
                    baseline = sum(c["same_decision_pool_mean_pct"] for c in observed)/len(observed) if observed else None
                    buckets.append({"period": p, "state": state, "horizon_sessions": horizon, "kd_policy_sha256": policy_id,
                                    "production_version": production_version, "requested": len(cohort), "observed": len(observed),
                                    "missing": sum(c["status"] == "missing" for c in cohort), "pending": sum(c["status"] == "pending" for c in cohort),
                                    "mean_hypothetical_net_pct": mean, "same_pool_baseline_pct": baseline,
                                    "difference_to_same_pool_pct": mean-baseline if mean is not None and baseline is not None else None})
    return {"version": VERSION, "as_of_session": str(as_of), "snapshot_count": len(snapshots), "checks": checks, "buckets": buckets,
            "validation_status": "unvalidated_out_of_sample", "automatic_weight_training": False,
            "limitations": "同决策日同观察池基准匹配，按生产版本与K/D配置分组；前瞻价格差异不是因果增量或全市场回测。重叠窗口和相关周期不是独立样本，缺失保留，未解决幸存者偏差、完整成交约束或多重检验"}
