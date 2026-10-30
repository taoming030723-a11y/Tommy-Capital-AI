"""Hypothetical trajectories test mechanics, never substitute live evidence."""
import copy
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tommy_capital.cli import DEFAULTS, clean, make_summary, write_outputs
from tommy_capital.data import DataError, SHANGHAI
from tommy_capital.technical import indicators, strategic_technical
from tommy_capital.reversal_opportunity import (PARAMETERS, VERSION, load_policy, validate_policy, observe,
    safe_observe, build_pool, attach_pool, evaluate_row, company_check)
from tommy_capital.reversal_research import (features_at, transition_state, freeze_pool, validate_snapshot,
    validate_pool, forward_outcome, attach_research)
from tommy_capital.reversal_reporting import render_pool
from test_core import good_row


def raw(values,index):
    values=np.array(values,dtype=float)
    return pd.DataFrame({'open':values*.995,'close':values,'high':values*1.02,'low':values*.98,'volume':100.},index=index)


def trajectories(rising=False):
    m=raw(np.r_[np.full(60,25),np.linspace(30,10,32),np.linspace(10.1,17,8)],pd.date_range(end='2026-09-30',periods=100,freq='ME'))
    w=raw(np.r_[np.full(240,10 if rising else 25),np.linspace(10,11,50) if rising else np.linspace(30,10,50),np.linspace(11 if rising else 10.3,19,10)],pd.date_range(end='2026-10-23',periods=300,freq='W-FRI'))
    d=raw(np.r_[np.full(540,11),np.linspace(11,16,50),np.linspace(16,15.7,10)],pd.bdate_range(end='2026-10-29',periods=600))
    return d,w,m


def feature(rising=False):
    d,w,m=trajectories(rising)
    return observe(indicators(d),indicators(w),indicators(m),'2026-10-29','2026-10-29',load_policy())


def row_and_report(rising=True):
    row={**good_row(),'announced_at':'2026-08-01','quality_statement_matched':True,'technical':{'reversal_features':feature(rising)},
         'eligible':False,'strategic_eligible':False,'score_inputs_complete':True,'reasons':['未满足原月J路径'],'score':None}
    report={'status':'partial','scan_complete':False,'ranking_complete':False,'session':'2026-10-29','scan_phase':'closing',
            'generated_at':'2026-10-29T15:05:00+08:00','finished_at':'2026-10-29T15:30:00+08:00',
            'scoring_version':'weekly_ma55_macd_50_50_v9','config':dict(DEFAULTS),'rankings':[],'observations':[row],
            'coverage':{'universe':1,'technical_failed':0},'top10_history':{'unchanged':True}}
    return row,report


def test_high_j_repair_is_allowed_and_falling_ma55_is_not_labelled_rising():
    f=feature()
    assert f['monthly']['j']>80 and f['long_decline_repair']
    assert f['weekly']['ma55']['latest_week_above'] and f['weekly']['ma55']['direction']=='falling'
    assert f['stage']==2 and '下降' in f['weekly']['structure_label']
    assert not f['orders_authorized'] and not f['influences_ranking']


def test_standing_above_rising_ma55_and_daily_pullback_is_stage3_not_a_buy():
    row,report=row_and_report()
    f=row['technical']['reversal_features']
    assert f['weekly']['ma55']['direction']=='rising' and f['daily']['consolidation'] and f['stage']==3
    out=evaluate_row(row,report,load_policy())
    assert out['stage']==3 and not out['execution_permission']
    assert out['company']['status']=='pending' and not out['company']['ready']


def test_monthly_new_lows_are_not_repaired_just_because_kdj_improves():
    d,w,m=trajectories()
    m.loc[m.index[-2]:,'low']=3.
    f=observe(indicators(d),indicators(w),indicators(m),'2026-10-29','2026-10-29',load_policy())
    assert not f['long_decline_repair'] and f['stage'] not in (1,2,3,4)


