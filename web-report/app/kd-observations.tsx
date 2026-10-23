"use client";
import {useState} from 'react';
import {KD_PERIODS,KD_NAMES} from '@/lib/kd-state.mjs';
type Row=Record<string,any>;
const periodNames:Record<string,string>=KD_NAMES;
const n=(x:unknown)=>typeof x==='number'&&Number.isFinite(x)?x.toFixed(2):'—';
const groupLabels:Record<string,string>={bullish:'偏多',bearish:'偏空',mixed:'未同向',conflict:'组内分歧',unavailable:'数据不足'};
export default function KDObservations({scope,reviews,expectedSession}:{scope:Row[],reviews:Record<string,Row>,expectedSession:string}){
 const [filter,setFilter]=useState('all');
 const visible=scope.filter(row=>{const m=reviews[row.code]?.kd_monitor;if(filter==='all')return true;if(filter==='missing')return !m?.data_complete;return Object.values(m?.frames||{}).some((f:any)=>['early_turn','low_golden_cross','golden_cross','bearish_turn','momentum_weakening'].includes(f.state));});
 return <section className="kd-observations" aria-labelledby="kd-title"><div className="section-heading"><div><span className="eyebrow">COMPLETED BARS · NO RANKING WEIGHT</span><h3 id="kd-title">多周期K/D状态观察</h3></div><label>显示<select value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">全部观察股</option><option value="turn">转折、交叉或减弱</option><option value="missing">存在数据缺口</option></select></label></div>
 <p className="muted">月周定方向，日线确认，分钟执行。仅描述完整K线，不增加评分或交易资格。J=3K−2D，不和K−D重复加分；120/60分钟为同一结构组，30/15分钟为同一执行组。</p>
 <div className="table-scroll"><table><thead><tr><th>股票／来源</th>{KD_PERIODS.map(p=><th key={p}>{periodNames[p]}</th>)}<th>综合观察</th></tr></thead><tbody>{visible.map(row=>{const review=reviews[row.code],m=review?.kd_monitor,stale=!!review&&review.session!==expectedSession;return <tr key={row.code}><td><b>{row.name||row.code}</b><small>{row.code} · {row.sources?.join('／')}</small></td>{KD_PERIODS.map(p=>{const f=m?.frames?.[p];return <td key={p}><b>{f?.label||'待生成K/D轨迹'}</b>{f?.status==='ok'&&<><small>K {n(f.previous.k)}→{n(f.current.k)}<br/>D {n(f.previous.d)}→{n(f.current.d)}</small><small>K−D {n(f.current.gap)}<br/>{f.last_bar?.replace('T',' ')}</small>{f.weak_cross&&<small>交叉间距小</small>}</>}</td>})}<td><b>{stale?'旧评估，等待更新':m?.label||'刷新全部技术面后生成'}</b><small>{m?.data_complete?'七周期K/D轨迹齐全':'缺失保持未知，不作为无信号'}</small></td></tr>})}</tbody></table></div>
 {!visible.length&&<p className="empty">当前筛选范围没有对应观察记录。</p>}
 {visible.map(row=>{const review=reviews[row.code],m=review?.kd_monitor;if(!m)return null;return <details key={row.code}><summary>{row.name||row.code} · K/D轨迹与分组依据</summary><p className="fineprint">实际检查 {review.checked_at}；行情日 {review.session}；{m.version}。指标交叉只确认交叉发生，不代表MACD严格背离、价格反转或可以买入。</p>
 <p>{m.groups?.map((g:Row)=>`${g.label}：${groupLabels[g.direction]||g.direction}${g.correlated?'（相关周期合并观察）':''}`).join('；')}。</p>
 {KD_PERIODS.map(p=>{const f=m.frames[p];return <div className="signal" key={p}><h4>{periodNames[p]} · {f.label}</h4>{f.status!=='ok'?<p>{f.error}</p>:<><p>K变动{n(f.k_change)}点，D变动{n(f.d_change)}点，K−D变动{n(f.gap_change)}点。最近轨迹内交叉：{f.last_cross_in_history?`${f.last_cross_in_history.type==='golden_cross'?'金叉':'死叉'} · ${f.last_cross_in_history.date}`:'无交叉'}。</p><div className="table-scroll"><table><thead><tr><th>完整K线</th><th>K</th><th>D</th><th>J（派生）</th><th>K−D</th></tr></thead><tbody>{f.history.map((h:Row)=><tr key={h.date}><td>{h.date.replace('T',' ')}</td><td>{n(h.k)}</td><td>{n(h.d)}</td><td>{n(h.j)}</td><td>{n(h.gap)}</td></tr>)}</tbody></table></div><p className="fineprint">来源 {f.source}；核验历史{f.source_bar_count}根，完整截止{f.complete_through}。小于配置变化阈值的波动不冒充明确上行/下行；低位金叉按交叉前一根K、D均不高于20识别。</p></>}</div>})}</details>})}
 </section>;
}
