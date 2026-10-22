// Frozen arithmetic fixtures, never injected as real prices or account records.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {emptyJournal,applyJournalAction,settlementPreview,lastMonday,portfolioMetrics,riskPositionSize,hypotheticalValuation,reportFreshness,holdingDecision,opportunityComparison,equityObservation,observedDrawdown} from '../lib/decision-support.mjs';

const day=s=>new Date(s+'T16:00:00+08:00');
test('old reports remain visibly dated when a new scan fails and on weekends',()=>{
 const value=reportFreshness({session:'2026-10-20'},{session:'2026-10-22',status:'publish_failed'},day('2026-10-22'));
 assert.equal(value.recommendations_current,false);assert.match(value.message,/2026-10-20/);assert.match(value.label,/未成功发布/);
 assert.equal(reportFreshness({session:'2026-10-22'},{},day('2026-10-24')).recommendations_current,false);
});
test('portfolio cannot infer share counts, cash, current prices or historical drawdown',()=>{
 const holdings=[{code:'600000',shares:100,cost:8},{code:'600001'}],reviews={'600000':{current_quote:{price:10,session:'2026-10-22'}}};
 const p=portfolioMetrics(holdings,reviews,{investable_cash:1000},'2026-10-22');
 assert.equal(p.equity,null);assert.equal(p.rows[0].weight,null);assert.equal(p.rows[0].unrealized_profit,200);assert.equal(p.portfolio_drawdown,null);
 const complete=portfolioMetrics(holdings.slice(0,1),reviews,{investable_cash:1000},'2026-10-22');assert.equal(complete.equity,2000);assert.equal(complete.rows[0].weight,.5);
 assert.equal(portfolioMetrics(holdings.slice(0,1),reviews,{investable_cash:1000},'2026-10-23').equity,null);
});
test('risk budget obeys a supplied trading unit and does not produce orders',()=>{
 const size=riskPositionSize(100000,100,92,{risk_per_trade:.005,max_position_weight:.2},1);
 assert.equal(size.shares,62);assert.equal(size.planned_loss,496);assert.equal(riskPositionSize(null,100,92,{}),null);
});
test('manual normalized-profit scenarios distinguish margin from upside and never become verified value',()=>{
 const v=hypotheticalValuation({price:'7',bear_profit:'60',base_profit:'100',bull_profit:'140',fair_pe:'10',shares:'100',current_profit:60,years:3});
 assert.equal(v.base,10);assert.ok(Math.abs(v.safety_margin-.3)<1e-9);assert.equal(v.status,'user_assumptions_unverified');assert.equal(v.influences_ranking,false);
 assert.equal(hypotheticalValuation({price:7,bear_profit:60,base_profit:100,bull_profit:140,fair_pe:10,shares:100,current_profit:-60}).required_cagr,null);
 assert.throws(()=>hypotheticalValuation({price:7,bear_profit:100,base_profit:60,bull_profit:140,fair_pe:10,shares:100}));
});
test('thesis and valuation edits preserve original state and are user hypotheses',()=>{
 const old=emptyJournal(),next=applyJournalAction(old,{action:'save_thesis',code:'600000',thesis:'研究假设',confirm_conditions:'财报验证',invalidate_conditions:'失效条件',next_review:'2026-11-01'},day('2026-10-22'));
 assert.deepEqual(old.theses,{});assert.equal(next.revision,1);assert.match(next.theses['600000'].evidence_status,/尚未核验/);
 assert.throws(()=>applyJournalAction(next,{action:'save_thesis',code:'600000',thesis:'',confirm_conditions:'x',invalidate_conditions:'y'}));
});
test('realized ledger uses broker net cash, deduplicates and never includes unrealized P/L',()=>{
 let s=applyJournalAction(emptyJournal(),{action:'save_settings',ledger_started_on:'2026-10-01'},day('2026-10-22'));
 const add={action:'add_trade',code:'600000',date:'2026-10-20',realized_net_cny:1000,statement_ref:'fixture-1'};
 s=applyJournalAction(s,add,day('2026-10-22'));assert.throws(()=>applyJournalAction(s,add,day('2026-10-22')),/重复/);
 s=applyJournalAction(s,{...add,date:'2026-10-21',realized_net_cny:-400,statement_ref:'fixture-2'},day('2026-10-22'));
 assert.equal(lastMonday(2026,9),'2026-10-26');assert.equal(settlementPreview(s,day('2026-10-22')).due,false);
 const p=settlementPreview(s,day('2026-10-26'));assert.equal(p.recorded_net_cny,600);assert.equal(p.proposed_allocation_cny,60);assert.equal(p.destination,'sister_watch');
 assert.throws(()=>applyJournalAction(s,{action:'settle'},day('2026-10-26')),/完整/);
 const settled=applyJournalAction(s,{action:'settle',confirm_complete:true},day('2026-10-26'));
 assert.equal(settled.settlements.length,1);assert.equal(settlementPreview(settled,day('2026-10-26')).due,false);
 assert.throws(()=>applyJournalAction(settled,{action:'settle',confirm_complete:true},day('2026-10-26')),/已结算/);
 assert.throws(()=>applyJournalAction(settled,{action:'void_trade',id:settled.trades[0].id},day('2026-10-26')),/已结算/);
 assert.throws(()=>applyJournalAction(settled,{...add,statement_ref:'fixture-3'},day('2026-10-26')),/已结算/);
});
test('loss periods allocate zero and new periods do not recount old trades',()=>{
 let s=applyJournalAction(emptyJournal(),{action:'save_settings',ledger_started_on:'2026-10-01'},day('2026-10-22'));
 s=applyJournalAction(s,{action:'add_trade',code:'600000',date:'2026-10-20',realized_net_cny:-100,statement_ref:'fixture-loss'},day('2026-10-22'));
 s=applyJournalAction(s,{action:'settle',confirm_complete:true},day('2026-10-26'));assert.equal(s.settlements[0].proposed_allocation_cny,0);
 s=applyJournalAction(s,{action:'add_trade',code:'600000',date:'2026-11-10',realized_net_cny:1000,statement_ref:'fixture-nov'},day('2026-11-10'));
 const p=settlementPreview(s,day('2026-11-30'));assert.equal(p.recorded_net_cny,1000);assert.equal(p.destination,'savings');assert.equal(p.period_start,'2026-10-27');
});
test('private journal route requires real visitor identity, origin and a compare-and-swap',async()=>{
 const source=await readFile(new URL('../app/api/investment-journal/route.ts',import.meta.url),'utf8');
 assert.match(source,/getChatGPTUser/);assert.match(source,/if\(!user\)/);assert.match(source,/headers.get\('origin'\)/);assert.match(source,/eq\(reportStudies.payload,old.payload\)/);assert.match(source,/onConflictDoNothing/);assert.doesNotMatch(source,/console\.log/);
});
test('a 15-minute top alone cannot trigger reduction and missing larger frames stay unknown',()=>{
 const study={session:'2026-10-22',current_quote:{price:10,session:'2026-10-22'},frames:{daily:{status:'ok',price:10,averages_qfq:{13:9},structure:{ma20_rising:true},signals:[]},'60':{status:'ok',signals:[]},'15':{status:'ok',signals:[{indicator:'MACD_DIF',direction:'bearish',quality_passed:true}]}}};
 assert.equal(holdingDecision(study,{base:7},'2026-10-22').partial_reduction_scenario,null);
 assert.equal(holdingDecision(undefined,null).data_complete,false);
 assert.equal(holdingDecision(study,null,'2026-10-23').data_complete,false);
 study.frames.daily.signals=[{indicator:'MACD_DIF',direction:'bearish',quality_passed:true}];study.risk_history={rows:[{close:6},{close:10}]};
 assert.deepEqual(holdingDecision(study,{base:7},'2026-10-22').partial_reduction_scenario,[1/3,.5]);
});
test('opportunity comparison applies incremental costs only to switching and rejects mixed dates',()=>{
 const reviews={a:{current_quote:{price:10,session:'2026-10-22'}},b:{current_quote:{price:10,session:'2026-10-22'}}},values={a:{base:15,bear:8},b:{base:15,bear:8}};
 const c=opportunityComparison('a','b',reviews,values,.01);assert.equal(c.holding.reward_risk,2.5);assert.ok(c.candidate.reward_risk<2.5);
 reviews.b.current_quote.session='2026-10-21';assert.equal(opportunityComparison('a','b',reviews,values).candidate,null);
});
test('observed equity adjusts verified cash flows, starts today and cannot infer a past maximum',()=>{
 const first=equityObservation([],10000,[],day('2026-10-22'));assert.equal(observedDrawdown([first]),null);
 const flow={id:'fixture-cash',at:day('2026-10-23').toISOString(),amount:1000};
 const second=equityObservation([first],11000,[flow],day('2026-10-23'));assert.equal(second.nav,1);
 const third=equityObservation([first,second],9900,[],day('2026-10-26'));assert.ok(Math.abs(observedDrawdown([first,second,third]).maximum_drawdown+.1)<1e-9);
 assert.throws(()=>equityObservation([first,second],12000,[flow],day('2026-10-26')),/唯一编号/);
 assert.throws(()=>applyJournalAction(emptyJournal(),{action:'record_equity',equity_cny:10000}),/入出金完整/);
});