@pytest.mark.parametrize('period', ['daily','weekly','monthly'])
def test_forming_bars_cannot_be_used_as_confirmation(period):
    frames=dict(zip(('daily','weekly','monthly'),trajectories()))
    frame=frames[period].copy()
    frame.loc[pd.Timestamp('2026-10-30') if period!='monthly' else pd.Timestamp('2026-10-31')]=frame.iloc[-1]
    frames[period]=frame
    result=safe_observe(*(indicators(frames[p]) for p in ('daily','weekly','monthly')),'2026-10-29','2026-10-29',load_policy())
    assert result['status']=='unavailable' and result['stage'] is None


@pytest.mark.parametrize('problem',['short_monthly','zero_volume','nan','wrong_macd','wrong_kdj','wrong_parameters'])
def test_invalid_or_missing_data_stays_unknown(problem):
    frames=[indicators(x) for x in trajectories()]
    kwargs={}
    if problem=='short_monthly':frames[2]=frames[2].tail(23)
    if problem=='zero_volume':frames[0].loc[frames[0].index[-1],'volume']=0
    if problem=='nan':frames[0].loc[frames[0].index[-1],'atr14']=np.nan
    if problem=='wrong_macd':frames[1]['macd']*=.5
    if problem=='wrong_kdj':frames[2].loc[frames[2].index[-1],'k']=99
    if problem=='wrong_parameters':kwargs['parameters']={**PARAMETERS,'macd':[10,23,9]}
    result=safe_observe(*frames,'2026-10-29','2026-10-29',load_policy(),**kwargs)
    assert result['status']=='unavailable' and result['label']=='数据不足，无法判断'


def test_short_monthly_ma55_stays_missing_not_fabricated():
    d,w,m=trajectories()
    f=observe(indicators(d),indicators(w),indicators(m.tail(30)),'2026-10-29','2026-10-29',load_policy())
    assert f['monthly']['long_ma55_missing'] and f['monthly']['ma55_distance_pct'] is None


def test_low_support_uses_bars_before_both_confirmation_closes():
    d,w,m=trajectories(True)
    for index in d.index[-2:]: d.loc[index,['open','close','high','low']]=[9.,9.,9.1,8.9]
    f=observe(indicators(d),indicators(w),indicators(m),'2026-10-29','2026-10-29',load_policy())
    assert f['daily']['support_lost'] and f['stage']==5


def test_false_breakout_returns_to_the_old_platform_not_just_below_yesterdays_high():
    d,w,m=trajectories(True)
    d.iloc[-2,d.columns.get_indexer(['open','close','high','low'])]=[17.,17.,17.2,16.8]
    d.iloc[-1,d.columns.get_indexer(['open','close','high','low'])]=[15.,15.,15.2,14.8]
    f=observe(indicators(d),indicators(w),indicators(m),'2026-10-29','2026-10-29',load_policy())
    assert f['daily']['failed_breakout']
    row,report=row_and_report();row['technical']['reversal_features']=f
    pool=build_pool(report)
    assert not pool['top20'] and pool['rows'][0]['status'] in ('risk_watch','invalidated')


def test_financial_data_missing_is_pending_not_deterioration():
    row,report=row_and_report();row.pop('cfo_ttm')
    c=company_check(row,report['session'],report['config'])
    assert 'cfo_ttm' in c['missing'] and not c['ready'] and c['status']=='pending'


def test_expensive_company_is_separate_from_technical_stage_and_cannot_advance():
    row,report=row_and_report();row['pe_ttm']=100
    out=evaluate_row(row,report,load_policy())
    assert out['stage']==3 and out['company']['status']=='blocked' and not out['company']['ready']
    assert 'valuation_expensive' in out['company']['adverse']


def test_review_cannot_self_certify_missing_original_sources_and_pharmacy_is_generic():
    row,report=row_and_report();row['name']='示例连锁药房'
    review={k:True for k in ('business_continuity','debt','goodwill','litigation','operating_improvement','store_productivity','lease_liabilities')}
    review.update(as_of='2026-10-29',sources=[{'verification':'verified','url':'https://www.sse.com.cn/fake-fixture','sha256':'a'*64,'published_at':'2026-10-28'}])
    c=company_check(row,report['session'],report['config'],review=review)
    assert 'review.store_productivity' in c['missing'] and 'review.lease_liabilities' in c['missing']
    row['name']='示例制造公司'
    c=company_check(row,report['session'],report['config'],review=review)
    assert 'review.store_productivity' not in c['missing']


