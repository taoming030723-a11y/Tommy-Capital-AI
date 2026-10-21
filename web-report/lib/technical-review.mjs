// Focused research only: no score/rank mutations. Formulas match technical.py;
// parity tests compare this browser-service implementation with the Python engine.
import monitorPolicy from './monitor-policy.json' with {type:'json'};
import technicalPolicy from './technical-policy.json' with {type:'json'};
export const REVIEW_VERSION = 'focused_six_period_v1';
export const POLICY_ID = (()=>{let hash=2166136261;for(const c of JSON.stringify([technicalPolicy,monitorPolicy]))hash=Math.imul(hash^c.charCodeAt(0),16777619);return (hash>>>0).toString(16);})();
export const PERIODS = ['monthly', 'weekly', 'daily', '60', '30', '15'];
export const PERIOD_NAMES = {monthly:'月线',weekly:'周线',daily:'日线','60':'60分钟','30':'30分钟','15':'15分钟'};
export const MA_PERIODS = [5,13,20,55,60,144,233];
const finite = x => x !== null && x !== undefined && Number.isFinite(x);
const mean = a => a.length && a.every(finite) ? a.reduce((x,y)=>x+y,0)/a.length : null;
const tailMean = (a,i,n) => i+1>=n ? mean(a.slice(i-n+1,i+1)) : null;
const min = a => Math.min(...a), max = a => Math.max(...a);

export function calculateIndicators(rows) {
  let ema12=null,ema26=null,dea=null,k=50,d=50;
  const closes=rows.map(r=>r.close),ranges=[];
  return rows.map((r,i)=>{
    const previous=i ? rows[i-1].close : r.close;
    ranges.push(Math.max(r.high-r.low,Math.abs(r.high-previous),Math.abs(r.low-previous)));
    ema12=ema12===null?r.close:ema12+(r.close-ema12)*2/13;
    ema26=ema26===null?r.close:ema26+(r.close-ema26)*2/27;
    const dif=ema12-ema26;dea=dea===null?dif:dea+(dif-dea)*2/10;
    const ma=Object.fromEntries(MA_PERIODS.map(n=>[n,tailMean(closes,i,n)]));
    let kval=null,dval=null,j=null;
    if(i>=8){const recent=rows.slice(i-8,i+1),lo=min(recent.map(x=>x.low)),hi=max(recent.map(x=>x.high));
      const rsv=hi>lo?100*(r.close-lo)/(hi-lo):50;k=2/3*k+rsv/3;d=2/3*d+k/3;kval=k;dval=d;j=3*k-2*d;}
    return {...r,ma,dif,dea,histogram:2*(dif-dea),atr14:tailMean(ranges,i,14),k:kval,d:dval,j};
  });
}

