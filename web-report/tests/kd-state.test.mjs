import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {classifyKD,combineKD} from '../lib/kd-state.mjs';
import {aggregate120Bars} from '../lib/technical-market.mjs';
import {assessTechnical,PERIODS} from '../lib/technical-review.mjs';
const rows=last=>Array.from({length:60},(_,i)=>{const [k,d]=i<60-last.length?[50,50]:last[i-(60-last.length)];return {date:new Date(Date.UTC(2026,0,i+1)).toISOString().slice(0,19),k,d,j:3*k-2*d};});
test('six screenshot states use slopes and history, not only current K>D',()=>{
 for(const [last,state] of [[[[16,19],[15,18],[12,16]],'low_weak'],[[[11,14.5],[10,14],[12,13.3]],'early_turn'],[[[13,16],[14,15],[18,16]],'low_golden_cross'],[[[53,49],[55,50],[60,53]],'bullish_continuation'],[[[68,58],[70,60],[68,62.7]],'momentum_weakening'],[[[62,58],[60,58],[55,57]],'bearish_turn']]){
  const h=rows(last),v=classifyKD(h,'60','frozen',h.at(-1).date);assert.equal(v.state,state);assert.equal(v.trade_confirmation,false);assert.equal(v.history.length,6);
 }
});
test('unknown, forming bars and derived J cannot become extra confirmation',()=>{
 const h=rows([[13,16],[14,15],[18,16]]),cutoff=h.at(-1).date,before=classifyKD(h,'60','test',cutoff);
 assert.deepEqual(classifyKD([...h,{date:'2026-05-01T10:30:00',k:null,d:null}],'60','test',cutoff),before);
 assert.equal(classifyKD(h.slice(-2),'60','test',cutoff).status,'unavailable');
 const invalid=structuredClone(h);invalid.at(-1).j=999;assert.equal(classifyKD(invalid,'60','test',cutoff).status,'unavailable');
 const combined=combineKD({'120':{...before,period:'120'},'60':before});assert.equal(combined.groups.filter(g=>g.periods.includes('60')).length,1);assert.equal(combined.trade_confirmation,false);assert.equal(combined.data_complete,false);
});
const minute=(date,clock,n)=>({date:date+'T'+clock,open:n,close:n+1,high:n+2,low:n-1,volume:100});
function halves(){return ['2026-10-09','2026-10-12'].flatMap(d=>['10:30:00','11:30:00','14:00:00','15:00:00'].map((t,i)=>minute(d,t,10+i)));}
test('120m groups exactly two real 60m bars per half-session and respects holidays',()=>{
 const result=aggregate120Bars(halves(),'2026-10-12',['2026-10-09','2026-10-12']);assert.equal(result.length,4);assert.equal(result[0].date,'2026-10-09T11:30:00');assert.equal(result[0].open,10);assert.equal(result[0].close,12);assert.equal(result[0].volume,200);assert.equal(result[1].date,'2026-10-09T15:00:00');
 assert.throws(()=>aggregate120Bars(halves().filter((_,i)=>i!==5),'2026-10-12',['2026-10-09','2026-10-12']),/缺少完整/);
 assert.throws(()=>aggregate120Bars(halves().slice(0,-1),'2026-10-12',['2026-10-09','2026-10-12']),/缺少完整|最新完整/);
 assert.throws(()=>aggregate120Bars(halves(),'2026-10-12',['2026-10-12']),/非交易日/);
 assert.equal(aggregate120Bars(halves().slice(1),'2026-10-12',['2026-10-09','2026-10-12']).length,3);
});
test('120/60 added observations cannot change existing six-period entry gates',()=>{
 const empty=Object.fromEntries(PERIODS.map(p=>[p,{status:'unavailable'}])),a=assessTechnical({code:'600000',session:'2026-10-22',frames:empty});
 const h=rows([[13,16],[14,15],[18,16]]),kd=classifyKD(h,'120','test',h.at(-1).date),b=assessTechnical({code:'600000',session:'2026-10-22',frames:{...empty,'120':{status:'kd_observation_only',kd_state:kd}}});
 assert.deepEqual(a.conditions,b.conditions);assert.equal(a.decision,b.decision);assert.deepEqual(PERIODS,['monthly','weekly','daily','60','30','15']);assert.equal(b.kd_monitor.trade_confirmation,false);
});
test('private observation UI keeps real scope and shows trajectory instead of inventing old states',()=>{
 const ui=readFileSync(new URL('../app/kd-observations.tsx',import.meta.url),'utf8');assert.match(ui,/scope\.filter/);assert.match(ui,/f\.history\.map/);assert.match(ui,/旧评估/);assert.match(ui,/刷新全部技术面后生成/);
});
