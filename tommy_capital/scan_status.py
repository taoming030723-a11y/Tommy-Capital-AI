"""Expose startup separately from audited market conclusions; never invent scan results."""
import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .data import SHANGHAI

BEGIN = "<!-- tommy-scan-status:start -->"
END = "<!-- tommy-scan-status:end -->"
LABELS = {"running": "正在扫描，最终报告尚未生成", "complete": "报告已发布",
          "partial": "部分结果已发布，数据缺口保留", "failed": "扫描失败，已发布诊断",
          "publish_failed": "报告发布未完成，请查看运行日志", "cancelled": "本次扫描已取消"}


def strip_banner(markdown):
    if markdown.startswith(BEGIN):
        _, separator, tail = markdown.partition(END)
        if separator:
            return tail.lstrip("\n")
    return markdown


def status_entries(state, markdown):
    """The old report keeps its actual date, counts and final JSON untouched."""
    data_label = "本次报告日线日期" if state.get('report_session') else "上一份已发布报告的数据日"
    data_day = state.get('report_session') or state.get('previous_report_session') or '未取得'
    text = (f"# Tommy Capital 当前扫描状态\n\n"
            f"目标日期：{state['session']}\n\n状态：{LABELS[state['status']]}\n\n"
            f"实际启动：{state['started_at']}\n\n最近更新：{state['updated_at']}\n\n"
            f"{data_label}：{data_day}\n\n"
            f"[查看当前 Actions 运行和进度]({state['run_url']}) · "
            f"[查看最近发布的中文报告](latest-report.md)\n\n"
            "启动状态不代表选股已完成；行情、候选数和完整性以审计后的最终报告为准。\n")
    if state.get('financial_integrity_status') == 'degraded':
        text += "\n**财报数据异常：本次仅发布诊断与已核验部分，不能把榜单缩减解释为全市场机会减少。**\n"
    entries = [{"path": "results/scan-status.json", "mode": "100644", "type": "blob",
                "content": json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)},
               {"path": "results/scan-status.md", "mode": "100644", "type": "blob", "content": text}]
    if markdown:
        original = strip_banner(markdown)
        if state['status'] in ('running', 'publish_failed', 'cancelled'):
            banner = (f"{BEGIN}\n> **{state['session']}：{LABELS[state['status']]}。** "
                      f"[查看本次实际进度]({state['run_url']})。"
                      f"下方保留上一份已发布报告，数据日为{state.get('previous_report_session') or '未取得'}。\n{END}\n\n")
            original = banner + original
        entries.append({"path": "results/latest-report.md", "mode": "100644", "type": "blob", "content": original})
    return entries


def updated_state(action, previous, run_id, repository, phase, now, summary, report=None,
                  publish_outcome=None, cancelled=False):
    """A late finish from an older run cannot overwrite a newer run's status."""
    summary = summary if isinstance(summary, dict) else {}
    if action == 'finish' and (not previous or previous.get('run_id') != run_id):
        return None
    state = dict(previous) if action == 'finish' else {
        "schema": 1, "run_id": run_id, "run_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        "session": now.date().isoformat(), "scan_phase": phase, "started_at": now.isoformat(),
        "previous_report_session": summary.get('session') if isinstance(summary, dict) else None}
    state['updated_at'] = now.isoformat()
    if action == 'start':
        state['status'] = 'running'
    elif cancelled:
        state['status'] = 'cancelled'
    elif publish_outcome == 'success' and isinstance(report, dict) and report.get('status') in ('complete', 'partial', 'failed'):
        if (summary.get('workflow_run_url') != state['run_url'] or summary.get('generated_at') != report.get('generated_at')
                or summary.get('status') != report.get('status')):
            state['status'] = 'publish_failed'
        else:
            state.update(status=report['status'], report_generated_at=report['generated_at'],
                         report_finished_at=report['finished_at'], report_session=report.get('session'),
                         scan_complete=report['scan_complete'])
            guard = report.get('financial_integrity')
            if guard:
                state['financial_integrity_status'] = guard['status']
    else:
        state['status'] = 'publish_failed'
    return state


def publish_status(action, repository, token, run_id, phase, report=None, publish_outcome=None, cancelled=False):
    if not token or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not str(run_id).isdigit():
        raise ValueError("扫描状态缺少GitHub凭据或当前运行身份")
    base = 'https://api.github.com/repos/' + repository
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'}
    def api(method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(base+path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    def read(path, parent):
        try:
            value = api('GET', f'/contents/{path}?ref={parent}')
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        if not value.get('content'):
            value = api('GET', '/git/blobs/' + value['sha'])
        return base64.b64decode(value['content']).decode('utf-8')
    for attempt in range(3):
        parent = api('GET', '/git/ref/heads/main')['object']['sha']
        previous = api('GET', '/git/commits/'+parent)
        state_json, summary_json = read('results/scan-status.json', parent), read('results/latest-summary.json', parent)
        state = updated_state(action, json.loads(state_json) if state_json else None, str(run_id), repository,
            phase, datetime.now(SHANGHAI), json.loads(summary_json) if summary_json else {}, report, publish_outcome, cancelled)
        if state is None:
            return None
        entries = status_entries(state, read('results/latest-report.md', parent) or '')
        tree = api('POST', '/git/trees', {'base_tree': previous['tree']['sha'], 'tree': entries})
        commit = api('POST', '/git/commits', {'tree': tree['sha'], 'parents': [parent],
                                           'message': '更新当前扫描状态 [skip ci]'})
        try:
            api('PATCH', '/git/refs/heads/main', {'sha': commit['sha'], 'force': False})
            return state
        except urllib.error.HTTPError as exc:
            if exc.code not in (409, 422) or attempt == 2:
                raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start', 'finish'))
    args = parser.parse_args()
    report = None
    file = Path('reports/latest/report.json')
    if args.action == 'finish' and file.is_file():
        report = json.loads(file.read_text())
    state = publish_status(args.action, os.environ['REPO_NAME'], os.environ['GH_TOKEN'], os.environ['GITHUB_RUN_ID'],
        os.environ.get('SCAN_PHASE', 'closing'), report, os.environ.get('PUBLISH_OUTCOME'), os.environ.get('JOB_CANCELLED') == 'true')
    print(json.dumps(state, ensure_ascii=False))


if __name__ == '__main__':
    main()
