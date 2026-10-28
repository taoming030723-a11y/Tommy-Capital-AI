"""Publish matching machine and human reports in a single GitHub commit."""
import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from .reporting import render_markdown, github_run_url


def publish(directory, token, repository):
    if not token or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("缺少GitHub报告发布凭据或仓库名称")
    directory = Path(directory)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    if (summary.get("generated_at") != report.get("generated_at") or
            summary.get("status") != report.get("status") or report.get("status") == "running" or
            summary.get("candidate_count") != len(report.get("rankings", []))):
        raise ValueError("完整报告与机器摘要不匹配，不能发布为最终结果")
    for key in ["selection_rule", "scan_phase", "scan_type", "session", "minute15_cutoff", "scan_complete", "coverage", "scoring_version", "score_weights", "display_limit", "ranking_complete", "ranking_incomplete_reasons", "ranking_policy", "ranking_purpose", "model_review", "model_validation", "forward_evaluation", "top10_tracking","macd_monitor", "valuations", "valuation_coverage", "technical_reviews"]:
        if summary.get(key) != report.get(key):
            raise ValueError(f"中文报告与机器摘要的{key}不一致")
    for key in ("investment_research", "data_quality", "financial_integrity", "financial_integrity_required"):
        if summary.get(key) != report.get(key):
            raise ValueError(f"研究附加字段{key}与机器摘要不一致")
    if report.get("investment_research") is not None:
        from .investment_research import validate_public_research
        validate_public_research(report["investment_research"])
    markdown = render_markdown(report, github_run_url())
    from .financial_integrity import validate_integrity
    validate_integrity(report, markdown)
    (directory / "report.md").write_text(markdown, encoding="utf-8")
    base = "https://api.github.com/repos/" + repository
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"}
    def api(method, path, payload=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = urllib.request.Request(base + path, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    entries = [{"path": "results/latest-summary.json", "mode": "100644", "type": "blob",
                "content": json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False)},
               {"path": "results/latest-report.md", "mode": "100644", "type": "blob", "content": markdown}]
    phase = report.get("scan_phase")
    from .monitor import validate_public_monitor
    validate_public_monitor(report.get("macd_monitor"))
    validate_public_monitor(report.get("technical_reviews"))
    if (report.get("macd_monitor") or {}).get("public") is True and (directory/"monitor-state.json").exists():
        monitor_state=json.loads((directory/"monitor-state.json").read_text())
        validate_public_monitor(report.get("macd_monitor"),monitor_state)
        if any("profit_observation" in value for value in monitor_state.get("stocks",{}).values()):
            raise ValueError("公开监控状态含个人浮盈信息，拒绝发布")
        entries.append({"path":"results/macd-event-state.json","mode":"100644","type":"blob",
                        "content":json.dumps(monitor_state,ensure_ascii=False,indent=2,allow_nan=False)})
    if phase in {"opening", "closing", "intraday", "pre_run", "model_review"}:
        day = str(report["generated_at"])[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("归档报告缺少有效扫描日期")
        entries += [{**entries[0], "path": f"results/{day}/{phase}-summary.json"},
                    {**entries[1], "path": f"results/{day}/{phase}-report.md"}]
    if (directory / "research-snapshot.json").is_file():
        snapshot = json.loads((directory / "research-snapshot.json").read_text())
        entries.append({"path": snapshot["repository_path"], "mode": "100644", "type": "blob",
                        "content": json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False)})
    if (directory / "forward-evaluation.json").is_file():
        entries.append({"path": "research/latest-evaluation.json", "mode": "100644", "type": "blob",
                        "content": (directory / "forward-evaluation.json").read_text()})
    if (directory / "ablation-snapshot.json").is_file():
        trial = json.loads((directory / "ablation-snapshot.json").read_text())
        from .backtest import freeze_ablation
        from datetime import datetime
        if trial != freeze_ablation(report, datetime.fromisoformat(trial["decision_at"])):
            raise ValueError("消融决策快照与原始分项不一致，拒绝发布")
        path = "research/experiments/" + trial["decision_at"][:10] + "/" + trial["sha256"] + ".json"
        entries.append({"path": path, "mode": "100644", "type": "blob", "content": json.dumps(trial, ensure_ascii=False, indent=2)})
    if (directory / "kd-observation-snapshot.json").is_file():
        from .kd_research import freeze_kd, validate_kd_snapshot
        from datetime import datetime
        trial = json.loads((directory / "kd-observation-snapshot.json").read_text())
        validate_kd_snapshot(trial)
        if trial != freeze_kd(report, datetime.fromisoformat(trial["decision_at"])):
            raise ValueError("K/D独立冻结记录与观察名单不一致")
        entries.append({"path": "research/kd-observations/"+trial["decision_at"][:10]+"/"+trial["sha256"]+".json",
                        "mode": "100644", "type": "blob", "content": json.dumps(trial,ensure_ascii=False,indent=2)})
    if (directory / "kd-forward-evaluation.json").is_file():
        entries.append({"path": "research/latest-kd-evaluation.json", "mode": "100644", "type": "blob",
                        "content": (directory/"kd-forward-evaluation.json").read_text()})
    if report.get("top10_history") is not None and phase == "closing" and report.get("status") in ("complete", "partial"):
        from .tracking import audit_tracking
        history = audit_tracking(report)
        if json.loads((directory / "top10-history.json").read_text()) != history:
            raise ValueError("前10累计记录与报告不匹配，不能发布")
        entries.append({"path": "results/top10-history.json", "mode": "100644", "type": "blob",
                        "content": json.dumps(history, ensure_ascii=False, indent=2, allow_nan=False)})
    for attempt in range(3):
        parent = api("GET", "/git/ref/heads/main")["object"]["sha"]
        previous = api("GET", "/git/commits/" + parent)
        if phase == "closing" and report.get("top10_tracking") is not None:
            from .tracking import PREFERENCES_PATH, apply_preferences, validate_preferences, ZONE
            from datetime import datetime
            try:
                preference_file = api("GET", "/contents/" + PREFERENCES_PATH + "?ref=" + parent)
                if not preference_file.get("content"):
                    preference_file = api("GET", "/git/blobs/" + preference_file["sha"])
                preferences = validate_preferences(json.loads(base64.b64decode(preference_file["content"])))
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                preferences = {"schema": 1, "ignored_codes": []}
            view = apply_preferences(report["top10_tracking"], preferences)
            if view != report["top10_tracking"]:
                # An ignore selection made during the scan must survive its later publication.
                view.update(preferences_updated_at=datetime.now(ZONE).isoformat(), preferences_source_commit=parent,
                    preferences_update_scope="发布时采用最新用户观察偏好；市场事实和排名保持")
                report["top10_tracking"] = summary["top10_tracking"] = view
                if report.get("macd_monitor") is not None:
                    from .watchlist import monitor_preference_view
                    monitor=monitor_preference_view(report["macd_monitor"],view)
                    report["macd_monitor"]=summary["macd_monitor"]=monitor
                if report.get("technical_reviews") is not None:
                    from .watchlist import technical_preference_view
                    technical = technical_preference_view(report["technical_reviews"], view)
                    report["technical_reviews"] = summary["technical_reviews"] = technical
                if report.get("investment_research") is not None:
                    from .investment_research import build_research
                    previous_research = report["investment_research"]
                    report["investment_research"] = summary["investment_research"] = build_research(report, previous_research.get("assumptions"), previous_research.get("policy"))
                if report.get("data_quality") is not None:
                    from .diagnostics import report_diagnostics
                    report["data_quality"] = summary["data_quality"] = report_diagnostics(report)
                if (directory / "kd-observation-snapshot.json").is_file():
                    from .kd_research import freeze_kd
                    old_trial = json.loads((directory / "kd-observation-snapshot.json").read_text())
                    revised = freeze_kd(report, datetime.fromisoformat(old_trial["decision_at"]))
                    entries[:] = [e for e in entries if not e["path"].startswith("research/kd-observations/")]
                    if revised is not None:
                        (directory / "kd-observation-snapshot.json").write_text(json.dumps(revised,ensure_ascii=False,indent=2))
                        entries.append({"path": "research/kd-observations/"+revised["decision_at"][:10]+"/"+revised["sha256"]+".json",
                                        "mode": "100644", "type": "blob", "content": json.dumps(revised,ensure_ascii=False,indent=2)})
                    else:
                        (directory / "kd-observation-snapshot.json").unlink()
                markdown = render_markdown(report, github_run_url())
                for entry in entries:
                    if entry["path"] in ("results/latest-summary.json", f"results/{day}/closing-summary.json"):
                        entry["content"] = json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False)
                    elif entry["path"] in ("results/latest-report.md", f"results/{day}/closing-report.md"):
                        entry["content"] = markdown
                for filename, data in (("report.json", report), ("summary.json", summary)):
                    (directory / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                (directory / "report.md").write_text(markdown, encoding="utf-8")
        tree = api("POST", "/git/trees", {"base_tree": previous["tree"]["sha"], "tree": entries})
        commit = api("POST", "/git/commits", {"message": "更新中文扫描报告及机器摘要 [skip ci]",
                                                "tree": tree["sha"], "parents": [parent]})
        try:
            api("PATCH", "/git/refs/heads/main", {"sha": commit["sha"], "force": False})
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
                    output.write(markdown)
            return commit["sha"]
        except urllib.error.HTTPError as exc:
            # A concurrent code push must be preserved; rebuild on fresh HEAD.
            if exc.code not in (409, 422) or attempt == 2:
                raise
    raise RuntimeError("中文报告发布未完成")


def main():
    publish("reports/latest", os.environ.get("GH_TOKEN", ""), os.environ.get("REPO_NAME", ""))
    print("已更新 results/latest-report.md、机器摘要和 Actions 中文报告。")


if __name__ == "__main__":
    main()
