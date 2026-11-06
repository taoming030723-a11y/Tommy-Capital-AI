"""Reproduce the frozen real sample, optionally attach it to a genuine report.

Does not publish, request market data, rescore A/B or fabricate full coverage.
"""
import argparse
import copy
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

import pandas as pd

from tommy_capital.reversal_research import features_at, attach_research, validate_pool, run_price_study
from tommy_capital.reversal_opportunity import VERSION, load_policy, digest
from tommy_capital.cli import write_outputs,make_summary,clean
from tommy_capital.scoring import ranked_top20,balanced_score
from tommy_capital.technical import strategic_technical
from tommy_capital.cli import DEFAULTS


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',default='tests/fixtures/reversal/manifest.json')
    parser.add_argument('--baseline-report',help='可选：原始真实全量收盘report.json，保持原排名/财务事实')
    parser.add_argument('--out',default='reports/reversal-validation')
    args=parser.parse_args();root=Path(args.manifest).parent;manifest=json.loads(Path(args.manifest).read_text())
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    findings=[];sample_features={};timing_frames=[];started=time.perf_counter()
    for s in manifest['samples']:
        if not s.get('file'):continue
        raw=(root/s['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==s['sha256']
        d=pd.read_csv(root/s['file'],index_col='date',parse_dates=True)
        context={}
        for p in ('monthly','weekly'):
            item=s.get(p,{})
            if item.get('file'):
                assert hashlib.sha256((root/item['file']).read_bytes()).hexdigest()==item['sha256']
                context[p]=pd.read_csv(root/item['file'],index_col='date',parse_dates=True)
        case=[]
        for clock in ('14:34','15:05'):
            asof=s['last']+'T'+clock+':00+08:00'
            f=features_at(d,asof,**context)
            record={'as_of':asof,'status':f.get('status'),'stage':f.get('stage'),'reason':f.get('reason')}
            if f.get('status')=='ok':
                record.update(monthly={k:f['monthly'][k] for k in ('last_bar','k','d','j','dif','dea','ma55_distance_pct')},
                              weekly_ma55={k:f['weekly']['ma55'][k] for k in ('ma55_qfq','direction','slope_3w_atr','above_count','consecutive_above','latest_week_above')},
                              daily={k:f['daily'][k] for k in ('last_bar','consolidation','breakout','retest','failed_breakout','support_qfq','resistance_qfq')})
            case.append(record)
            if clock=='15:05':
                f['sources']={'daily':s['source'],'weekly':s.get('weekly',{}).get('file','daily_aggregate'),
                              'monthly':s.get('monthly',{}).get('file','daily_aggregate')}
                sample_features[s['code']]=f
        findings.append({'code':s['code'],'name':s['name'],'source_sha256':s['sha256'],'checks':case})
        timing_frames.append((d,{p:f[f.index<=pd.Timestamp(s['last'])] for p,f in context.items()},pd.Timestamp(s['last']).date()))
    elapsed=time.perf_counter()-started
    validation={'version':VERSION,'kind':'frozen_real_sample_validation_not_production_scan','policy_sha256':digest(load_policy()),
        'source_manifest_sha256':hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest(),'samples':findings,
        'sample_size':len(findings),'twelve_prefix_observations_seconds':elapsed,'core_provider_requests_added':0,
        'out_of_sample_validated':False,'production_report_updated':False}
    timings={}
    for name,policy in [('core',None),('core_plus_route_c',load_policy())]:
        start=time.perf_counter()
        for _ in range(3):
            for d,context,session in timing_frames:
                result=strategic_technical(d,session,config=DEFAULTS,monthly_history=context.get('monthly'),
                    weekly_history=context.get('weekly'),execution_focus=False,opportunity_policy=policy)
        timings[name+'_seconds']=time.perf_counter()-start
    validation['cpu_comparison']={**timings,'repetitions':3,'stocks_per_repetition':len(timing_frames),
        'kind':'local_cpu_sample_only_not_full_market_latency_guarantee','additional_provider_requests':0}
    if args.baseline_report:
        report=json.loads(Path(args.baseline_report).read_text())
        original=copy.deepcopy(report)
        for row in report['rankings']+report['observations']:
            if row['code'] in sample_features:row['technical']['reversal_features']=sample_features[row['code']]
        # The only changes inside original cards are the explicitly separate
        # optional features. Strip them to verify all market/score facts.
        stripped=copy.deepcopy(report)
        for row in stripped['rankings']+stripped['observations']:row['technical'].pop('reversal_features',None)
        assert stripped==original
        original_top=copy.deepcopy(ranked_top20(original['rankings']))
        write_outputs(out,report)
        (out/'summary.json').write_text(json.dumps(clean(make_summary(report)),ensure_ascii=False,indent=2))
        pool=attach_research(out,state_path=out/'absent-prior-state.json')
        enriched=json.loads((out/'report.json').read_text());validate_pool(enriched)
        assert [(r['code'],r['score']) for r in ranked_top20(enriched['rankings'])]==[(r['code'],r['score']) for r in original_top]
        assert enriched['top10_history']==original['top10_history'] and enriched['coverage']==original['coverage']
        validation['actual_report_comparison']={'source_run_url':report['workflow_run_url'],'data_session':report['session'],
            'original_technical_verified':len(report['rankings'])+len(report['observations']),
            'route_c_features_observed':len(sample_features),'route_c_candidate_count':pool['candidate_count'],
            'route_c_unknown_count':pool['technical_unavailable_count'],'production_top20_and_scores_unchanged':True,
            'coverage_and_top10_history_unchanged':True,'not_full_market_route_c_validation':True,
            'sample_ab':[{k:r.get(k) for k in ('code','name','eligible','reasons')} for r in original['rankings']+original['observations'] if r['code'] in sample_features]}
    (out/'validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2,allow_nan=False))
    study=run_price_study(args.manifest,out)
    print(json.dumps({'real_samples':len(findings),'observations_seconds':round(elapsed,3),
        'study_records':len(study['records']),'unknown_decisions':study['unknown_decision_count'],
        'post_hoc_cases':dict(Counter(x['ex_post_label'] for x in study['ex_post_cases'])),'validated_excess_returns':False},ensure_ascii=False))


if __name__=='__main__':main()
