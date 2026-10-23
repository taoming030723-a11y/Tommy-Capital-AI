"""Frozen hypothetical trajectories are tests, never published market evidence."""
import copy
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tommy_capital.data import SHANGHAI
from tommy_capital.kd_state import classify_kd, combine_kd, from_frame, load_policy, observe_acquired
from tommy_capital.kd_research import freeze_kd, validate_kd_snapshot, evaluate_kd
from tommy_capital.research import freeze_snapshot
from tommy_capital.technical import indicators
from tommy_capital.cli import clean, write_outputs
from tommy_capital.focused_reporting import render_technical_reviews
from test_watchlist_preferences import report_with_watch

ROOT = Path(__file__).resolve().parents[1]


def trajectory(last, count=60):
    days = pd.bdate_range('2026-01-01', periods=count)
    pairs = [(50., 50.)]*(count-len(last))+last
    return [{"date": t.isoformat(), "k": k, "d": d, "j": 3*k-2*d} for t, (k,d) in zip(days,pairs)]


CASES = [([(16.,19.),(15.,18.),(12.,16.)], 'low_weak'),
         ([(11.,14.5),(10.,14.),(12.,13.3)], 'early_turn'),
         ([(13.,16.),(14.,15.),(18.,16.)], 'low_golden_cross'),
         ([(43.,49.),(45.,48.),(52.,49.3)], 'golden_cross'),
         ([(53.,49.),(55.,50.),(60.,53.)], 'bullish_continuation'),
         ([(68.,58.),(70.,60.),(68.,62.7)], 'momentum_weakening'),
         ([(62.,58.),(60.,58.),(55.,57.)], 'bearish_turn'),
         ([(41.,40.),(40.,39.8),(41.,40.2)], 'early_turn')]


@pytest.mark.parametrize('last,expected', CASES)
def test_trajectory_states_are_not_just_current_k_above_d(last, expected):
    rows=trajectory(last);result=classify_kd(rows,'60','offline-frozen',rows[-1]['date'])
    assert result['state']==expected and result['status']=='ok'
    assert len(result['history'])==6 and result['previous']==result['history'][-2]
    assert not result['influences_ranking'] and not result['trade_confirmation']


def test_cross_does_not_repeat_and_a_tiny_cross_is_not_a_trade_confirmation():
    rows=trajectory([(13.,16.),(14.,15.),(18.,16.)]);cross=classify_kd(rows,'60','test',rows[-1]['date'])
    rows.append({'date':'2026-04-01T00:00:00','k':20.,'d':17.3,'j':25.4})
    result=classify_kd(rows,'60','test',rows[-1]['date'])
    assert cross['state']=='low_golden_cross' and result['state']=='bullish_continuation'
    tiny=trajectory([(40.,40.),(40.,40.),(40.1,40.02)])
    result=classify_kd(tiny,'60','test',tiny[-1]['date'])
    assert result['weak_cross'] and not result['trade_confirmation']


@pytest.mark.parametrize('problem',['short','missing','wrong_j','duplicate','unsorted'])
def test_missing_and_invalid_history_is_unknown_not_no_signal(problem):
    rows=trajectory([(13.,16.),(14.,15.),(18.,16.)])
    if problem=='short':rows=rows[-2:]
    elif problem=='missing':rows[-1]['k']=None
    elif problem=='wrong_j':rows[-1]['j']=999.
    elif problem=='duplicate':rows[-2]['date']=rows[-1]['date']
    elif problem=='unsorted':rows[0],rows[1]=rows[1],rows[0]
    result=classify_kd(rows,'60','test','2026-12-31')
    assert result['status']=='unavailable' and result['history']==[]


def test_future_rows_cannot_change_a_completed_observation():
    rows=trajectory([(13.,16.),(14.,15.),(18.,16.)]);cutoff=rows[-1]['date']
    before=classify_kd(rows,'60','test',cutoff)
    rows.extend([{'date':'2026-04-01T00:00:00','k':None,'d':None,'j':None}])
    assert classify_kd(rows,'60','test',cutoff)==before


