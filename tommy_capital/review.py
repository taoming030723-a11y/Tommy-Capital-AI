"""Rescore a verified real-data artifact; never fetch or invent replacement prices."""
import argparse
import copy
import hashlib
import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

import requests

from .cli import clean, make_summary, write_outputs, scan_is_complete
from .coverage import ranking_completeness
from .data import SHANGHAI
from .reporting import github_run_url
from .scoring import (SCORING_VERSION, SCORE_WEIGHTS, RANKING_POLICY, balanced_score,
                      ranking_eligible, confirmed_ranking_eligible)
from .tracking import attach_tracking
from .patterns import PROFILE_VERSION, WEEKLY_MA55_VERSION


def review_report(source, now, source_hash, artifact=None):
    if source.get("status") not in ("complete", "partial") or not source.get("rankings"):
        raise ValueError("源扫描未完成或没有可核对的完整观察池")
    if source.get("scan_phase") != "closing" or source.get("scan_type") != "after_close":
        raise ValueError("复核源必须是已结束的真实收盘扫描，不能用旧复核循环替代")
    if source["session"] != source["generated_at"][:10] or source["session"] > now.date().isoformat():
        raise ValueError("源日线日期与收盘扫描不一致或含未来日期")
    if not re.fullmatch(r"[a-f0-9]{64}", source_hash):
        raise ValueError("完整源报告缺少SHA256")
    if any(row.get("technical", {}).get("timeframes", {}).get(period, {}).get("macro_profile", {}).get("version") != PROFILE_VERSION
           for row in source["rankings"] for period in ("monthly", "weekly", "daily")):
        raise ValueError("源附件缺少MA5/13/20及KDJ路径的原始计算记录，不能补造新版分项；须使用含新指标的真实收盘附件复核")
    if any(row.get("technical", {}).get("timeframes", {}).get("weekly", {}).get("macro_profile", {}).get("weekly_ma55", {}).get("version") != WEEKLY_MA55_VERSION
           for row in source["rankings"]):
        raise ValueError("源附件缺少逐周MA55及近期位置原始记录，不能补造新版分项；须使用含新指标的真实收盘附件复核")
    report = copy.deepcopy(source)
    report["model_review"] = {"source_generated_at": source["generated_at"],
        "source_finished_at": source["finished_at"], "source_scan_type": source["scan_type"],
        "source_scan_phase": source["scan_phase"], "source_scoring_version": source["scoring_version"],
        "source_commit": source.get("source_commit"), "source_run_url": source.get("workflow_run_url"),
        "source_report_sha256": source_hash, "artifact": artifact,
        "new_market_requests": 0, "new_evidence_requests": 0,
        "purpose": "模型规则复核；不构成历史收益回测，不把原有缺口改为通过"}
    report.update(generated_at=now.isoformat(), finished_at=now.isoformat(),
        mode="model_review", scan_phase="model_review", scan_type="model_review",
        scoring_version=SCORING_VERSION, score_weights=SCORE_WEIGHTS,
        ranking_policy=RANKING_POLICY, ranking_purpose="strategic_research_not_entry",
        model_validation={"status": "unvalidated_out_of_sample", "automatic_weight_training": False},
        source_commit=os.environ.get("GITHUB_SHA"), workflow_run_url=github_run_url())
    for row in report["rankings"]:
        row.update(balanced_score(row, row["technical"], row["route"], row["leading"], row["minute_frames"]))
        row["ranking_eligible"] = ranking_eligible(row)
    report["rankings"].sort(key=lambda r: (-r["score"], r["code"]))
    coverage = report["coverage"]
    coverage["ranking_eligible_count"] = sum(ranking_eligible(r) for r in report["rankings"])
    coverage["confirmed_ranking_count"] = sum(confirmed_ranking_eligible(r) for r in report["rankings"])
    coverage["financial_sector_review_missing"] = sum(r["fundamental_quality"]["financial_sector"] for r in report["rankings"])
    coverage["score_inputs_completed"] = sum(r["score_inputs_complete"] for r in report["rankings"])
    report["ranking_complete"], report["ranking_incomplete_reasons"] = ranking_completeness(report)
    report["scan_complete"] = source["scan_complete"] and scan_is_complete(report)
    report["status"] = "complete" if report["scan_complete"] else "partial"
    return report


def downloaded_source(repository, token, summary_path):
    summary = json.loads(Path(summary_path).read_text())
    run_url = summary.get("model_review", {}).get("source_run_url") if summary.get("model_review") else summary.get("workflow_run_url")
    match = re.fullmatch(r"https://github.com/" + re.escape(repository) + r"/actions/runs/(\d+)", run_url or "")
    if not match:
        raise ValueError("源Actions运行不是本仓库，不能任意下载外部输入")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    url = f"https://api.github.com/repos/{repository}/actions/runs/{match[1]}/artifacts"
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    artifacts = [a for a in response.json()["artifacts"] if not a["expired"] and
                 a["name"] in ("tommy-capital-top20-report", "tommy-capital-report")]
    if len(artifacts) != 1:
        raise ValueError("必须唯一匹配未过期的真实扫描附件")
    item = artifacts[0]
    if item["size_in_bytes"] > 50_000_000:
        raise ValueError("源附件超过大小界限")
    response = requests.get(item["archive_download_url"], headers=headers, timeout=60)
    response.raise_for_status()
    raw = response.content
    digest = hashlib.sha256(raw).hexdigest()
    if item.get("digest") != "sha256:" + digest:
        raise ValueError("源附件SHA256与GitHub记录不符")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entry = archive.getinfo("report.json")
        if entry.file_size > 400_000_000:
            raise ValueError("完整报告超过大小界限")
        content = archive.read(entry)
    report = json.loads(content)
    if not report.get("model_review") and report.get("workflow_run_url") != run_url:
        raise ValueError("源完整报告Actions身份不符")
    return content, {"run_url": run_url, "artifact_id": item["id"], "zip_sha256": digest}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="完整真实收盘report.json；未指定则按latest-summary下载并验证Actions附件")
    parser.add_argument("--output", default="reports/latest")
    args = parser.parse_args()
    if args.input:
        content, artifact = Path(args.input).read_bytes(), None
    else:
        content, artifact = downloaded_source(os.environ["REPO_NAME"], os.environ["GH_TOKEN"], "results/latest-summary.json")
    report = review_report(json.loads(content), datetime.now(SHANGHAI), hashlib.sha256(content).hexdigest(), artifact)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "source-report.json").write_bytes(content)
    report["finished_at"] = datetime.now(SHANGHAI).isoformat()
    attach_tracking(report)
    write_outputs(out, report)
    summary = clean(make_summary(report))
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps({"source_session": report["session"], "review_generated_at": report["generated_at"],
        "requested": report["coverage"]["technical_requested"], "completed": report["coverage"]["technical_completed"],
        "research_eligible": summary["ranking_eligible_count"], "displayed": len(summary["top_candidates"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