export function strictDivergences(calculated,config={}) {
  const a=calculated.slice(-120),signals=[],weak=[];
  if(a.length<35)return {status:'insufficient_bars',signals,weak_signals:weak};
  for(const [direction,field] of [['bullish','low'],['bearish','high']]){
    const pivots=[];
    for(let i=3;i<a.length-3;i++){
      const neighbors=[...a.slice(i-3,i),...a.slice(i+1,i+4)].map(r=>r[field]);
      if(direction==='bullish'?a[i][field]<min(neighbors):a[i][field]>max(neighbors))pivots.push(i);
    }
    if(pivots.length<2)continue;
    const [p,q]=pivots.slice(-2);if(a.length-1-q>12)continue;
    const x=a[p],y=a[q];
    for(const [indicator,key] of [['MACD_DIF','dif'],['KDJ_J','j']]){
      if(!finite(x[key])||!finite(y[key]))continue;
      const ok=direction==='bullish'?y[field]<x[field]&&y[key]>x[key]:y[field]>x[field]&&y[key]<x[key];if(!ok)continue;
      const signal={direction,indicator,previous_pivot:x.date,current_pivot:y.date,confirmed_at:a[q+3].date,price_previous:x[field],price_current:y[field],indicator_previous:x[key],indicator_current:y[key]};
      if(indicator==='MACD_DIF'){
        const atr=y.atr14,move=Math.abs(y[field]-x[field]),difMove=Math.abs(y.dif-x.dif),rebound=(direction==='bullish'?1:-1)*(a[q+3].close-y.close);
        const c={pivot_spacing:q-p>=(config.macd_min_pivot_spacing??5),price_extension:move/x[field]*100>=(config.macd_min_price_extension_pct??.2),price_extension_atr:atr>0&&move/atr>=(config.macd_min_price_extension_atr??.15),dif_improvement_atr:atr>0&&difMove/atr>=(config.macd_min_dif_improvement_atr??.1),same_zero_region:direction==='bullish'?x.dif<0&&y.dif<0:x.dif>0&&y.dif>0,confirmation_rebound:atr>0&&rebound/atr>=(config.macd_min_confirmation_rebound_atr??.3),not_invalidated:direction==='bullish'?min(a.slice(q+3).map(r=>r.low))>=y.low:max(a.slice(q+3).map(r=>r.high))<=y.high};
        signal.conditions=c;signal.quality_passed=Object.values(c).every(Boolean);
        if(!signal.quality_passed){weak.push(signal);continue;}
      }
      signals.push(signal);
    }
  }
  return {status:'ok',signals,weak_signals:weak};
}

export function weeklyMA55(calc){
  const recent=calc.slice(-6),latest=calc.at(-1),start=calc.at(-4);
  if(recent.length<6||!latest?.atr14||recent.some(r=>!(r.ma[55]>0)))return {status:'unavailable'};
  const slope=(latest.ma[55]-start.ma[55])/latest.atr14;
  let consecutive=0;for(const r of [...recent].reverse()){if(r.close<=r.ma[55])break;consecutive++;}
  return {status:'ok',ma55_qfq:latest.ma[55],slope_3w_atr:slope,direction:slope>=.05?'rising':slope<=-.05?'falling':'flat',above_count:recent.filter(r=>r.close>r.ma[55]).length,consecutive_above:consecutive,latest_week_above:latest.close>latest.ma[55]};
}

export function histogramWaves(calc,policy=monitorPolicy){
  const observations=[];
  for(const [sign,direction,field] of [[1,'bearish','high'],[-1,'bullish','low']]){
    const groups=[];let current=null;
    for(let i=0;i<calc.length;i++){
      if(calc[i].histogram*sign>0){
        if(!current)current={start:i,end:i,peak:i,closed:false};
        current.end=i;if(Math.abs(calc[i].histogram)>Math.abs(calc[current.peak].histogram))current.peak=i;
      }else if(current){current.closed=true;if(current.end-current.start+1>=policy.wave_min_bars)groups.push(current);current=null;}
    }
    if(current&&current.end-current.start+1>=policy.wave_min_bars)groups.push(current);
    if(groups.length<2)continue;
    const [a,b]=groups.slice(-2),first=Math.abs(calc[a.peak].histogram),second=Math.abs(calc[b.peak].histogram),atr=calc[b.peak].atr14;
    if(!(first>0&&(first-second)/first*100>=policy.hist_peak_reduction_pct&&atr>0&&first-second>=policy.hist_peak_difference_atr*atr))continue;
    const extreme=g=>sign>0?max(calc.slice(g.start,g.end+1).map(x=>x[field])):min(calc.slice(g.start,g.end+1).map(x=>x[field]));
    const old=extreme(a),now=extreme(b),extended=sign>0?now>old:now<old;
    observations.push({direction,state:extended?'suspected':'momentum_change',kind:extended?'histogram_divergence':'histogram_decay',label:extended?(sign>0?'疑似柱体顶背离，等待严格枢轴确认':'疑似柱体底背离，等待严格枢轴确认'):(sign>0?'上涨动能减弱，未确认顶背离':'下跌动能减弱，未确认底背离'),histogram_peaks:[calc[a.peak].histogram,calc[b.peak].histogram],peak_dates:[calc[a.peak].date,calc[b.peak].date],price_extremes:[old,now],current_wave_complete:b.closed,confirmed:false});
  }
  return observations;
}