def test_correlated_periods_are_one_group_and_j_is_not_an_independent_vote():
    rows=trajectory([(53.,49.),(55.,50.),(60.,53.)]);bull=classify_kd(rows,'60','test',rows[-1]['date'])
    result=combine_kd({'120':{**bull,'period':'120'},'60':bull,'15':{**bull,'period':'15'}})
    assert not result['trade_confirmation'] and result['groups'][3]['periods']==['120','60']
    assert result['groups'][3]['correlated'] and not result['data_complete']
    assert 'score' not in result and 'signal_count' not in result
    bearish=trajectory([(62.,58.),(60.,58.),(55.,57.)]);bear=classify_kd(bearish,'weekly','test',bearish[-1]['date'])
    assert combine_kd({'weekly':bear,'60':bull})['conflict']


def test_policy_is_shared_and_cannot_lower_history_floors():
    policy=load_policy();assert policy==json.loads((ROOT/'web-report/lib/kd-state-policy.json').read_text())
    policy['minimum_bars']['60']=10
    with pytest.raises(ValueError,match='不得降低'):classify_kd([], '60', 'test','2026-10-22',policy)


def test_python_and_javascript_classification_and_grouping_match():
    if not shutil.which('node'):pytest.skip('Node required for parity')
    rows=[trajectory(last) for last,_ in CASES]
    script="""import {classifyKD,combineKD} from './web-report/lib/kd-state.mjs';
    let input='';for await(const c of process.stdin)input+=c;
    const results=JSON.parse(input).map(rows=>classifyKD(rows,'60','frozen',rows.at(-1).date));
    console.log(JSON.stringify({results,combined:combineKD({'60':results[4],'weekly':results[6]})}));"""
    out=json.loads(subprocess.run(['node','--input-type=module','-e',script],cwd=ROOT,input=json.dumps(rows),text=True,capture_output=True,check=True).stdout)
    expected=[classify_kd(r,'60','frozen',r[-1]['date']) for r in rows]
    assert out['results']==expected
    assert out['combined']==combine_kd({'60':expected[4],'weekly':expected[6]})


def test_actual_indicator_prefix_replay_never_changes_past_kd_states():
    rng=np.random.default_rng(23);close=20+np.cumsum(rng.normal(0,.2,285))
    raw=pd.DataFrame({'open':close,'high':close+.5,'low':close-.5,'close':close,'volume':1000.},index=pd.bdate_range('2025-01-01',periods=285))
    calculated=indicators(raw)
    for size in range(250,286):
        prefix=from_frame(raw.iloc[:size],'daily','frozen',raw.index[size-1].isoformat())
        expected=from_frame(calculated,'daily','frozen',raw.index[size-1].isoformat())
        assert prefix==expected
    raw.loc[raw.index[270]:,'close']=200.
    assert from_frame(raw,'daily','frozen',raw.index[259].isoformat())==from_frame(calculated,'daily','frozen',raw.index[259].isoformat())


def focused_report():
    report=report_with_watch();stock=report['top10_tracking']['rows'][0]
    report['ranking_complete']=False
    report.setdefault('score_weights',{'fundamental':50,'technical':50})
    rows=trajectory([(53.,49.),(55.,50.),(60.,53.)])
    for row,day in zip(rows,pd.bdate_range(end=report['session'],periods=len(rows))):row['date']=day.isoformat()
    frame=classify_kd(rows,'60','offline-frozen',rows[-1]['date'])
    report['technical_reviews']={'schema':1,'public':True,'rows':[{'code':stock['code'],'name':stock['name'],'session':report['session'],'kd_monitor':combine_kd({'60':frame})}]}
    return report


