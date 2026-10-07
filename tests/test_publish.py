"""Publication guards: preserve report dates and publish both views together."""
import io
import json
import urllib.error

import pytest

from tommy_capital.publish import publish


def files(directory, summary_date="2026-10-07T13:25:30+08:00", report_date=None):
    report_date = report_date or summary_date
    report = {"generated_at": report_date, "status": "partial", "scan_complete": False,
              "scan_type": "closed_session", "session": "2026-09-30", "rankings": [],
              "observations": [], "divergences": [], "errors": ["测试网络不可用"], "coverage": {}}
    summary = {"generated_at": summary_date, "status": "partial", "candidate_count": 0}
    (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")


def test_mixed_scan_dates_are_never_published(tmp_path, monkeypatch):
    files(tmp_path, report_date="2026-10-08T09:50:00+08:00")
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('must not write mixed-date reports'))
    with pytest.raises(ValueError, match="不匹配"):
        publish(tmp_path, 'test-token', 'owner/repo')


def test_partial_human_and_machine_reports_share_one_commit(tmp_path, monkeypatch):
    files(tmp_path)
    calls=[]
    def respond(request, **kwargs):
        payload=json.loads(request.data) if request.data else None
        calls.append((request.method,request.full_url,payload))
        if request.method=='GET' and '/git/ref/' in request.full_url:
            data={'object':{'sha':'parent'}}
        elif request.method=='GET':
            data={'tree':{'sha':'base-tree'}}
        elif '/git/trees' in request.full_url:
            data={'sha':'new-tree'}
        elif '/git/commits' in request.full_url:
            data={'sha':'new-commit'}
        else:
            data={'object':{'sha':'new-commit'}}
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr('urllib.request.urlopen',respond)
    summary_path=tmp_path/'actions-summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY',str(summary_path))
    assert publish(tmp_path,'test-token','owner/repo')=='new-commit'
    trees=[body for method,url,body in calls if method=='POST' and url.endswith('/git/trees')]
    assert len(trees)==1
    by_path={entry['path']:entry['content'] for entry in trees[0]['tree']}
    assert set(by_path)=={'results/latest-report.md','results/latest-summary.json'}
    assert json.loads(by_path['results/latest-summary.json'])['generated_at']=='2026-10-07T13:25:30+08:00'
    assert '部分完成' in by_path['results/latest-report.md'] and 'scan_complete=false' in by_path['results/latest-report.md']
    assert '不是10月8日开盘结果' in by_path['results/latest-report.md']
    assert summary_path.read_text()==by_path['results/latest-report.md']
    assert calls[-1][2]=={'sha':'new-commit','force':False}


def test_concurrent_push_is_preserved_by_rebuilding_from_new_head(tmp_path, monkeypatch):
    files(tmp_path)
    head_count=0
    parents=[]
    def respond(request, **kwargs):
        nonlocal head_count
        payload=json.loads(request.data) if request.data else None
        if request.method=='GET' and '/git/ref/' in request.full_url:
            head_count+=1
            data={'object':{'sha':f'head-{head_count}'}}
        elif request.method=='GET':
            data={'tree':{'sha':f'base-{head_count}'}}
        elif '/git/trees' in request.full_url:
            data={'sha':f'tree-{head_count}'}
        elif request.method=='POST':
            parents.append(payload['parents'])
            data={'sha':f'commit-{head_count}'}
        elif head_count==1:
            raise urllib.error.HTTPError(request.full_url,422,'not a fast forward',{},None)
        else:
            data={'object':{'sha':f'commit-{head_count}'}}
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr('urllib.request.urlopen',respond)
    assert publish(tmp_path,'test-token','owner/repo')=='commit-2'
    assert parents==[['head-1'],['head-2']]