export function frameSnapshot(rows,period,source,config={}){
  const required=period==='daily'?250:period==='weekly'?60:period==='monthly'?24:60;
  if(rows.length<required)return {period,status:'insufficient_bars',source,bar_count:rows.length,required_bars:required,label:'数据不足，无法判断'};
  const calc=calculateIndicators(rows),r=calc.at(-1),p=calc.at(-2),old=calc.at(-6),baseline=mean(rows.slice(-21,-1).map(x=>x.volume));
  const div=strictDivergences(calc,config),waves=histogramWaves(calc);
  const macdCross=r.dif>r.dea&&p.dif<=p.dea,kdjCross=r.k>r.d&&p.k<=p.d;
  const hist=r.histogram,pHist=p.histogram;
  const histogramChange=hist>0&&pHist>0&&hist<pHist?'最近红柱缩短（仅动能变化）':hist<0&&pHist<0&&Math.abs(hist)<Math.abs(pHist)?'最近绿柱缩短（仅动能变化）':'柱体未见同向缩短';
  const structure={close_qfq:r.close,low_qfq:r.low,high_qfq:r.high,previous_close_qfq:p.close,ma20_qfq:r.ma[20],ma20_rising:r.ma[20]>old.ma[20],support_qfq:min(rows.slice(-20).map(x=>x.low)),resistance_qfq:max(rows.slice(-21,-1).map(x=>x.high)),ratio20:baseline>0?r.volume/baseline:null,price_reclaimed:r.close>p.high||r.close>r.ma[20],overheated:r.j>100&&r.close>r.ma[20]*1.03,pullback_rebound:p.close>=p.ma[20]&&r.low<=r.ma[20]&&r.close>r.ma[20]&&r.close>p.close};
  return {period,status:'ok',source,last_bar:r.date,bar_count:rows.length,required_bars:required,price:r.close,averages_qfq:r.ma,above_averages:Object.fromEntries(MA_PERIODS.map(n=>[n,finite(r.ma[n])?r.close>r.ma[n]:null])),ma20_rising:structure.ma20_rising,macd:{dif:r.dif,dea:r.dea,histogram:hist,previous_histogram:pHist,golden_cross:macdCross,histogram_change:histogramChange,wave_observations:waves},kdj:{k:r.k,d:r.d,j:r.j,previous_j:p.j,rising:r.j>p.j,golden_cross:kdjCross},structure,signals:div.signals,weak_signals:div.weak_signals,divergence_status:div.status,monthly_j_path:period==='monthly'?calc.slice(-6).map(x=>({date:x.date,j:x.j})):null,weekly_ma55:period==='weekly'?weeklyMA55(calc):null,label:div.signals.map(s=>(s.direction==='bullish'?'底':'顶')+'背离／'+s.indicator).join('、')||waves.map(w=>w.label).join('、')||'无已确认背离'};
}

export function weeklyStructure(weekly,daily,config={}){
  if(weekly.length<7)return {confirmed:false};
  const recent=weekly.slice(-6),before=recent.slice(0,3),after=recent.slice(3),last=recent.at(-1);
  const dailyCount=w=>daily.filter(r=>r.date.slice(0,10)<=w.date.slice(0,10)&&r.date.slice(0,10)>new Date(Date.parse(w.date.slice(0,10)+'T00:00:00Z')-7*86400000).toISOString().slice(0,10)).length;
  const volumes=recent.map(r=>dailyCount(r)?r.volume/dailyCount(r):null),volumeRatio=mean(volumes.slice(3))/mean(volumes.slice(0,3));
  const range=(max(recent.map(r=>r.high))/min(recent.map(r=>r.low))-1)*100;
  const lowChange=(min(after.map(r=>r.low))/min(before.map(r=>r.low))-1)*100;
  const stable=last.close>=mean(after.map(r=>r.close));
  const previousRange=max(before.map(r=>r.high))/min(before.map(r=>r.low))-1,currentRange=max(after.map(r=>r.high))/min(after.map(r=>r.low))-1;
  const conditions={base:range<=(config.weekly_base_max_range_pct??20)&&lowChange>=-(config.weekly_base_low_tolerance_pct??3)&&last.close>=mean(recent.map(r=>r.close))&&finite(volumeRatio)&&volumeRatio<=(config.weekly_base_max_volume_ratio??1.1),higher_low:min(after.map(r=>r.low))>min(before.map(r=>r.low))&&stable,volatility_contraction:previousRange>0&&currentRange<previousRange&&currentRange*100<=(config.weekly_base_max_range_pct??20)&&stable&&min(after.map(r=>r.low))>=min(before.map(r=>r.low)),platform_breakout:last.close>max(weekly.slice(-7,-1).map(r=>r.high))};
  return {confirmed:Object.values(conditions).some(Boolean),conditions};
}