def test_stage4_requires_original_execution_gate_and_fully_evidenced_valuation():
    row,report=row_and_report()
    d,w,m=trajectories(True)
    f=row['technical']['reversal_features']
    row['technical']=strategic_technical(d,pd.Timestamp('2026-10-29').date(),monthly_history=m,weekly_history=w)
    row['technical']['reversal_features']=f
    report['financial_integrity']={'status':'healthy'}
    row['technical']['reversal_features']['daily']['breakout']=True
    row['technical']['reversal_features']['daily'].update(momentum_recovering=True,kd_recovering=True)
    source={'verification':'verified','url':'https://www.sse.com.cn/protocol-fixture','sha256':'a'*64,'published_at':'2026-10-28'}
    row['leading_evidence']={'records':[source]}
    review={k:True for k in ('business_continuity','debt','goodwill','litigation','operating_improvement')}
    review.update(as_of='2026-10-29',sources=[source])
    study={'valuation':{'status':'complete','price_judgment':'低估（假设下）','safety_margin':.3,'model_disagreement':False}}
    result=evaluate_row(row,report,load_policy(),review,study)
    assert result['stage']==4 and not result['execution_permission']
    assert row['eligible'] is False and row['strategic_eligible'] is False
    f['weekly']['ma55']['direction']='falling'
    assert evaluate_row(row,report,load_policy(),review,study)['stage']==3
    f['weekly']['ma55']['direction']='rising';study['valuation']['safety_margin']=.05
    assert evaluate_row(row,report,load_policy(),review,study)['stage']==3


def test_independent_pool_does_not_fill_production_top20_or_change_tracking():
    row,report=row_and_report();before=copy.deepcopy(report)
    pool=attach_pool(report)
    assert pool['candidate_count']==1 and len(pool['top20'])==1
    assert {k:v for k,v in report.items() if k!='reversal_opportunities'}==before
    assert report['rankings']==[] and report['top10_history']==before['top10_history']
    validate_pool(report)
    report['reversal_opportunities']['top20'][0]['stage']=4
    with pytest.raises(ValueError): validate_pool(report)


def test_shared_technical_analysis_with_route_c_preserves_every_original_fact():
    d,w,m=trajectories(True)
    original=strategic_technical(d,pd.Timestamp('2026-10-29').date(),monthly_history=m,weekly_history=w)
    extra=strategic_technical(d,pd.Timestamp('2026-10-29').date(),monthly_history=m,weekly_history=w,opportunity_policy=load_policy())
    assert extra.pop('reversal_features')['status']=='ok'
    assert clean(extra)==clean(original)


def test_optional_feature_failure_keeps_valid_core_analysis(monkeypatch):
    import tommy_capital.reversal_opportunity as module
    def fail(*a,**kw):raise DataError('独立模块模拟数据请求失败')
    monkeypatch.setattr(module,'observe',fail)
    d,w,m=trajectories(True)
    result=strategic_technical(d,pd.Timestamp('2026-10-29').date(),monthly_history=m,weekly_history=w,opportunity_policy=load_policy())
    assert result['daily_bar_date']=='2026-10-29' and result['reversal_features']['status']=='unavailable'


def test_event_refresh_is_deduplicated_and_stage_transition_retains_history():
    row,report=row_and_report();pool=attach_pool(report)
    first=transition_state(pool);again=transition_state(pool,first)
    assert len(first['events'])==len(again['events'])==1
    changed=copy.deepcopy(pool);changed['rows'][0]['stage']=5;changed['rows'][0]['label']='结构失效'
    last=transition_state(changed,again)
    assert len(last['events'])==2 and last['events'][0]==first['events'][0]
    old=copy.deepcopy(pool);old['session']='2026-10-28'
    with pytest.raises(ValueError):transition_state(old,last)


