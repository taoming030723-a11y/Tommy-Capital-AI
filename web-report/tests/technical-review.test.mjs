import test from 'node:test';
import assert from 'node:assert/strict';
import {calculateIndicators,strictDivergences,histogramWaves,frameSnapshot,assessTechnical,REVIEW_VERSION,POLICY_ID,PERIODS} from '../lib/technical-review.mjs';
import {adjustMinuteBars,validateBars,aggregateBars,marketSymbol,validateNativePeriod,latestTechnicalSession} from '../lib/technical-market.mjs';
import {explainTechnical,modelFacts,technicalFingerprint} from '../lib/ai-technical.mjs';
import {reusableValuation,baselineView} from '../lib/valuation-cache.mjs';
import {reusableTechnical} from '../lib/study-policy.mjs';
const day=i=>new Date(Date.UTC(2024,0,1+i)).toISOString().slice(0,10);
function rows(n=300){return Array.from({length:n},(_,i)=>{const p=20+i*.02+Math.sin(i/5);return {date:day(i)+'T00:00:00',open:p,high:p+.4,low:p-.4,close:p,volume:100+i%5};});}
function review(){const input=rows(),frames=Object.fromEntries(PERIODS.map(p=>[p,frameSnapshot(input,p,'frozen_fixture')]))
  return assessTechnical({code:'600089',session:day(299),frames,rawFrames:{monthly:input,weekly:input,daily:input}});}