def test_frozen_kd_cohort_preserves_scores_history_and_ignores_and_uses_real_decision_time():
    report=focused_report();before=copy.deepcopy(report);original=freeze_snapshot(report)
    now=datetime(2026,10,23,13,40,tzinfo=SHANGHAI);snapshot=freeze_kd(report,now)
    assert snapshot['decision_at']==now.isoformat() and snapshot['data_session']==report['session']
    assert report==before and freeze_snapshot(report)==original
    validate_kd_snapshot(snapshot)
    altered=copy.deepcopy(snapshot);altered['stocks'][0]['code']='999999'
    with pytest.raises(ValueError,match='改写'):validate_kd_snapshot(altered)
    report['top10_tracking']['preferences']['ignored_codes']=[snapshot['stocks'][0]['code']]
    assert freeze_kd(report,now)['stocks']==[]


def test_kd_cohort_returns_and_missing_stay_separate_without_training_weights():
    report=focused_report();now=datetime(2026,10,23,13,40,tzinfo=SHANGHAI);snapshot=freeze_kd(report,now)
    days=pd.bdate_range('2026-10-20',periods=80)
    frame=pd.DataFrame({'日期':days,'开盘':10.,'收盘':11.,'最高':12.,'最低':9.})
    result=evaluate_kd([snapshot],days,pd.Timestamp('2026-10-30').date(),lambda *args:frame)
    assert len(result['checks'])==4 and result['checks'][0]['entry_session']=='2026-10-26'
    assert result['checks'][0]['status']=='observed' and result['checks'][-1]['status']=='pending'
    assert not result['automatic_weight_training'] and result['validation_status']=='unvalidated_out_of_sample'
    missing=evaluate_kd([snapshot],days,pd.Timestamp('2026-10-30').date(),lambda *args:frame.iloc[:1])
    assert missing['checks'][0]['status']=='missing' and any(b['missing'] for b in missing['buckets'])


def test_report_contains_independent_kd_table_and_original_score_frozen_fields(tmp_path):
    report=focused_report();before=freeze_snapshot(report)
    lines='\n'.join(render_technical_reviews(report['technical_reviews']))
    assert '多周期K/D状态观察' in lines and 'K−D' in lines
    write_outputs(tmp_path,report)
    after=json.loads((tmp_path/'report.json').read_text())
    assert freeze_snapshot(after)==before and after['rankings']==clean(report['rankings'])


def test_week_month_observer_excludes_forming_periods_and_keeps_minute_gaps_unknown():
    days=pd.bdate_range(end='2026-10-26',periods=900);close=np.linspace(10.,20.,len(days))
    raw=pd.DataFrame({'open':close,'high':close+.5,'low':close-.5,'close':close,'volume':1000.},index=days)
    acquired={'daily':{'status':'ok','frame':raw,'source':'frozen','cutoff':'2026-10-22T23:59:59'}}
    result=observe_acquired(acquired,pd.Timestamp('2026-10-22').date())
    assert result['frames']['monthly']['last_bar'].startswith('2026-09-30')
    assert result['frames']['weekly']['last_bar'].startswith('2026-10-16')
    assert result['frames']['daily']['last_bar'].startswith('2026-10-22')
    assert result['frames']['120']['status']=='unavailable' and not result['data_complete']


def test_kd_results_do_not_mix_production_versions_or_policies():
    report=focused_report();snapshot=freeze_kd(report,datetime(2026,10,23,13,40,tzinfo=SHANGHAI))
    other=copy.deepcopy(snapshot);other['production_version']='different_production_model'
    from tommy_capital.kd_research import digest
    other['sha256']=digest({k:v for k,v in other.items() if k!='sha256'})
    days=pd.bdate_range('2026-10-20',periods=80);prices=pd.DataFrame({'日期':days,'开盘':10.,'收盘':11.,'最高':12.,'最低':9.})
    result=evaluate_kd([snapshot,other],days,pd.Timestamp('2026-10-30').date(),lambda *args:prices)
    assert len({b['production_version'] for b in result['buckets']})==2
    assert all(b['requested']==1 for b in result['buckets'])
    assert all(b['difference_to_same_pool_pct']==pytest.approx(0.) for b in result['buckets'] if b['observed'])
