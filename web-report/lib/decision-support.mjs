// Private research arithmetic. No model calls, ranking changes or trading actions.
const finite=x=>x!==null&&x!==undefined&&x!==''&&typeof x!=='boolean'&&Number.isFinite(Number(x))?Number(x):null;
const validDay=x=>typeof x==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(x)&&new Date(x+'T00:00:00Z').toISOString().slice(0,10)===x;
const code=x=>{if(!/^\d{6}$/.test(x||''))throw Error('需六位股票代码');return x;};
export const localDay=(now=new Date())=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Singapore',year:'numeric',month:'2-digit',day:'2-digit'}).format(now);
export function reportFreshness(report,status,now=new Date()){
  const today=localDay(now),reported=report.session,expected=status?.session;
  const fresh=reported===today,failed=expected>reported&&['failed','publish_failed','cancelled'].includes(status?.status);
  return {checked_on:today,data_session:reported,expected_session:expected||null,is_same_calendar_day:fresh,recommendations_current:fresh,
    label:fresh?'当日已核验收盘研究':failed?'新扫描未成功发布，以下是历史报告':'历史已核验报告',
    message:fresh?'按报告自身覆盖与缺失状态阅读':`以下数据日为${reported}，不能视为${today}的新推荐。${failed?'最近扫描未通过发布；请查看实际运行及审计原因。':''}`};
}
export function emptyJournal(){return {schema:1,revision:0,settings:{investable_cash:null,risk_per_trade:.005,max_position_weight:.2,ledger_started_on:null,october2026_destination:'sister_watch'},theses:{},valuations:{},trades:[],settlements:[],equity_points:[]};}
const text=(x,max=6000)=>typeof x==='string'&&x.trim()&&x.length<=max?x.trim():(()=>{throw Error('请输入有效文本，长度超出或内容为空');})();
export function lastMonday(year,month){const d=new Date(Date.UTC(year,month+1,0));d.setUTCDate(d.getUTCDate()-((d.getUTCDay()+6)%7));return d.toISOString().slice(0,10);}
export function settlementPreview(state,now=new Date()){
  const today=localDay(now),[year,month]=today.split('-').map(Number);let end=lastMonday(year,month-1);
  if(end>today)end=lastMonday(month===1?year-1:year,month===1?11:month-2);
  const previous=state.settlements.map(s=>s.period_end).sort().at(-1);
  const start=previous?new Date(Date.parse(previous+'T00:00:00Z')+86400000).toISOString().slice(0,10):state.settings.ledger_started_on,settled=new Set(state.settlements.flatMap(s=>s.trade_ids));
  const rows=state.trades.filter(t=>!t.voided&&!settled.has(t.id)&&start&&t.date>=start&&t.date<=end);
  const net=rows.reduce((n,t)=>n+t.realized_net_cny,0),due=!!start&&start<=end&&!state.settlements.some(s=>s.period_end===end);
  return {due,period_start:start,period_end:end,trade_ids:rows.map(t=>t.id),recorded_net_cny:net,proposed_allocation_cny:Math.max(0,net)*.1,
    destination:end.startsWith('2026-10')?state.settings.october2026_destination:'savings',
    basis:'只计已录入、已核对的券商已实现净盈亏；浮盈不计，亏损不提取；确认完整后才锁定结算，不执行转账'};
}
export function applyJournalAction(input,action,now=new Date()){
  const state=structuredClone(input),today=localDay(now);
  if(action.action==='save_settings'){
    for(const [key,allowZero] of [['investable_cash',true],['risk_per_trade',false],['max_position_weight',false]]){
      if(!(key in action))continue;const n=finite(action[key]);
      if(n===null){if(key==='investable_cash'){state.settings[key]=null;continue;}throw Error('风险参数需明确数值');}
      if(n<0||(!allowZero&&n===0)||(key!=='investable_cash'&&n>1))throw Error('风险参数范围无效');state.settings[key]=n;
    }
    if(action.ledger_started_on){if(!validDay(action.ledger_started_on)||action.ledger_started_on>today)throw Error('流水起点日期无效');if(state.settlements.length&&action.ledger_started_on!==state.settings.ledger_started_on)throw Error('已有结算不能改写流水起点');state.settings.ledger_started_on=action.ledger_started_on;}
    if(action.october2026_destination!==undefined){if(!['sister_watch','savings'].includes(action.october2026_destination))throw Error('分配用途无效');state.settings.october2026_destination=action.october2026_destination;}
  }else if(action.action==='save_thesis'){
    const c=code(action.code);state.theses[c]={code:c,thesis:text(action.thesis),confirm_conditions:text(action.confirm_conditions),invalidate_conditions:text(action.invalidate_conditions),next_review:action.next_review||null,updated_at:now.toISOString(),evidence_status:'人工研究假设，尚未核验为事实'};state.thesis_events??=[];state.thesis_events.push({id:crypto.randomUUID(),at:now.toISOString(),record:structuredClone(state.theses[c])});
    if(action.next_review&&!validDay(action.next_review))throw Error('复核日期无效');
  }else if(action.action==='add_trade'){
    const c=code(action.code),pnl=finite(action.realized_net_cny),ref=text(action.statement_ref,120);
    if(pnl===null||!validDay(action.date)||action.date>today)throw Error('交易日期或已实现净盈亏无效');
    if(state.settings.ledger_started_on&&action.date<state.settings.ledger_started_on)throw Error('交易早于已确认流水起点');
    if(state.settlements.some(s=>action.date<=s.period_end))throw Error('已结算期间不回填，需在后续期间另记券商核对调整');
    if(state.trades.some(t=>t.code===c&&t.date===action.date&&t.statement_ref===ref))throw Error('该券商流水已记录，避免重复计算');
    state.trades.push({id:crypto.randomUUID(),code:c,date:action.date,realized_net_cny:pnl,statement_ref:ref,note:String(action.note||'').slice(0,1000),recorded_at:now.toISOString(),basis:'用户核对券商净额，已含交易费用；不由行情推算历史成本'});
  }else if(action.action==='save_valuation_hypothesis'){
    const c=code(action.code),result=hypotheticalValuation(action),source=new URL(action.source_url);
    if(source.protocol!=='https:'||source.username||source.password)throw Error('研究来源需HTTPS，不含凭据');
    state.valuations??={};state.valuations[c]={...result,code:c,source_url:source.href,assumption_basis:text(action.assumption_basis),as_of:today,recorded_at:now.toISOString(),verification:'人工输入假设，来源页面尚未自动核验；不能视为已证实内在价值'};
  }else if(action.action==='record_equity'){
    if(action.confirm_cashflows_complete!==true)throw Error('请核对该期间入出金完整；未记录的资金变化会扭曲收益');
    state.equity_points??=[];state.equity_points.push(equityObservation(state.equity_points,action.equity_cny,action.cashflows||[],now));
  }else if(action.action==='void_trade'){
    if(state.settlements.some(s=>s.trade_ids.includes(action.id)))throw Error('已结算流水不能撤销，需追加调整记录');
    const t=state.trades.find(t=>t.id===action.id);if(!t)throw Error('流水不存在');t.voided=true;t.voided_at=now.toISOString();
  }else if(action.action==='settle'){
    const preview=settlementPreview(state,now);if(!preview.due)throw Error('尚未到结算期或本期已结算');
    if(action.confirm_complete!==true)throw Error('请先核对本期券商流水完整且净额已含费用');
    state.settlements.push({...preview,id:crypto.randomUUID(),confirmed_at:now.toISOString(),confirmed_ledger_complete:true});
  }else throw Error('未知研究操作');
  if(state.trades.length>2000)throw Error('流水超过本窗口容量，请先导出备份');
  state.revision++;return state;
}
export function portfolioMetrics(holdings,reviews,settings,session){
  const missing=[],cash=finite(settings.investable_cash),rows=[];
  if(cash===null||cash<0)missing.push('可投资现金未填写');
  for(const holding of holdings){const study=reviews[holding.code],quote=study?.current_quote,shares=finite(holding.shares),cost=finite(holding.cost);
    const valid=quote?.session===session&&finite(quote.price)>0&&shares>0;
    if(!valid)missing.push(holding.code+'股数或当期未复权报价未齐');
    rows.push({code:holding.code,name:holding.name,shares,price:valid?quote.price:null,market_value:valid?quote.price*shares:null,
      unrealized_profit:valid&&cost>0?(quote.price-cost)*shares:null,industry:holding.industry||study?.industry||'行业待核验'});
  }
  const equity=missing.length?null:cash+rows.reduce((s,r)=>s+r.market_value,0),sector={};
  for(const r of rows){r.weight=equity>0?r.market_value/equity:null;if(r.weight!==null)sector[r.industry]=(sector[r.industry]||0)+r.weight;const h=reviews[r.code]?.risk_history,values=h?.rows?.slice(1).map((x,i)=>x.close/h.rows[i].close-1);r.annualized_volatility=null;if(h?.calendar_complete&&h.session===session&&values.length>=40){const mean=values.reduce((a,b)=>a+b,0)/values.length;r.annualized_volatility=Math.sqrt(values.reduce((a,b)=>a+(b-mean)**2,0)/(values.length-1)*252);}}
  const correlations=[];
  for(let i=0;i<rows.length;i++)for(let j=i+1;j<rows.length;j++){
    const returns=r=>{const h=reviews[r.code]?.risk_history;if(h?.session!==session)return new Map();const v=h.rows;return new Map(v.slice(1).map((x,k)=>[x.date,{previous:v[k].date,value:x.close/v[k].close-1}]));};
    const a=returns(rows[i]),b=returns(rows[j]),pairs=[...a].filter(([d,v])=>b.has(d)&&b.get(d).previous===v.previous).map(([d,v])=>[v.value,b.get(d).value]);let corr=null;
    if(pairs.length>=40){const n=pairs.length,ma=pairs.reduce((s,p)=>s+p[0],0)/n,mb=pairs.reduce((s,p)=>s+p[1],0)/n;const aa=pairs.reduce((s,p)=>s+(p[0]-ma)**2,0),bb=pairs.reduce((s,p)=>s+(p[1]-mb)**2,0);if(aa*bb>0)corr=pairs.reduce((s,p)=>s+(p[0]-ma)*(p[1]-mb),0)/Math.sqrt(aa*bb);}
    correlations.push({a:rows[i].code,b:rows[j].code,samples:pairs.length,correlation:corr});
  }
  return {rows,missing,equity,sector,correlations,portfolio_drawdown:null,drawdown_note:'缺少带入出金调整的真实权益曲线，未用当前持仓倒推历史回撤',basis:'可投资现金及已确认持仓；学费等保护资金不计入预算'};
}
export function riskPositionSize(equity,entry,stop,settings,lot=100){
  if(!(equity>0&&entry>stop&&stop>0&&Number.isInteger(lot)&&lot>0))return null;
  const shares=Math.floor(Math.min(equity*settings.risk_per_trade/(entry-stop),equity*settings.max_position_weight/entry)/lot)*lot;
  return {shares,value:shares*entry,planned_loss:shares*(entry-stop),basis:'仓位研究上限，交易单位需按证券规则确认；跳空/跌停/T+1等可能使实际损失超预算，无下单'};
}
export function hypotheticalValuation({price,bear_profit,base_profit,bull_profit,fair_pe,shares,years=3,current_profit}){
  const values=[price,bear_profit,base_profit,bull_profit,fair_pe,shares,years].map(finite);
  [price,bear_profit,base_profit,bull_profit,fair_pe,shares,years]=values;current_profit=finite(current_profit);
  if(values.some(v=>v===null||v<=0)||!(bear_profit<=base_profit&&base_profit<=bull_profit))throw Error('需明确正的价格、股数、合理PE及有序三情景利润；这是手动假设');
  const [bear,base,bull]=[bear_profit,base_profit,bull_profit].map(p=>p*fair_pe/shares);
  return {price,shares,fair_pe,years,current_profit,bear,base,bull,safety_margin:1-price/base,bear_downside:Math.max(0,1-bear/price),required_profit:price*shares/fair_pe,
    required_cagr:current_profit>0?(price*shares/fair_pe/current_profit)**(1/years)-1:null,status:'user_assumptions_unverified',influences_ranking:false};
}
/** @param {any} review @param {any} scenario @param {string|null} expectedSession */
export function holdingDecision(review,scenario,expectedSession=null){
  const top=p=>review?.frames?.[p]?.signals?.some(s=>s.direction==='bearish'&&s.indicator==='MACD_DIF'&&s.quality_passed===true)===true;
  const daily=review?.frames?.daily,sixty=review?.frames?.['60'];
  const price=review?.current_quote?.price,ma13=daily?.averages_qfq?.['13'];
  const supportWeak=daily?.status==='ok'&&daily.price<ma13&&daily.structure?.ma20_rising===false;
  const weak=top('daily')||(top('60')&&supportWeak),expensive=scenario?.base>0&&price/scenario.base>=1.2;
  const points=(review?.risk_history?.rows||[]).slice(-60);let low=Infinity,advance=0;
  for(const r of points){low=Math.min(low,r.close);if(low>0)advance=Math.max(advance,r.close/low-1);}
  const complete=daily?.status==='ok'&&sixty?.status==='ok'&&price>0&&(!expectedSession||review.session===expectedSession);
  const label=!complete?'数据不足或日期未齐，无法完整判断持仓风险':expensive&&weak?'估值假设偏高且技术转弱，优先核验利润保护':weak?'核验技术及投资理由失效条件':expensive?'核对估值假设与兑现预期':'按原趋势及投资逻辑观察';
  return {label,weak,data_complete:complete,valuation_known:!!scenario,material_advance_close_proxy:advance>=.2,
    partial_reduction_scenario:complete&&weak&&advance>=.2?[1/3,.5]:null,
    reasons:[top('daily')?'日线严格MACD顶背离':null,top('60')?'60分钟严格MACD顶背离':null,supportWeak?'日收盘在MA13下且MA20未走强':null].filter(Boolean),
    basis:'以真实日线及60分钟确认结合估值假设；15分钟单独信号不定义趋势结束；近60日涨幅为收盘序列代理，不是持仓真实收益。无自动卖单',orders_authorized:false};
}
export function opportunityComparison(holdingCode,candidateCode,reviews,scenarios,costFraction=0){
  if(!(costFraction>=0&&costFraction<1))throw Error('增量换仓成本范围无效');
  if(!reviews[holdingCode]?.current_quote?.session||reviews[holdingCode].current_quote.session!==reviews[candidateCode]?.current_quote?.session)return {holding:null,candidate:null,basis:'两只股票报价日期未齐或不一致，未混用日期计算换仓比较'};
  const row=(c,cost)=>{const v=scenarios[c],p=reviews[c]?.current_quote?.price;if(!v||!(p>0&&v.base>p&&v.bear>=0&&v.bear<p))return null;return {reward_risk:(v.base-p-p*cost)/(p-v.bear+p*cost),price:p,scenario_as_of:v.as_of,confidence:v.status};};
  return {holding:row(holdingCode,0),candidate:row(candidateCode,costFraction),switching_cost_fraction:costFraction,basis:'相同手动情景口径；候选扣增量换仓成本；区间并非概率分布，不自动决定换仓'};
}
export function equityObservation(points,equity,cashflows,now=new Date()){
  const value=finite(equity),end=now.getTime(),previous=points.at(-1);
  if(!(value>0)||!Array.isArray(cashflows)||cashflows.length>100)throw Error('需正的可投资权益和明确入出金列表');
  if(!previous){if(cashflows.length)throw Error('首次观测不回填此前资金流，直接记录初始权益');return {at:now.toISOString(),equity_cny:value,cashflows:[],nav:1,period_return:null,basis:'从首次实际记录开始，不推算此前最高权益'};}
  const start=Date.parse(previous.at),duration=end-start,seen=new Set(points.flatMap(p=>p.cashflows.map(f=>f.id)));
  if(duration<=0)throw Error('权益观测必须晚于上一条记录');
  let total=0,weighted=0;const flows=cashflows.map(f=>{const amount=finite(f.amount),at=Date.parse(f.at);
    if(typeof f.id!=='string'||!f.id||seen.has(f.id)||amount===null||!(at>start&&at<=end))throw Error('资金流时间、金额或唯一编号无效');seen.add(f.id);total+=amount;weighted+=amount*(end-at)/duration;return {id:f.id,at:new Date(at).toISOString(),amount};});
  const capital=previous.equity_cny+weighted;if(!(capital>0))throw Error('现金流调整后的投入资本必须为正');
  const r=(value-previous.equity_cny-total)/capital;if(!Number.isFinite(r)||r<=-1)throw Error('无法构建有效的权益观察净值');
  return {at:now.toISOString(),equity_cny:value,cashflows:flows,nav:previous.nav*(1+r),period_return:r,basis:'Modified Dietz现金流加权近似收益；按实际时间登记资金流，非逐笔TWR或券商实际收益证明'};
}
export function observedDrawdown(points){if(points.length<2)return null;let peak=points[0].nav,dd=0;for(const p of points){peak=Math.max(peak,p.nav);dd=Math.min(dd,p.nav/peak-1);}return {maximum_drawdown:dd,started_at:points[0].at,last_at:points.at(-1).at,samples:points.length,basis:'仅已记录且资金流已核对的权益观察期；不代表历史真实最大回撤'};}
