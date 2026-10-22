import { codeValue } from './controls.mjs';
import { frameSnapshot, assessTechnical, PERIODS } from './technical-review.mjs';
import config from './technical-policy.json' with {type:'json'};
const DAY=86400000;
const number=x=>x!==null&&x!==''&&x!==undefined&&Number.isFinite(Number(x))?Number(x):null;
const day=x=>typeof x==='string'&&/^\d{4}-\d{2}-\d{2}/.test(x)?x.slice(0,10):null;
export function marketSymbol(code){codeValue(code);return (code.startsWith('6')?'sh':/^(4|8|92)/.test(code)?'bj':'sz')+code;}
async function read(url,text=false){const r=await fetch(url,{signal:AbortSignal.timeout(22000),headers:{Accept:'application/json',Referer:'https://finance.sina.com.cn/'}});if(!r.ok)throw Error('真实K线源HTTP '+r.status);return text?r.text():r.json();}
export function validateBars(rows,minute=false){
  if(!Array.isArray(rows)||!rows.length)throw Error('真实K线源为空');
  const seen=new Set();
  const result=rows.map(r=>{
    const date=minute?String(r.date).replace(' ','T'):day(r.date)+'T00:00:00';
    if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(date)||!Number.isFinite(Date.parse(date+'+08:00')))throw Error('K线时间格式异常');
    if(seen.has(date))throw Error('K线包含重复日期');seen.add(date);
    const v={date,...Object.fromEntries(['open','high','low','close','volume'].map(k=>[k,number(r[k])]))};
    if(Object.values(v).some(x=>x===null)||v.low<=0||v.open<=0||v.close<=0||v.high<Math.max(v.open,v.close,v.low)||v.low>Math.min(v.open,v.close,v.high)||v.volume<0)throw Error('K线价格/成交量不完整或异常');return v;
  });return result.sort((a,b)=>a.date.localeCompare(b.date));
}
export function weekEnd(date){const t=new Date(day(date)+'T00:00:00Z'),w=t.getUTCDay();t.setUTCDate(t.getUTCDate()+(5-w+7)%7);return t.toISOString().slice(0,10);}
export function monthEnd(date){const d=day(date);return new Date(Date.UTC(Number(d.slice(0,4)),Number(d.slice(5,7)),0)).toISOString().slice(0,10);}
export function aggregateBars(rows,period,session){const groups=new Map();for(const r of rows){const end=period==='weekly'?weekEnd(r.date):monthEnd(r.date);if(end>session)continue;
  const old=groups.get(end);groups.set(end,old?{...old,high:Math.max(old.high,r.high),low:Math.min(old.low,r.low),close:r.close,volume:old.volume+r.volume}:{...r,date:end+'T00:00:00'});
}return [...groups.values()];}
async function nativeBars(code,kind,session,adjust='qfq',count=640){
  const sym=marketSymbol(code),today=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Singapore'}),end=today===session?'':session;
  const url=new URL('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get');url.search=new URLSearchParams({param:`${sym},${kind},,${end},${count},${adjust}`});
  const body=(await read(url)).data?.[sym],key=adjust==='qfq'?'qfq'+kind:kind,records=body?.[key];
  if(!records?.length)throw Error('未取得真实'+key+'，未改用未复权价格');
  return validateBars(records.map(r=>({date:r[0],open:r[1],close:r[2],high:r[3],low:r[4],volume:r[5]}))).filter(r=>day(r.date)<=session);
}
let exchangeCache=null;
async function exchangeDays(cutoff){
  if(exchangeCache?.cutoff===cutoff&&Date.now()-exchangeCache.at<60000)return exchangeCache.days;
  const url=new URL('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get');url.search=new URLSearchParams({param:'sh000001,day,,,640,'});
  const body=(await read(url)).data?.sh000001?.day;
  if(!body?.length)throw Error('真实市场交易日历源缺失');
  const index=validateBars(body.map(r=>({date:r[0],open:r[1],close:r[2],high:r[3],low:r[4],volume:r[5]}))).filter(r=>day(r.date)<=cutoff);
  if(index.length<250)throw Error('市场交易日历历史不足');
  const days=index.map(r=>day(r.date));exchangeCache={cutoff,days,at:Date.now()};return days;
}
export async function latestTechnicalSession(now=new Date()){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Singapore',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(now).map(p=>[p.type,p.value]));
  const today=`${parts.year}-${parts.month}-${parts.day}`,clock=Number(parts.hour)*60+Number(parts.minute);
  const days=(await exchangeDays(today)).filter(d=>d<today||(d===today&&clock>=15*60+5));
  if(!days.length)throw Error('没有已验证的完整市场交易日');
  return days.at(-1);
}
export async function marketCalendar(session){
  const days=await exchangeDays(session);
  if(days.at(-1)!==session)throw Error('市场交易日历未覆盖指定行情日');
  return days;
}
export function validateNativePeriod(native,daily,period,session){
  const mapped=native.map(r=>({...r,date:(period==='weekly'?weekEnd(r.date):monthEnd(r.date))+'T00:00:00'})).filter(r=>day(r.date)<=session);
  const seen=new Set();for(const r of mapped){if(seen.has(r.date))throw Error('原生周/月线重复周期');seen.add(r.date);}
  const reference=aggregateBars(daily,period,session),expected=reference.at(-1)?.date;
  if(!expected||mapped.at(-1)?.date!==expected)throw Error('原生周/月线没有覆盖最新完整周期');
  for(const r of reference.slice(-3)){
    const other=mapped.find(x=>x.date===r.date);if(!other)throw Error('原生周/月与日线复权周期不匹配');
    for(const field of ['open','high','low','close'])if(Math.abs(other[field]-r[field])>Math.max(.021,r[field]*.001))throw Error('原生周/月与日线前复权价格不一致');
  }
  return mapped;
}
export function adjustMinuteBars(raw,daily,rawDaily,period,session,calendar){
  const end=session+'T15:00:00',rows=raw.filter(r=>r.date<=end).slice(-120);
  if(rows.at(-1)?.date!==end)throw Error(period+'分钟未覆盖报告日15:00完整K线');
  const factors=new Map();for(const r of daily){const base=rawDaily.find(x=>x.date===r.date);if(base)factors.set(day(r.date),r.close/base.close);}
  const allowed=[];for(const date of calendar){if(date<day(rows[0]?.date)||date>session)continue;
    for(const [hour,minute] of [[9,30],[13,0]])for(let n=period;n<=120;n+=period){const total=hour*60+minute+n;const t=`${date}T${String(Math.floor(total/60)).padStart(2,'0')}:${String(total%60).padStart(2,'0')}:00`;if(t>=rows[0].date&&t<=end)allowed.push(t);}}
  if(allowed.length!==rows.length||allowed.some((t,i)=>t!==rows[i].date))throw Error('分钟数据包含交易日/午休错位或内部缺根');
  return rows.map(r=>{const factor=factors.get(day(r.date));if(!(factor>0))throw Error('分时前复权因子缺失');return {...r,...Object.fromEntries(['open','high','low','close'].map(k=>[k,r[k]*factor]))};});
}
async function minutes(code,period){
  const url=new URL('https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData');url.search=new URLSearchParams({symbol:marketSymbol(code),scale:String(period),ma:'no',datalen:'240'});
  const text=await read(url,true),match=text.match(/=\((\[.*\])\);?/s);if(!match)throw Error('真实分钟源格式不匹配');
  return validateBars(JSON.parse(match[1]).map(r=>({...r,date:r.day})),true);
}
export async function fetchTechnicalReview(code,session,route=null){
  codeValue(code);if(!/^\d{4}-\d{2}-\d{2}$/.test(session))throw Error('交易日无效');
  const frames={},rawFrames={},source='tencent_native_qfq;minute_sina_matched_daily_factors';
  const setError=(p,e)=>{frames[p]={period:p,status:'unavailable',source,label:'数据不足，无法判断',error:e.message};};
  let daily,rawDaily,calendar;
  try{
    [daily,rawDaily,calendar]=await Promise.all([nativeBars(code,'day',session),nativeBars(code,'day',session,''),marketCalendar(session)]);
    if(daily.at(-1)?.date.slice(0,10)!==session||rawDaily.at(-1)?.date.slice(0,10)!==session)throw Error('复权/原始日线没有覆盖报告交易日');
    rawFrames.daily=daily;frames.daily=frameSnapshot(daily,'daily','tencent_qfqday',config);
  }catch(e){for(const p of PERIODS)setError(p,e);return assessTechnical({code,session,frames,rawFrames,config,route});}
  await Promise.all([
    ...[['weekly','week',300],['monthly','month',120]].map(async([p,kind,count])=>{try{const rows=validateNativePeriod(await nativeBars(code,kind,session,'qfq',count),daily,p,session);rawFrames[p]=rows;frames[p]=frameSnapshot(rows,p,'tencent_native_qfq'+kind+';validated_daily_overlap',config);}catch(e){setError(p,e);}}),
    ...[60,30,15].map(async(period)=>{try{const rows=adjustMinuteBars(await minutes(code,period),daily,rawDaily,period,session,calendar);rawFrames[String(period)]=rows;frames[String(period)]=frameSnapshot(rows,String(period),'sina_raw;matched_raw_daily_qfq_factors',config);}catch(e){setError(String(period),e);}})
  ]);
  const result=assessTechnical({code,session,frames,rawFrames,config,route});
  const rawQuote=rawDaily.at(-1),adjustedQuote=daily.at(-1);
  result.current_quote={price:rawQuote.close,session:rawQuote.date.slice(0,10),source:'tencent_raw_day',basis:'当期真实未复权日线收盘，用于私人持仓市值，不混用历史成本复权'};
  result.risk_history={session,basis:'同次前复权响应计算收益相关性，非真实组合净值或完整分红总回报',rows:daily.slice(-121).map(r=>({date:r.date.slice(0,10),close:r.close}))};
  const expectedRiskDays=calendar.filter(d=>d>=result.risk_history.rows[0].date&&d<=session);
  result.risk_history.calendar_complete=expectedRiskDays.length===result.risk_history.rows.length&&expectedRiskDays.every((d,i)=>d===result.risk_history.rows[i].date);
  if(Math.abs(rawQuote.close-adjustedQuote.close)>Math.max(.021,rawQuote.close*.001))result.current_quote=null;
  return result;
}