def test_actual_time_snapshot_is_immutable_and_not_backdated():
    row,report=row_and_report();attach_pool(report)
    now=datetime(2026,10,30,17,tzinfo=SHANGHAI)
    snap=freeze_pool(report,now);validate_snapshot(snap)
    assert snap['observed_at']==now.isoformat() and snap['data_session']=='2026-10-29'
    snap['rows'][0]['stage']=4
    with pytest.raises(ValueError):validate_snapshot(snap)


def test_forecast_only_uses_completed_prefix_and_not_future_prices():
    d,w,m=trajectories(True)
    asof='2026-10-20T15:05:00+08:00'
    first=features_at(d,asof,monthly=m,weekly=w)
    d.loc[d.index>pd.Timestamp('2026-10-20'),['open','close','high','low']]*=100
    assert features_at(d,asof,monthly=m,weekly=w)==first
    # At 14:34, today's bar has no role in the formal observation.
    before=features_at(d,'2026-10-29T14:34:00+08:00',monthly=m,weekly=w)
    assert before['daily']['last_bar'][:10]=='2026-10-28'


@pytest.mark.parametrize('bad',[{'macd':[10,23,9]},{'minimum_bars':{'daily':50}},{'breakout_rvol':0.5},{'schema':True}])
def test_invalid_configuration_cannot_silently_change_parameter_basis(bad):
    p=load_policy();p.update(bad)
    with pytest.raises(ValueError):validate_policy(p)


def test_future_financials_are_unknown_and_module_failure_is_local():
    row,report=row_and_report();row['announced_at']='2027-01-01'
    assert 'known_financial_publication' in company_check(row,report['session'],report['config'])['missing']
    report['observations'][0]['technical']['reversal_features']={'status':'unavailable','reason':'源失败'}
    pool=attach_pool(report)
    assert pool['technical_unavailable_count']==1 and not pool['top20'] and report['status']=='partial'
    text='\n'.join(render_pool(pool));assert '未知1只' in text


def test_unknown_and_zero_candidates_are_distinct():
    row,report=row_and_report();report['status']='failed'
    assert attach_pool(report)['status']=='unavailable'
    report['status']='partial';report['observations']=[]
    assert attach_pool(report)['candidate_count']==0 and report['reversal_opportunities']['status']=='partial'
    report['status']='complete'
    assert attach_pool(report)['status']=='complete'
    report['coverage']['unscanned']=10
    assert attach_pool(report)['status']=='partial'


def test_research_outcomes_preserve_t_plus_one_gaps_cost_and_untradeable_cases():
    d,_,_=trajectories()
    day=d.index[-70];calendar=d.index.tolist()
    result=forward_outcome(d,day,5,calendar,d.index[-1])
    assert result['status']=='observed' and result['entry_session']==d.index[-69].date().isoformat()
    missing=d.drop(d.index[-67])
    assert forward_outcome(missing,day,5,calendar,d.index[-1])['status']=='unavailable'
    d.loc[d.index[-69],'high']=d.loc[d.index[-69],'low']
    assert forward_outcome(d,day,5,calendar,d.index[-1])['status']=='unavailable'
    assert forward_outcome(d,d.index[-3],60,calendar,d.index[-1])['status']=='pending'


def test_unknown_day_keeps_old_event_and_condition_disappearance_is_not_fake_invalidation():
    row,report=row_and_report();pool=attach_pool(report);first=transition_state(pool)
    missing=copy.deepcopy(pool);missing['rows']=[{'code':row['code'],'status':'unavailable'}]
    assert transition_state(missing,first)['events']==first['events']
    gone=copy.deepcopy(pool);gone['rows']=[{'code':row['code'],'status':'not_detected','stage':None}]
    cleared=transition_state(gone,first)
    assert len(cleared['events'])==2 and cleared['events'][-1]['stage']==0
    assert transition_state(gone,cleared)['events']==cleared['events']


