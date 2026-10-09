"""Publish matching machine and human reports in a single GitHub commit."""
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
    for key in ["selection_rule", "scan_phase", "scan_type", "session", "minute15_cutoff", "scan_complete", "coverage", "scoring_version", "score_weights", "display_limit", "ranking_complete", "ranking_incomplete_reasons", "ranking_policy"]:
        if summary.get(key) != report.get(key):
            raise ValueError(f"中文报告与机器摘要的{key}不一致")
    markdown = render_markdown(report, github_run_url())
    (directory / "report.md").write_text(markdown, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(markdown)
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
    if phase in {"opening", "closing", "intraday", "pre_run"}:
        day = str(report["generated_at"])[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("归档报告缺少有效扫描日期")
        entries += [{**entries[0], "path": f"results/{day}/{phase}-summary.json"},
                    {**entries[1], "path": f"results/{day}/{phase}-report.md"}]
    for attempt in range(3):
        parent = api("GET", "/git/ref/heads/main")["object"]["sha"]
        previous = api("GET", "/git/commits/" + parent)
        tree = api("POST", "/git/trees", {"base_tree": previous["tree"]["sha"], "tree": entries})
        commit = api("POST", "/git/commits", {"message": "更新中文扫描报告及机器摘要 [skip ci]",
                                                "tree": tree["sha"], "parents": [parent]})
        try:
            api("PATCH", "/git/refs/heads/main", {"sha": commit["sha"], "force": False})
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
