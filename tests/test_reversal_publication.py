"""Offline GitHub protocol tests; never publish fixture research to a repository."""
import io
import json

import pytest

from tommy_capital.cli import write_outputs,make_summary,clean
from tommy_capital.publish import publish
from tommy_capital.reversal_research import attach_research
from test_reversal_opportunity import row_and_report


def prepared(tmp_path):
    _,report=row_and_report()
    report.pop('top10_history')
    report.update(errors=[],warnings=[],divergences=[],excluded=[],selection_rule='dual_route_monthly_recovery',
                  minute15_cutoff='2026-10-29T15:00:00',scan_type='after_close')
    write_outputs(tmp_path,report)
    (tmp_path/'summary.json').write_text(json.dumps(clean(make_summary(report))))
    attach_research(tmp_path,state_path=tmp_path/'not-present.json')
    return tmp_path


def git_api(monkeypatch):
    calls=[]
    def urlopen(request,**kwargs):
        method=request.get_method();payload=json.loads(request.data) if request.data else None
        calls.append((method,request.full_url,payload))
        if '/git/ref/' in request.full_url: response={'object':{'sha':'parent'}}
        elif method=='GET' and '/git/commits/' in request.full_url: response={'tree':{'sha':'base-tree'}}
        elif '/git/trees' in request.full_url: response={'sha':'new-tree'}
        elif '/git/commits' in request.full_url: response={'sha':'new-commit'}
        else: response={}
        return io.BytesIO(json.dumps(response).encode())
    monkeypatch.setattr('urllib.request.urlopen',urlopen)
    monkeypatch.delenv('GITHUB_STEP_SUMMARY',raising=False)
    return calls


def test_route_c_report_snapshot_and_events_share_one_publication(tmp_path,monkeypatch):
    root=prepared(tmp_path);calls=git_api(monkeypatch)
    assert publish(root,'offline-fixture-token','fixture/repository')=='new-commit'
    entries=next(payload['tree'] for method,url,payload in calls if url.endswith('/git/trees'))
    paths={e['path'] for e in entries}
    assert 'results/reversal-event-state.json' in paths
    assert any(p.startswith('research/reversal/') for p in paths)
    assert 'results/top10-history.json' not in paths
    human=next(e['content'] for e in entries if e['path']=='results/latest-report.md')
    machine=json.loads(next(e['content'] for e in entries if e['path']=='results/latest-summary.json'))
    assert '多周期右侧反转机会池' in human and machine['candidate_count']==0
    assert machine['reversal_opportunities']['candidate_count']==1


def test_optional_route_c_data_failure_still_allows_original_report_publication(tmp_path,monkeypatch):
    root=prepared(tmp_path);report=json.loads((root/'report.json').read_text())
    report['observations'][0]['technical']['reversal_features']={'status':'unavailable','reason':'源失败'}
    write_outputs(root,report)
    (root/'summary.json').write_text(json.dumps(clean(make_summary(report))))
    attach_research(root,state_path=root/'no-state.json')
    git_api(monkeypatch)
    assert publish(root,'offline-fixture-token','fixture/repository')=='new-commit'


def test_private_or_tampered_route_c_state_never_reaches_github_writes(tmp_path,monkeypatch):
    root=prepared(tmp_path);state=json.loads((root/'reversal-event-state.json').read_text());state['cost']=10
    (root/'reversal-event-state.json').write_text(json.dumps(state))
    calls=git_api(monkeypatch)
    with pytest.raises(ValueError):publish(root,'offline-fixture-token','fixture/repository')
    assert not calls


def test_disabling_optional_research_clears_old_outgoing_artifacts(tmp_path):
    root=prepared(tmp_path);report=json.loads((root/'report.json').read_text())
    policy=report['reversal_opportunities']['policy'];policy['enabled']=False
    report['reversal_policy']=policy
    write_outputs(root,report)
    attach_research(root,state_path=root/'absent.json')
    updated=json.loads((root/'report.json').read_text())
    assert updated['reversal_opportunities']['status']=='disabled'
    assert not (root/'reversal-snapshot.json').exists() and not (root/'reversal-event-state.json').exists()
    assert updated['status']=='partial'