def test_one_malformed_feature_does_not_discard_other_route_c_candidates():
    row,report=row_and_report();bad=copy.deepcopy(row);bad['code']='600999';bad['technical']['reversal_features']['daily'].pop('breakout')
    report['observations'].append(bad)
    pool=attach_pool(report)
    assert pool['technical_unavailable_count']==1 and pool['candidate_count']==1


def test_red_histogram_shortening_alone_is_never_a_second_launch():
    d,w,m=trajectories(True)
    m=raw(np.full(100,25.),m.index)
    f=safe_observe(indicators(d),indicators(w),indicators(m),'2026-10-29','2026-10-29',load_policy())
    assert f.get('stage') not in (1,2,3,4)


def test_ledger_tampering_and_private_fields_are_rejected():
    from tommy_capital.reversal_research import validate_event_state
    row,report=row_and_report();state=transition_state(attach_pool(report))
    state['events'][0]['stage']=4
    with pytest.raises(ValueError):validate_event_state(state)
    state=transition_state(report['reversal_opportunities']);state['cost']=10
    with pytest.raises(ValueError):validate_event_state(state)


def test_same_day_older_source_cannot_overwrite_new_event_history():
    row,report=row_and_report();pool=attach_pool(report);state=transition_state(pool)
    pool['source_finished_at']='2026-10-29T15:10:00+08:00'
    with pytest.raises(ValueError):transition_state(pool,state)


def test_frozen_real_case_uses_complete_bars_and_does_not_force_a_buy():
    root=Path(__file__).parent/'fixtures/reversal';manifest=json.loads((root/'manifest.json').read_text())
    s=next(s for s in manifest['samples'] if s.get('code')=='603883')
    d=pd.read_csv(root/s['file'],index_col='date',parse_dates=True)
    context={p:pd.read_csv(root/s[p]['file'],index_col='date',parse_dates=True) for p in ('monthly','weekly')}
    f=features_at(d,'2026-10-30T14:34:00+08:00',**context)
    assert f['stage']==2 and f['monthly']['last_bar'].startswith('2026-09-30')
    assert f['weekly']['last_bar'].startswith('2026-10-23') and f['daily']['last_bar'].startswith('2026-10-29')
    assert f['weekly']['ma55']['direction']=='falling' and f['weekly']['ma55']['latest_week_above']


def test_all_frozen_real_sources_have_matching_hashes_and_quotes():
    import hashlib
    root=Path(__file__).parent/'fixtures/reversal';manifest=json.loads((root/'manifest.json').read_text())
    for s in manifest['samples']:
        p=root/s['file'];assert hashlib.sha256(p.read_bytes()).hexdigest()==s['sha256']
        d=pd.read_csv(p,index_col='date',parse_dates=True)
        assert abs(d.close.iloc[-1]-s['quote_price'])<=.011
        for period in ('weekly','monthly'):
            item=s[period];assert hashlib.sha256((root/item['file']).read_bytes()).hexdigest()==item['sha256']


def test_existing_confirmed_weekly_macd_risk_is_reused_not_redefined():
    row,report=row_and_report()
    row['technical']['timeframes']={'weekly':{'signals':[{'indicator':'MACD_DIF','direction':'bearish','quality_passed':True}]}}
    pool=attach_pool(report)
    assert pool['candidate_count']==0 and pool['rows'][0]['status']=='risk_watch'
    assert any('原引擎' in x for x in pool['rows'][0]['confirmation_missing'])


def test_changed_research_policy_cannot_reuse_old_feature_stages():
    row,report=row_and_report()
    policy=load_policy();policy['minimum_decline_pct']=25
    pool=attach_pool(report,policy)
    assert pool['technical_unavailable_count']==1 and pool['candidate_count']==0
    assert report['status']=='partial' and report['rankings']==[]


def test_kd_descriptions_exactly_reuse_original_classifier():
    from tommy_capital.kd_state import from_frame
    f=feature()
    for period,raw_frame in zip(('daily','weekly','monthly'),trajectories()):
        calculated=indicators(raw_frame)
        assert f[period]['kd_state']==from_frame(calculated,period,'production_calculated_qfq',calculated.index[-1].isoformat())
