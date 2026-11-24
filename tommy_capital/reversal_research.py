"""Route C event ledger, actual-time snapshots and reproducible price studies.

Historical price studies are retrospective and explicitly lack point-in-time
financial/universe membership. They are not validated strategy backtests.
"""
import argparse
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .data import SHANGHAI, DataError, Provider, completed_session
from .reversal_opportunity import (VERSION, PARAMETERS, digest, load_policy, safe_observe, build_pool, attach_pool)
from .technical import aggregate, indicators
from .backtest import price_window_return

HORIZONS=(5,10,20,60)


def features_at(daily, as_of, policy=None, monthly=None, weekly=None):
    """No indicator sees a bar later than the supplied completed-session cutoff."""
    now=pd.Timestamp(as_of)
    if now.tzinfo is None:
        raise ValueError('研究时点必须有时区')
    now=now.tz_convert(SHANGHAI)
    cutoff=now.normalize().tz_localize(None)
    if (now.hour,now.minute)<(15,5): cutoff-=pd.Timedelta(days=1)
    selected=daily[daily.index.normalize()<=cutoff]
    if selected.empty: return {'status':'unavailable','reason':'没有当时已完成的日线'}
    d=indicators(selected)
    w=indicators(weekly[weekly.index<=cutoff] if weekly is not None else aggregate(selected,'W-FRI',cutoff))
    m=indicators(monthly[monthly.index<=cutoff] if monthly is not None else aggregate(selected,'ME',cutoff))
    return safe_observe(d,w,m,selected.index[-1].date(),cutoff,policy or load_policy(),parameters=PARAMETERS)


def transition_state(pool, previous=None):
    """A repeated stage in the same observed episode is not a new alarm."""
    if pool.get('status') in ('unavailable','disabled'):
        return copy.deepcopy(previous) if previous is not None else {'schema':1,'version':VERSION,'stocks':{},'events':[]}
    result=copy.deepcopy(previous) if previous is not None else {'schema':1,'version':VERSION,'stocks':{},'events':[]}
    if result.get('schema')!=1 or result.get('version')!=VERSION:
        raise ValueError('Route C事件历史版本不一致，不覆盖旧记录')
    if result.get('last_session','')>pool['session']:
        raise ValueError('历史研究不能覆盖较新的Route C事件状态')
    if result.get('source_finished_at','')>pool.get('source_finished_at',''):
        raise ValueError('旧扫描不能覆盖同日较新Route C状态')
    for row in pool['rows']:
        code=row['code'];old=result['stocks'].get(code)
        if row.get('status')=='unavailable': continue
        cleared=row.get('status')=='not_detected' and old and old['stage'] in (1,2,3,4)
        if row.get('stage') not in (1,2,3,4,5) and not cleared: continue
        if row['stage']==5 and old is None: continue
        f=row.get('features');episode=f['episode'] if f else old['episode']
        stage=0 if cleared else row['stage']
        signature=digest({'code':code,'episode':episode,'policy':pool['policy_sha256'],'stage':stage})
        first=not old or old['signature']!=signature
        sequence=old['sequence']+1 if old and first else old['sequence'] if old else 1
        first_seen=pool['checked_at'] if first else old['first_observed_at']
        stock={'signature':signature,'episode':episode,'stage':stage,'sequence':sequence,
               'first_observed_at':first_seen,'last_observed_at':pool['checked_at'],'last_session':pool['session']}
        result['stocks'][code]=stock
        if first:
            event={'code':code,'episode':episode,'stage':stage,'sequence':sequence,'policy_sha256':pool['policy_sha256'],
                   'first_observed_at':first_seen,'data_session':pool['session'],'label':'条件不再满足（不等同结构失效）' if cleared else row['label'],
                   'feature_sha256':digest(f) if f else None,'reasons':row.get('discovery_reasons',[]),'orders_authorized':False}
            event['id']=digest(event)
            result['events'].append(event)
    result['last_session']=pool['session']
    result['source_finished_at']=pool.get('source_finished_at')
    validate_event_state(result)
    return result


