// Independent completed-bar observations, parity-tested against kd_state.py.
import defaults from './kd-state-policy.json' with {type:'json'};
export const KD_VERSION='kd_state_observer_v1';
export const KD_PERIODS=['monthly','weekly','daily','120','60','30','15'];
export const KD_NAMES={monthly:'月线',weekly:'周线',daily:'日线','120':'120分钟','60':'60分钟','30':'30分钟','15':'15分钟'};
const labels={low_weak:'低位弱势',early_turn:'早期转折',low_golden_cross:'低位金叉',golden_cross:'金叉（非低位）',bullish_continuation:'偏多延续',momentum_weakening:'动能减弱',bearish_turn:'偏空转折',bearish_continuation:'偏空延续',mixed:'方向未一致',flat:'中性整理'};
const directions={low_weak:'bearish',early_turn:'recovering',low_golden_cross:'bullish',golden_cross:'bullish',bullish_continuation:'bullish',momentum_weakening:'weakening',bearish_turn:'bearish',bearish_continuation:'bearish',mixed:'mixed',flat:'mixed'};
const finite=x=>typeof x==='number'&&Number.isFinite(x);
export function validateKDPolicy(p){
  if(p.schema!==1||!Number.isInteger(p.history_bars)||p.history_bars<3||p.history_bars>20)throw Error('K/D配置版本或轨迹长度无效');
  if(['low_level','high_level','slope_min_points','gap_change_min_points','weak_cross_gap_points'].some(k=>!finite(p[k])))throw Error('K/D阈值必须为有限数值');
  if(!(0<=p.low_level&&p.low_level<p.high_level&&p.high_level<=100))throw Error('K/D高低位阈值无效');
  if(['slope_min_points','gap_change_min_points','weak_cross_gap_points'].some(k=>p[k]<=0))throw Error('K/D变化阈值须为正');
  const floors={monthly:24,weekly:60,daily:250,'120':60,'60':60,'30':60,'15':60};
  if(Object.keys(p.minimum_bars||{}).sort().join()!==[...KD_PERIODS].sort().join()||KD_PERIODS.some(k=>!Number.isInteger(p.minimum_bars[k])||p.minimum_bars[k]<Math.max(floors[k],p.history_bars)))throw Error('K/D观察不得降低已有周期历史要求');
  return p;
}
export function unavailableKD(period,source=null,reason='数据不足，无法判断'){
  return {version:KD_VERSION,period,source,status:'unavailable',state:'unavailable',label:'数据不足，无法判断',error:reason,history:[],influences_ranking:false,trade_confirmation:false};
}
function timestamp(value){
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?$/.test(value))throw Error('K/D缺少完整K线时间');
  const zoned=/(?:Z|[+-]\d{2}:\d{2})$/.test(value),normalized=value.length===10?value+'T00:00:00':value;
  const ms=Date.parse(zoned?value:normalized+'+08:00');if(!Number.isFinite(ms))throw Error('K/D缺少完整K线时间');
  return new Date(ms+8*3600000).toISOString().slice(0,19);
}
export function classifyKD(rows,period,source,completeThrough,policy=defaults){
  const p=validateKDPolicy(policy);if(!KD_PERIODS.includes(period))throw Error('未知K/D观察周期');
  let cutoff,selected,history;
  try{
    cutoff=timestamp(completeThrough);selected=rows.map(r=>({...r,date:timestamp(r.date)})).filter(r=>r.date<=cutoff);
    if(selected.some((r,i)=>i&&r.date<=selected[i-1].date))throw Error('K/D源时间重复或乱序');
    if(selected.length<p.minimum_bars[period])throw Error('完整周期历史不足，不能仅凭最新K/D判断');
    history=selected.slice(-p.history_bars).map(r=>{
      if(!finite(r.k)||!finite(r.d)||r.k<0||r.k>100||r.d<0||r.d>100)throw Error('最近K/D轨迹缺失或超出有效范围');
      const j=3*r.k-2*r.d;if(r.j!==null&&r.j!==undefined&&(!finite(r.j)||Math.abs(r.j-j)>1e-8))throw Error('J与3K−2D口径不一致');
      return {date:r.date,k:r.k,d:r.d,j,gap:r.k-r.d};
    });
  }catch(e){return unavailableKD(period,source,e.message);}
  const [a,b,c]=history.slice(-3),dk=c.k-b.k,dd=c.d-b.d,gapChange=c.gap-b.gap,slope=p.slope_min_points,change=p.gap_change_min_points;
  const up=b.gap<=0&&c.gap>0,down=b.gap>=0&&c.gap<0,low=Math.max(c.k,c.d)<=p.low_level,high=Math.min(c.k,c.d)>=p.high_level;
  const conditions={k_above_d:c.gap>0,k_rising:dk>=slope,d_rising:dd>=slope,k_falling:dk<=-slope,d_falling:dd<=-slope,cross_up:up,cross_down:down,gap_narrowing:b.gap>c.gap&&c.gap>=0&&gapChange<=-change};
  let state;
  if(up)state=Math.max(b.k,b.d)<=p.low_level?'low_golden_cross':'golden_cross';
  else if(down)state='bearish_turn';
  else if(low&&c.gap<0&&dk<=-slope&&dd<=-slope)state='low_weak';
  else if(dk>=slope&&dd<slope)state='early_turn';
  else if(c.gap>0&&dk>=slope&&dd>=slope)state='bullish_continuation';
  else if(dk<=-slope&&conditions.gap_narrowing)state='momentum_weakening';
  else if(c.gap<0&&dk<=-slope&&dd<=-slope)state='bearish_continuation';
  else if(Math.abs(dk)<slope&&Math.abs(dd)<slope)state='flat';
  else state='mixed';
  const transitions=[];for(let i=1;i<history.length;i++){const old=history[i-1],r=history[i];if(old.gap<=0&&r.gap>0||old.gap>=0&&r.gap<0)transitions.push({date:r.date,type:r.gap>0?'golden_cross':'dead_cross',gap:r.gap,weak_separation:Math.abs(r.gap)<p.weak_cross_gap_points});}
  return {version:KD_VERSION,period,source,status:'ok',state,label:labels[state],direction:directions[state],zone:low?'low':high?'high':'middle',last_bar:c.date,complete_through:cutoff,source_bar_count:selected.length,current:c,previous:b,k_change:dk,d_change:dd,gap_change:gapChange,new_k_upturn:dk>=slope&&b.k-a.k<slope,conditions,history,last_cross_in_history:transitions.at(-1)||null,weak_cross:(up||down)&&Math.abs(c.gap)<p.weak_cross_gap_points,influences_ranking:false,trade_confirmation:false,basis:'仅完整K线的K/D状态；J=3K−2D，不作为独立第二因子加分；指标交叉不等于价格或买点确认'};
}
export function combineKD(states){
  const frames=Object.fromEntries(KD_PERIODS.map(p=>[p,states[p]||unavailableKD(p)]));
  const groups=[['monthly',['monthly'],'月线战略'],['weekly',['weekly'],'周线趋势'],['daily',['daily'],'日线确认'],['structure',['120','60'],'120/60分钟结构组'],['execution',['30','15'],'30/15分钟执行组']].map(([group,periods,label])=>{
    const available=periods.filter(p=>frames[p].status==='ok'),directions=new Set(available.map(p=>frames[p].direction));
    const direction=directions.has('bullish')&&directions.has('bearish')?'conflict':directions.size===1&&directions.has('bullish')?'bullish':directions.size===1&&directions.has('bearish')?'bearish':available.length?'mixed':'unavailable';
    return {group,label,periods,available_periods:available,direction,correlated:periods.length>1};
  });
  const bulls=KD_PERIODS.filter(p=>frames[p].direction==='bullish'),bears=KD_PERIODS.filter(p=>frames[p].direction==='bearish'),conflict=!!(bulls.length&&bears.length),macro=['monthly','weekly'].every(p=>frames[p].direction==='bullish');
  return {version:KD_VERSION,frames,groups,data_complete:KD_PERIODS.every(p=>frames[p].status==='ok'),missing_periods:KD_PERIODS.filter(p=>frames[p].status!=='ok'),bullish_periods:bulls,bearish_periods:bears,conflict,label:conflict?'周期K/D分歧':macro?'月周K/D偏多，仍需原买点条件':'K/D状态观察，不独立确认买点',influences_ranking:false,orders_authorized:false,trade_confirmation:false,basis:'月周定方向、日线确认、分钟执行；120/60同源且相关，30/15亦相关，按组展示不按信号个数累加；J与K−D不重复计分'};
}