test('MACD is exactly 2*(DIF-DEA), KDJ/MA warmup is missing rather than zero',()=>{
  const calc=calculateIndicators(rows(80));assert.equal(calc[6].j,null);assert.equal(calc[53].ma[55],null);assert.ok(calc[54].ma[55]>0);
  for(const r of calc)assert.equal(r.histogram,2*(r.dif-r.dea));
});
test('future bars cannot alter prior indicators or prematurely confirm a right pivot',()=>{
  const frozen=rows(100),short=calculateIndicators(frozen.slice(0,70)),long=calculateIndicators(frozen);
  assert.deepEqual(short,long.slice(0,70));
  const signals=strictDivergences(short);for(const s of signals.signals)assert.ok(s.confirmed_at<=frozen[69].date);
});
test('same-sign histogram waves compare actual peaks; decay alone remains unconfirmed',()=>{
  const calc=Array.from({length:30},(_,i)=>({date:day(i),histogram:i<5?0:i<10?5:i<14?-1:i<19?2:-1,atr14:1,high:10,low:9}));
  const result=histogramWaves(calc);const top=result.find(r=>r.direction==='bearish');
  assert.deepEqual(top.histogram_peaks,[5,2]);assert.equal(top.state,'momentum_change');assert.equal(top.confirmed,false);
  calc.slice(14,19).forEach(r=>r.high=12);assert.equal(histogramWaves(calc).find(r=>r.direction==='bearish').state,'suspected');
});
test('a 15-minute signal cannot independently satisfy a six-period buy requirement',()=>{
  const r=review();r.frames['15'].signals=[{direction:'bullish',indicator:'MACD_DIF',quality_passed:true,price_current:20}];
  r.frames.weekly.weekly_ma55.direction='falling';
  const result=assessTechnical({...r,frames:r.frames,rawFrames:{daily:rows(),weekly:rows(),monthly:rows()}});
  assert.equal(result.technical_entry_watch,false);assert.equal(result.conditions.weekly_ma55,false);assert.equal(result.orders_authorized,false);assert.equal(result.t_eligible,false);
});
test('missing any requested period is unavailable, never interpreted as no divergence or a confirmed entry',()=>{
  const r=review();r.frames['30']={status:'unavailable',error:'frozen failure'};
  const result=assessTechnical({...r,frames:r.frames});assert.equal(result.data_complete,false);assert.deepEqual(result.missing_periods,['30']);assert.equal(result.technical_entry_watch,false);
});
test('period aggregation excludes unfinished weeks and months',()=>{
  const input=validateBars(['2026-10-15','2026-10-16','2026-10-19','2026-10-20'].map(date=>({date,open:10,high:11,low:9,close:10,volume:100})));
  assert.equal(aggregateBars(input,'weekly','2026-10-20').at(-1).date,'2026-10-16T00:00:00');assert.equal(aggregateBars(input,'monthly','2026-10-20').length,0);
});
test('focused studies can advance beyond an old full-market report, but never use a forming daily bar',async()=>{
  const original=globalThis.fetch;
  const dates=[];for(let t=Date.parse('2025-01-01');t<=Date.parse('2026-10-21');t+=86400000){const d=new Date(t);if(![0,6].includes(d.getUTCDay())&&!/^2026-10-0[12]/.test(d.toISOString()))dates.push(d.toISOString().slice(0,10));}
  globalThis.fetch=async()=>Response.json({data:{sh000001:{day:dates.map(d=>[d,10,10,11,9,100])}}});
  try{
    assert.equal(await latestTechnicalSession(new Date('2026-10-21T14:00:00+08:00')),'2026-10-20');
    assert.equal(await latestTechnicalSession(new Date('2026-10-21T16:00:00+08:00')),'2026-10-21');
    assert.equal(await latestTechnicalSession(new Date('2026-10-04T16:00:00+08:00')),'2026-09-30');
  }finally{globalThis.fetch=original;}
});
test('minute data enforces the real calendar, lunch gap, cutoff and qfq factors',()=>{
  const session='2026-10-20',daily=validateBars([{date:session,open:10,high:11,low:9,close:10,volume:1}]);
  const minute=['10:30','11:30','14:00','15:00'].map(t=>({date:session+'T'+t+':00',open:20,high:22,low:18,close:20,volume:100}));
  const raw=daily.map(r=>({...r,open:20,high:22,low:18,close:20}));
  const adjusted=adjustMinuteBars(minute,daily,raw,60,session,[session]);assert.equal(adjusted[0].close,10);
  assert.throws(()=>adjustMinuteBars(minute.slice(0,3),daily,raw,60,session,[session]),/15:00/);
  assert.throws(()=>adjustMinuteBars([minute[0],{...minute[1],date:session+'T12:30:00'},...minute.slice(2)],daily,raw,60,session,[session]),/午休|缺根/);
  assert.throws(()=>adjustMinuteBars(minute,daily,[],60,session,[session]),/复权因子/);
});
test('Beijing code routing and independent weekly validation never substitute raw data',()=>{
  assert.equal(marketSymbol('920599'),'bj920599');assert.equal(marketSymbol('920946'),'bj920946');assert.equal(marketSymbol('600089'),'sh600089');
  const input=rows(100),native=aggregateBars(input,'weekly',day(99));
  assert.throws(()=>validateNativePeriod(native,input.map(r=>({...r,close:r.close*2,high:r.high*2,low:r.low*2,open:r.open*2})),'weekly',day(99)),/不一致|完整周期/);
});
test('completed valuation baselines persist across technical days without a financial recheck',()=>{
  const value={status:'complete',price_as_of:'2026-10-20',label:'相对合理'};
  assert.equal(reusableValuation(value),true);const view=baselineView(value,'2026-10-21');assert.equal(view.baseline_as_of,'2026-10-20');assert.equal(view.report_session,'2026-10-21');assert.equal(reusableValuation({status:'unavailable'}),false);
});
test('unchanged completed technical snapshots reuse cache, a new session or force refresh does not',()=>{
  const record={updatedAt:'2026-10-20T16:00:00+08:00',value:{version:REVIEW_VERSION,policy_id:POLICY_ID,session:'2026-10-20',data_complete:true}};
  assert.equal(reusableTechnical(record,'2026-10-20'),true);assert.equal(reusableTechnical(record,'2026-10-21'),false);assert.equal(reusableTechnical(record,'2026-10-20',{force:true}),false);
});
const explanation={summary:'冻结模型输出，仅测试解析。',period_comments:Object.fromEntries(PERIODS.map(p=>[p,'未满足条件应继续等待。'])),risks:['冻结风险'],next_conditions:['等待完整确认']};
test('missing model credentials do not call a model or impersonate a ChatGPT analysis',async()=>{
  const result=await explainTechnical(review(),{fetcher:()=>{throw Error('must not call')}});assert.equal(result.status,'not_configured');
});
test('structured model explanations cannot overwrite program gates and identical facts reuse the response',async()=>{
  const r=review(),original=structuredClone(r);let count=0;
  const fetcher=async(url,options)=>{count++;assert.equal(url,'https://api.openai.com/v1/responses');const body=JSON.parse(options.body);assert.equal(body.store,false);assert.equal(body.text.format.type,'json_schema');assert.equal(body.text.format.strict,true);assert.ok(!JSON.stringify(body).includes('position_cost'));
    return Response.json({id:'fixture-response',status:'completed',output:[{content:[{type:'output_text',text:JSON.stringify(explanation)}]}]});};
  const result=await explainTechnical(r,{apiKey:'fixture-key',fetcher});assert.equal(result.status,'complete');assert.deepEqual(r,original);
  assert.equal((await explainTechnical({...r,checked_at:'2030-01-01'},{apiKey:'fixture-key',fetcher,previous:result})).reused,true);assert.equal(count,1);
  const facts=modelFacts({...r,position_cost:100,shares:1000,note:'private'});assert.equal(facts.position_cost,undefined);assert.equal(facts.shares,undefined);assert.equal(facts.note,undefined);
  assert.equal(await technicalFingerprint(r),await technicalFingerprint({...r,checked_at:'2030'}));
  assert.equal(await technicalFingerprint(r),await technicalFingerprint({...r,frames:Object.fromEntries(Object.entries(r.frames).reverse())}));
});
test('model refusal, incomplete output and bad schema are never completed evaluations',async()=>{
  const r=review();
  assert.equal((await explainTechnical(r,{apiKey:'fixture',fetcher:async()=>Response.json({status:'incomplete'})})).status,'incomplete');
  assert.equal((await explainTechnical(r,{apiKey:'fixture',fetcher:async()=>Response.json({status:'completed',output:[{content:[{type:'refusal'}]}]})})).status,'refused');
  await assert.rejects(explainTechnical(r,{apiKey:'fixture',fetcher:async()=>Response.json({status:'completed',output:[{content:[{type:'output_text',text:'{"entry_watch":true}'}]}]})}),/不匹配/);
});