def validate_event_state(state):
    if state.get('schema')!=1 or state.get('version')!=VERSION: raise ValueError('Route C事件版本不一致')
    events=state.get('events',[])
    if len({e.get('id') for e in events})!=len(events): raise ValueError('Route C事件重复')
    for e in events:
        if e.get('id')!=digest({k:v for k,v in e.items() if k!='id'}) or e.get('orders_authorized') is not False:
            raise ValueError('Route C历史事件哈希或权限被改写')
    from .investment_research import validate_public_research
    validate_public_research(state)


def freeze_pool(report, now=None):
    now=now or datetime.now(SHANGHAI)
    if now.tzinfo is None: raise ValueError('冻结时间必须有时区')
    pool=report['reversal_opportunities']
    if pool.get('status') in ('unavailable','disabled') or report.get('scan_phase')!='closing': return None
    if now.date().isoformat()<report['session']: raise ValueError('冻结决策不得来自未来数据')
    for field in ('generated_at','finished_at'):
        if datetime.fromisoformat(report[field])>now: raise ValueError('Route C不能提前冻结尚未结束的扫描')
    body={'schema':1,'kind':'actual_time_prospective_research_snapshot','version':VERSION,
          'observed_at':now.isoformat(),'data_session':report['session'],'source_generated_at':report['generated_at'],
          'source_run_url':report.get('workflow_run_url'),'source_commit':report.get('source_commit'),
          'policy_sha256':pool['policy_sha256'],'production_version':report['scoring_version'],
          'scope':'verified_quoted_securities_only_not_complete_point_in_time_universe',
          'production_ranking_unchanged':True,'orders_authorized':False,
          'rows':[{k:r[k] for k in ('code','name','stage','price','company','confirmation_missing')} for r in pool['top20']]}
    body['sha256']=digest(body)
    body['repository_path']=f"research/reversal/{now:%Y-%m-%d}/{body['sha256']}.json"
    return body


def validate_snapshot(snapshot):
    payload={k:v for k,v in snapshot.items() if k not in ('sha256','repository_path')}
    if digest(payload)!=snapshot.get('sha256') or snapshot.get('version')!=VERSION:
        raise ValueError('Route C冻结快照已被修改')
    now=datetime.fromisoformat(snapshot['observed_at'])
    if now.tzinfo is None or snapshot['data_session']>now.astimezone(SHANGHAI).date().isoformat():
        raise ValueError('Route C快照时间不合法')
    if datetime.fromisoformat(snapshot['source_generated_at'])>now:
        raise ValueError('Route C使用未来源扫描')
    if snapshot['repository_path']!=f"research/reversal/{now:%Y-%m-%d}/{snapshot['sha256']}.json":
        raise ValueError('Route C快照归档路径不一致')
    if len(snapshot['rows'])>20 or len({r['code'] for r in snapshot['rows']})!=len(snapshot['rows']):
        raise ValueError('Route C快照重复或超过20只')


def validate_pool(report):
    pool=report.get('reversal_opportunities')
    if pool is None: return
    if pool.get('version')!=VERSION or pool.get('influences_ranking') is not False or pool.get('orders_authorized') is not False:
        raise ValueError('Route C不能修改原排名或授予下单权限')
    if pool.get('status')=='unavailable':
        if pool.get('top20') or pool.get('candidate_count')!=0: raise ValueError('Route C未知状态不能伪造名单')
        return
    expected=build_pool(report,pool['policy'],pool['review_inputs'],pool['valuation_assumptions'],pool['valuation_policy'])
    if {k:v for k,v in pool.items() if k!='checked_at'}!={k:v for k,v in expected.items() if k!='checked_at'}:
        raise ValueError('Route C名单/阶段/独立排序与原特征不一致')
    for r in pool['rows']:
        f=r.get('features')
        if f is None: continue
        if f['parameters']!=PARAMETERS or f['data_session']!=report['session']:
            raise ValueError('Route C数据日期/参数不一致')
        for name, minimum in [('monthly',24),('weekly',60),('daily',250)]:
            if f[name]['bar_count']<minimum or f[name]['last_bar'][:10]>f['complete_through']:
                raise ValueError('Route C含形成中K线或降低历史长度')
        if f['daily']['last_bar'][:10]!=report['session']:
            raise ValueError('Route C日线时效未核验')
        if r['stage']==4 and (not r['company']['ready'] or r['execution_permission'] is not False):
            raise ValueError('Route C执行条件/权限不合法')
    from .investment_research import validate_public_research
    validate_public_research(pool)