export function firstBreakout(daily,config={}){
  const events=[];let last=-1000;
  for(let i=Math.max(20,daily.length-(config.daily_breakout_lookback??20));i<daily.length;i++){
    const r=daily[i],prior=daily.slice(i-20,i),v=mean(prior.map(x=>x.volume)),high=max(prior.map(x=>x.high));if(!(v>0))continue;
    if(i-last>=20&&r.volume/v>=(config.daily_breakout_rvol??1.5)&&r.close>high&&r.high>r.low&&(r.close-r.low)/(r.high-r.low)>=(config.daily_breakout_close_location??.65)&&(high/min(prior.map(x=>x.low))-1)*100<=(config.daily_breakout_platform_range_pct??20)){
      events.push({confirmed_at:r.date,platform_high_qfq:high,ratio20:r.volume/v});last=i;
    }
  }
  return {active:!!events.length&&daily.at(-1).close>=events[0].platform_high_qfq,first_event:events[0]||null};
}

export function assessTechnical({code,session,frames,rawFrames={},config=technicalPolicy,route=null,checkedAt=new Date().toISOString()}){
  const valid=p=>frames[p]?.status==='ok',f=p=>valid(p)?frames[p]:null;
  const m=f('monthly'),w=f('weekly'),d=f('daily'),m60=f('60'),m30=f('30'),m15=f('15');
  const ma55=w?.weekly_ma55,path=m?.monthly_j_path||[],js=path.map(x=>x.j),j=js.at(-1),rising=m?.kdj.rising===true;
  const pathA=finite(j)&&(j<20||(min(js.slice(-3))<20&&rising)),pathB=js.length===6&&min(js)<20&&rising;
  const weekStructure=rawFrames.weekly&&rawFrames.daily?weeklyStructure(rawFrames.weekly,rawFrames.daily,config):{confirmed:false};
  const breakout=rawFrames.daily?firstBreakout(rawFrames.daily,config):{active:false};
  const bottoms=(m15?.signals||[]).filter(s=>s.direction==='bullish'&&s.indicator==='MACD_DIF'&&s.quality_passed);
  const support=bottoms.length?min(bottoms.map(s=>s.price_current)):m15?.structure.support_qfq;
  const near=support>0&&m15&&m15.structure.low_qfq>=support&&(m15.structure.low_qfq/support-1)*100<=(config.execution_support_distance_pct??3);
  const latestDaily=rawFrames.daily?calculateIndicators(rawFrames.daily):null;
  const latestWeekly=rawFrames.weekly?calculateIndicators(rawFrames.weekly):null;
  const c={
    data_complete:PERIODS.every(valid),
    monthly_route:route==='B'?pathB:route==='A'?pathA:(pathA||pathB),
    monthly_j_recovering:rising,
    weekly_ma55:!!(ma55?.status==='ok'&&['rising','flat'].includes(ma55.direction)&&ma55.latest_week_above&&d?.price>ma55.ma55_qfq&&ma55.above_count>=(config.weekly_ma55_min_above_count??3)&&ma55.consecutive_above>=(config.weekly_ma55_min_consecutive??2)),
    weekly_structure:!!weekStructure.confirmed,
    daily_price_volume:route==='B'?breakout.active:breakout.active||!!(d?.structure.ratio20>=(config.daily_volume_abnormal_ratio??2)),
    monthly_trend:!!(m&&m.price>m.averages_qfq[20]),
    weekly_trend:!!(w&&w.price>w.averages_qfq[20]&&latestWeekly&&latestWeekly.at(-1).ma[20]>=latestWeekly.at(-4).ma[20]),
    daily_trend:!!(d&&d.price>d.averages_qfq[60]&&latestDaily&&latestDaily.at(-1).ma[20]>latestDaily.at(-6).ma[20]),
    same_session_60m_structure:!!(m60&&m60.price>m60.averages_qfq[20]&&m60.last_bar.startsWith(session)),
    '30m_without_top_conflict':!!(m30&&!m30.signals.some(s=>s.direction==='bearish')),
    confirmed_15m_bottom:!!bottoms.length,
    '15m_without_top_conflict':!!(m15&&!m15.signals.some(s=>s.direction==='bearish')),
    not_overheated:!!(m15&&!m15.structure.overheated),
    support_rebound:!!(near&&m15.price>m15.structure.previous_close_qfq),
    price_reclaimed:!!m15?.structure.price_reclaimed,
    execution_volume:!!(m15?.structure.ratio20>=(config.execution_volume_ratio??1.2)),
  };
  const labels={data_complete:'六周期完整行情',monthly_route:'原月J路线',monthly_j_recovering:'完整月J向上恢复',weekly_ma55:'周MA55强化门槛',weekly_structure:'周线结构确认',daily_price_volume:'原日线量价确认',monthly_trend:'原月线趋势',weekly_trend:'原周线趋势',daily_trend:'原日线趋势',same_session_60m_structure:'同交易日60分钟守MA20','30m_without_top_conflict':'30分钟无顶背离冲突',confirmed_15m_bottom:'15分钟严格MACD底背离','15m_without_top_conflict':'15分钟无顶背离冲突',not_overheated:'15分钟未过热',support_rebound:'已确认支撑附近回升',price_reclaimed:'价格结构收回',execution_volume:'原执行量比确认'};
  const unmet=Object.entries(c).filter(([,v])=>!v).map(([key])=>({key,label:labels[key]}));
  const macro=['monthly_route','monthly_j_recovering','weekly_ma55','weekly_structure','daily_price_volume','monthly_trend','weekly_trend','daily_trend'].every(k=>c[k]);
  const entry=Object.values(c).every(Boolean);
  const conflicts=PERIODS.filter(p=>valid(p)&&frames[p].signals.some(s=>s.direction==='bearish'));
  return {schema:1,version:REVIEW_VERSION,policy_id:POLICY_ID,code,session,checked_at:checkedAt,frames,data_complete:c.data_complete,missing_periods:PERIODS.filter(p=>!valid(p)),monthly_path:{route_a:pathA,route_b:pathB,rising},weekly_ma55:ma55||null,weekly_structure:weekStructure,first_breakout:breakout,conditions:c,condition_labels:labels,unmet_conditions:unmet,macro_conditions_met:macro,technical_entry_watch:entry,decision:!c.data_complete?'数据未齐，不能确认买点':entry?'技术买点观察条件齐备':macro?'大周期已确认，等待执行买点':'当前未满足原技术买点条件',conflicting_periods:conflicts,fundamental_evaluation:'复用已有基本面/估值基准，本模块不重复评估；仍需独立有效的基本面资格',orders_authorized:false,t_eligible:false,basis:'月周定方向，日线确认，60/30/15执行；任何单个小周期背离不授予新开仓或做T资格。'};
}

export function publicStudyStock(report,code){
  if(typeof code!=='string'||!/^\d{6}$/.test(code)||(report.public_ignored||[]).includes(code))return null;
  return (report.tracking||[]).find(r=>r.code===code&&r.appearance_count>=3)||null;
}
