"""Manage user watch preferences without changing market facts or research ranks."""
import argparse
import base64
import copy
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime

from .reporting import render_tracking_section
from .tracking import (PREFERENCES_PATH, ZONE, apply_preferences, closing_record,
                       digest, validate_preferences)


def changed_preferences(preferences, action, code):
    value = validate_preferences(preferences)
    action = {"忽略": "ignore", "恢复": "restore"}.get(action, action)
    if action not in ("ignore", "restore") or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code.strip()):
        raise ValueError("请选择忽略或恢复，并填写六位股票代码（保留前导0）")
    codes = set(value["ignored_codes"])
    if action == "ignore":
        codes.add(code.strip())
    else:
        codes.discard(code.strip())
    return validate_preferences({"schema": 1, "ignored_codes": sorted(codes)})


def replace_tracking_section(markdown, tracking, config):
    start = markdown.find("## 综合前10累计至少3次")
    if start < 0:
        raise ValueError("原报告没有可核对的观察表，保留原文")
    end = markdown.find("\n## ", start + 1)
    section = "\n".join(render_tracking_section(tracking, config)).rstrip() + "\n\n"
    return markdown[:start] + section + (markdown[end + 1:] if end >= 0 else "")


def refreshed_entries(summary, markdown, archive_summary, archive_markdown, preferences, now, actor):
    """Only a separately timestamped user-view overlay changes, not the scan result."""
    if (not isinstance(summary, dict) or closing_record(summary) is None or
            summary != archive_summary or not isinstance(summary.get("top10_tracking"), dict)):
        return []
    view = apply_preferences(summary["top10_tracking"], preferences)
    desired = replace_tracking_section(markdown, view, summary.get("config"))
    archived = replace_tracking_section(archive_markdown, view, summary.get("config"))
    if (summary["top10_tracking"].get("preferences_sha256") == digest(preferences) and
            desired == markdown and archived == archive_markdown):
        return []
    view.update(preferences_updated_at=now.astimezone(ZONE).isoformat(), preferences_updated_by=actor,
                preferences_update_scope="仅用户观察表显示偏好；行情、排名、评分、扫描时间及原始入榜次数保持")
    updated = copy.deepcopy(summary)
    updated["top10_tracking"] = view
    assert all(updated[k] == v for k, v in summary.items() if k != "top10_tracking")
    text = json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False)
    day = summary["session"]
    return [{"path": path, "mode": "100644", "type": "blob", "content": content} for path, content in (
        ("results/latest-summary.json", text), (f"results/{day}/closing-summary.json", text),
        ("results/latest-report.md", desired), (f"results/{day}/closing-report.md", archived))]


def manage(repository, token, action=None, code=None, now=None, actor=""):
    if not token or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("缺少观察名单管理的仓库身份")
    now = now or datetime.now(ZONE)
    base = "https://api.github.com/repos/" + repository
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "Content-Type": "application/json"}

    def api(method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def read(path, parent):
        try:
            result = api("GET", "/contents/" + path + "?ref=" + parent)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        if not result.get("content"):
            result = api("GET", "/git/blobs/" + result["sha"])
        return base64.b64decode(result["content"]).decode("utf-8")

    for attempt in range(3):
        parent = api("GET", "/git/ref/heads/main")["object"]["sha"]
        previous = api("GET", "/git/commits/" + parent)
        original = read(PREFERENCES_PATH, parent)
        preferences = validate_preferences(json.loads(original)) if original else {"schema": 1, "ignored_codes": []}
        if action:
            preferences = changed_preferences(preferences, action, code)
        entries = []
        if not original or validate_preferences(json.loads(original)) != preferences:
            entries.append({"path": PREFERENCES_PATH, "mode": "100644", "type": "blob",
                            "content": json.dumps(preferences, ensure_ascii=False, indent=2) + "\n"})
        raw_summary, markdown = read("results/latest-summary.json", parent), read("results/latest-report.md", parent)
        summary = json.loads(raw_summary) if raw_summary else {}
        day = summary.get("session", "")
        refreshed = []
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day) and markdown:
            raw_archive, archive_md = read(f"results/{day}/closing-summary.json", parent), read(f"results/{day}/closing-report.md", parent)
            if raw_archive and archive_md:
                refreshed = refreshed_entries(summary, markdown, json.loads(raw_archive), archive_md,
                                               preferences, now, actor)
                entries.extend(refreshed)
        if not entries:
            return {"changed": False, "refreshed": False, "preferences": preferences}
        tree = api("POST", "/git/trees", {"base_tree": previous["tree"]["sha"], "tree": entries})
        commit = api("POST", "/git/commits", {"tree": tree["sha"], "parents": [parent],
            "message": "更新用户观察名单偏好与显示 [skip ci]"})
        try:
            api("PATCH", "/git/refs/heads/main", {"sha": commit["sha"], "force": False})
            return {"changed": True, "refreshed": bool(refreshed), "preferences": preferences,
                    "commit": commit["sha"], "session": day}
        except urllib.error.HTTPError as exc:
            if exc.code not in (409, 422) or attempt == 2:
                raise
            # Re-read the new report and preferences; never overwrite a concurrent scan or user change.
    raise RuntimeError("观察名单管理提交未完成")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", default=os.environ.get("WATCHLIST_ACTION") or None)
    parser.add_argument("--code", default=os.environ.get("WATCHLIST_CODE"))
    args = parser.parse_args()
    result = manage(os.environ["REPO_NAME"], os.environ["GH_TOKEN"], args.action, args.code,
                    actor=os.environ.get("GITHUB_ACTOR", ""))
    print(json.dumps(result, ensure_ascii=False))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            codes = "、".join(result["preferences"]["ignored_codes"]) or "无"
            state = "当前观察表已刷新" if result["refreshed"] else "设置已核对；下一次收盘扫描继续读取"
            output.write(f"# 观察名单管理\n\n{state}。\n\n当前忽略代码：{codes}\n\n"
                         f"[返回中文报告](https://github.com/{os.environ['REPO_NAME']}/blob/main/results/latest-report.md)\n\n"
                         "忽略只隐藏持续观察行；恢复保留原累计次数，不影响扫描和综合前20。\n")


if __name__ == "__main__":
    main()