def attach_research(directory, state_path='results/reversal-event-state.json', now=None):
    from .cli import write_outputs,make_summary,clean
    root=Path(directory);report=json.loads((root/'report.json').read_text())
    protected={k:copy.deepcopy(report.get(k)) for k in ('rankings','observations','coverage','config','top10_history','top10_tracking','scoring_version','status','scan_complete','ranking_complete')}
    try:
        attach_pool(report,report.get('reversal_policy') or (report.get('reversal_opportunities') or {}).get('policy'))
        validate_pool(report)
        if report['reversal_opportunities'].get('status')=='disabled':
            for name in ('reversal-snapshot.json','reversal-event-state.json'):
                (root/name).unlink(missing_ok=True)
            report['reversal_research']={'version':VERSION,'status':'disabled'}
            assert protected=={k:report.get(k) for k in protected}
            write_outputs(root,report)
            (root/'summary.json').write_text(json.dumps(clean(make_summary(report)),ensure_ascii=False,indent=2,allow_nan=False))
            return report['reversal_opportunities']
        if report['reversal_opportunities'].get('status')=='unavailable':
            raise DataError(report['reversal_opportunities'].get('reason','Route C不可用'))
        file=Path(state_path);previous=json.loads(file.read_text()) if file.exists() else None
        state=transition_state(report['reversal_opportunities'],previous)
        snapshot=freeze_pool(report,now)
        if snapshot:
            validate_snapshot(snapshot)
            (root/'reversal-snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2,allow_nan=False))
        else:
            (root/'reversal-snapshot.json').unlink(missing_ok=True)
        (root/'reversal-event-state.json').write_text(json.dumps(state,ensure_ascii=False,indent=2,allow_nan=False))
        report['reversal_research']={'version':VERSION,'events':len(state['events']),
            'new_events':len(state['events'])-len((previous or {}).get('events',[])),
            'snapshot_path':snapshot['repository_path'] if snapshot else None,'validation_status':'unvalidated_out_of_sample'}
    except Exception as exc:
        from .reversal_opportunity import unavailable
        report['reversal_opportunities']={**unavailable(str(exc)),'top20':[],'rows':[],'candidate_count':0}
        report['reversal_research']={'version':VERSION,'status':'unavailable','reason':str(exc)}
        for name in ('reversal-snapshot.json','reversal-event-state.json'):
            (root/name).unlink(missing_ok=True)
    assert protected=={k:report.get(k) for k in protected}
    write_outputs(root,report)
    (root/'summary.json').write_text(json.dumps(clean(make_summary(report)),ensure_ascii=False,indent=2,allow_nan=False))
    return report['reversal_opportunities']


def forward_outcome(daily, signal_day, horizon, calendar, as_of, cost_bps=20):
    """T+1 price observation, no assumption that a limit/illiquid bar is tradable."""
    if not 0<=cost_bps<=100 or horizon not in HORIZONS: raise ValueError('研究成本/窗口无效')
    days=sorted(set(pd.Timestamp(x).normalize() for x in calendar))
    following=[x for x in days if x>pd.Timestamp(signal_day)]
    item={'horizon':horizon,'status':'pending','assumed_cost_bps_per_side':cost_bps,
          'execution_feasibility':'unknown_not_actual_trade','returns_are_hypothetical':True}
    if len(following)<horizon or following[horizon-1]>pd.Timestamp(as_of): return item
    window=following[:horizon]
    item.update(entry_session=window[0].date().isoformat(),exit_session=window[-1].date().isoformat())
    try:
        selected=daily.reindex(window)
        if selected[['open','close','high','low','volume']].isna().any().any() or (selected.volume<=0).any():
            raise DataError('停牌/交易日缺失，不跳过或填补')
        if selected.iloc[0].high==selected.iloc[0].low: raise DataError('单价K线，未证明可成交')
        raw=selected.reset_index(names='日期').rename(columns={'open':'开盘','close':'收盘','high':'最高','low':'最低'})
        result=price_window_return(raw,[d.date() for d in window])
        entry=float(selected.open.iloc[0]);exit=float(selected.close.iloc[-1]);fee=cost_bps/10000
        item.update(status='observed',price_observation=result,
            hypothetical_net_return_pct=(exit*(1-fee)/(entry*(1+fee))-1)*100,
            close_max_drawdown_pct=_close_drawdown([entry,*selected.close.astype(float)]),
            adverse_low_vs_entry_pct=(float(selected.low.min())/entry-1)*100)
    except (ValueError,KeyError,DataError) as exc:
        item.update(status='unavailable',reason=str(exc))
    return item


def _close_drawdown(values):
    peak=values[0];draw=0.
    for value in values:
        peak=max(peak,value);draw=min(draw,value/peak-1)
    return draw*100


def run_price_study(manifest_file, out, decision_stride=21):
    """Fixed calendar sampling; outcomes never choose a signal or its parameters."""
    manifest_file=Path(manifest_file);manifest=json.loads(manifest_file.read_text())
    policy=load_policy()
    calendar_file=manifest_file.parent/'calendar.csv'
    if not calendar_file.exists(): raise ValueError('必须有真实交易日历，不用工作日猜测')
    if hashlib.sha256(calendar_file.read_bytes()).hexdigest()!=manifest.get('calendar_sha256'):
        raise ValueError('真实交易日历哈希不一致')
    calendar=pd.to_datetime(pd.read_csv(calendar_file)['trade_date']).tolist()
    output=[];cases=[];suppressed=0;decision_checks=[]
    for sample in manifest['samples']:
        if sample.get('status')=='unavailable': continue
        path=manifest_file.parent/sample['file']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=sample['sha256']: raise ValueError('冻结真实样本SHA256不一致')
        daily=pd.read_csv(path,index_col='date',parse_dates=True)
        last_selected={}
        # Full monthly warmup precedes all research signals. The 5/10/20/60
        # evaluation windows may be pending; never drop them from denominators.
        context={}
        for period in ('monthly','weekly'):
            item=sample.get(period,{})
            if item.get('file'):
                p=manifest_file.parent/item['file']
                if hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']: raise ValueError('周/月真实样本哈希不符')
                context[period]=pd.read_csv(p,index_col='date',parse_dates=True)
        dates=daily.index[250::decision_stride]
        for day in dates:
            f=features_at(daily,day.isoformat()+'T15:05:00+08:00',policy=policy,**context)
            decision_checks.append({'code':sample['code'],'anchor_session':day.date().isoformat(),'status':f.get('status'),
                                    'stage':f.get('stage'),'reason':f.get('reason')})
            if f.get('status')!='ok': continue
            technical_candidate=f['stage'] in (1,2,3,4) and not f['monthly_chase_risk'] and not any(f['daily'][k] for k in ('chase_risk','supply_disagreement','failed_breakout'))
            choices={'simple_weekly_ma55':f['weekly']['ma55']['latest_week_above'],
                'route_c_technical_pool':technical_candidate,
                'monthly_weekly_daily_confirmation':technical_candidate and f['stage']==3 and
                    (f['daily']['breakout'] or f['daily']['retest']) and f['daily']['momentum_recovering'] and f['daily']['kd_recovering']}
            position=daily.index.get_loc(day)
            for variant,eligible in choices.items():
                if not eligible: continue
                if position-last_selected.get(variant,-10000)<max(HORIZONS):
                    suppressed+=1;continue
                last_selected[variant]=position
                outcomes=[forward_outcome(daily,day,h,calendar,daily.index[-1]) for h in HORIZONS]
                output.append({'code':sample['code'],'name':sample['name'],'anchor_session':day.date().isoformat(),'variant':variant,
                    'stage':f['stage'],'kind':'retrospective_price_only_replay_not_historical_live_signal','outcomes':outcomes})
            # Ex-post descriptions are separated from all selection predicates.
            future=forward_outcome(daily,day,60,calendar,daily.index[-1])
            if future['status']=='observed':
                ret=future['hypothetical_net_return_pct'];dd=future['close_max_drawdown_pct']
                label='successful_price_recovery' if ret>=10 and dd>-15 else 'failed_price_recovery' if ret<=-10 or dd<=-20 else 'sideways' if abs(ret)<5 and dd>-10 else 'mixed'
                cases.append({'code':sample['code'],'anchor_session':day.date().isoformat(),'ex_post_label':label,'stage_at_anchor':f['stage'],'net_60_pct':ret,'close_drawdown_60_pct':dd,'failed_breakout_at_anchor':f['daily']['failed_breakout']})
    result={'schema':1,'version':VERSION,'policy':policy,'policy_sha256':digest(policy),
        'algorithm_sha256':hashlib.sha256(Path(__file__).with_name('reversal_opportunity.py').read_bytes()).hexdigest(),
        'generated_at':datetime.now(SHANGHAI).isoformat(),
        'kind':'retrospective_small_survivor_sample_price_study','validation_status':'unvalidated_out_of_sample',
        'production_ab_comparison':{'status':'unavailable','reason':'缺少历史全市场成分/退市股与当时已披露财报、估值证据；不能用今天财报回填A/B'},
        'lookahead_protection':'每个信号仅对当时完整K线前缀重新计算；未来价格只用于独立的事后窗口观察',
        'limitations':['当前前复权历史不是历史实时留存锚点','仅当前存续小样本，存在幸存者偏差','不模拟实际成交，涨跌停/流动性及成本模型不能保证可执行','小样本与重叠市场环境不足以证明增量价值'],
        'source_manifest_sha256':hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
        'suppressed_overlapping_signals':suppressed,'decision_checks':decision_checks,
        'unknown_decision_count':sum(r['status']!='ok' for r in decision_checks),'records':output,'ex_post_cases':cases,
        'production_weights_changed':False,'parameters_trained':False}
    summaries=[]
    for variant in ('simple_weekly_ma55','route_c_technical_pool','monthly_weekly_daily_confirmation'):
        rows=[r for r in output if r['variant']==variant]
        for horizon in HORIZONS:
            outcomes=[o for r in rows for o in r['outcomes'] if o['horizon']==horizon]
            observed=[o for o in outcomes if o['status']=='observed']
            summaries.append({'variant':variant,'horizon':horizon,'signals':len(rows),
                'observed':len(observed),'pending':sum(o['status']=='pending' for o in outcomes),
                'unavailable':sum(o['status']=='unavailable' for o in outcomes),
                'mean_hypothetical_net_pct':sum(o['hypothetical_net_return_pct'] for o in observed)/len(observed) if observed else None,
                'mean_close_max_drawdown_pct':sum(o['close_max_drawdown_pct'] for o in observed)/len(observed) if observed else None})
    result['descriptive_summaries']=summaries
    target=Path(out);target.mkdir(exist_ok=True,parents=True)
    (target/'price-study.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    return result


def main():
    parser=argparse.ArgumentParser(description='独立Route C研究；不修改生产评分或下单')
    parser.add_argument('--attach',help='只对本次扫描完整JSON附加Route C研究状态/快照')
    parser.add_argument('--state',default='results/reversal-event-state.json')
    parser.add_argument('--study-manifest',help='已冻结真实OHLC CSV和来源清单')
    parser.add_argument('--out',default='reports/reversal-study')
    args=parser.parse_args()
    if args.attach:
        pool=attach_research(args.attach,args.state)
        print(json.dumps({k:pool.get(k) for k in ('version','status','candidate_count','technical_unavailable_count')},ensure_ascii=False))
    elif args.study_manifest:
        result=run_price_study(args.study_manifest,args.out)
        print(json.dumps({'records':len(result['records']),'cases':len(result['ex_post_cases']),'validation_status':result['validation_status']}))
    else: parser.error('请选择--attach或--study-manifest')


if __name__=='__main__': main()
