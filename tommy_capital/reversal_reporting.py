"""Chinese Route C research view, separate from production TOP20 and holdings."""
from .reporting import table, cell, number


def render_pool(pool):
    lines=['## 多周期右侧反转机会池 Top20','',
           'Route C独立研究；不改变原A/B资格、50/50分数、Top20或Top10累计。Stage 1–3为观察，Stage 4仍无下单授权。',
           '口径：MACD(12,26,9)，柱=2×(DIF−DEA)；KDJ(9,3,3)，J不独立重复计分；只使用完整前复权K线。','']
    if pool.get('status') in ('unavailable','disabled'):
        return lines+['无法判断：'+cell(pool.get('reason',pool['status']))+'。','']
    lines += [f"检查时间{pool['checked_at']}；数据日{pool['session']}；Route C状态{pool['status']}；原已核验范围{pool['evaluated_count']}只，其中Route C可判{pool['evaluated_count']-pool['technical_unavailable_count']}只、未知{pool['technical_unavailable_count']}只；独立形态池{pool['candidate_count']}只，展示至多20只；原技术数据失败{pool['core_technical_missing_count']}只、未扫描{pool['core_unscanned_count']}只。",'排序：阶段→基本面核验状态→周MA55连续守住/最近6周守住数量→代码；未经样本外验证。','']
    rows=[]
    for r in pool['top20']:
        f=r['features'];m,w,d=f['monthly'],f['weekly'],f['daily'];ref=w['ma55']
        daily='突破确认' if d['breakout'] else '回踩确认' if d['retest'] else '整理等待' if d['consolidation'] else '尚待整理'
        rows.append([r['name']+'（'+r['code']+'）',number(r['price']),f['data_session'],str(r['stage'])+'：'+r['label'],
            'KD '+number(m['k'])+'/'+number(m['d'])+'；DIF/DEA '+number(m['dif'])+'/'+number(m['dea']),
            number(ref['ma55_qfq'])+'；'+ref['direction_label']+'；'+str(ref['above_count'])+'/6、连续'+str(ref['consecutive_above'])+'周',
            w['kd_state']['label']+'；DIF/DEA '+number(w['dif'])+'/'+number(w['dea']),daily,
            number(w['rvol20']['ratio20'])+'/'+number(d['rvol20']['ratio20']),
            {'verified':'已核验','pending':'待核验','blocked':'存在限制'}[r['company']['status']]+'；'+r['company']['valuation'],
            number(d['support_qfq'])+'/'+number(d['resistance_qfq']),cell('；'.join(r['confirmation_missing'][:3]))])
    lines+=table(['股票','收盘价','日线截止','机会阶段','完整月KD/MACD','周MA55方向/位置','周KD/MACD','日线状态','周/日RVOL20','基本面/估值','支撑/压力参考','主要待确认'],rows or [['—','—','—','无符合当前研究形态的股票',*['—']*8]])
    for r in pool['top20']:
        f=r['features'];m,w,d=f['monthly'],f['weekly'],f['daily']
        lines += ['**'+cell(r['name'])+'（'+r['code']+'）**：'+cell('；'.join(r['discovery_reasons']))+'。',
            '完整周期：月'+m['last_bar'][:10]+'，周'+w['last_bar'][:10]+'，日'+d['last_bar'][:10]+'；月J '+number(m['j'])+
            '，月MA55距离'+(number(m['ma55_distance_pct'])+'%' if m['ma55_distance_pct'] is not None else '缺少足够历史，无法判断')+'。',
            '日线：首轮上涨'+number(d['first_leg_advance_pct'])+'%，回调'+number(d['pullback_pct'])+'%，MA5/13间距'+number(d['ma5_13_gap_pct'])+
            '%，回调活跃度比'+number(d['pullback_volume_ratio'])+'，CLV '+number(d['clv'])+'。'+d['efficiency_basis']+'。',
            '待确认：'+cell('；'.join(r['confirmation_missing']))+'。','']
    lines += ['基本面与估值为单独核验栏；债务、商誉、诉讼、经营改善无已核对证据时保持未知。连锁药房另须门店效率与租赁负债核验；不套用到其他行业。完整逐股特征及风险/失效记录见同次JSON附件。','']
    return lines
