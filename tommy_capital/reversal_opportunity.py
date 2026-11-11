"""Independent Route C research. No production score, entry or T mutations.

Reuses completed, consistently adjusted frames and the production indicators.
Stage ordering is lexicographic, not a new additive multi-timeframe score.
"""
import copy
import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .patterns import macro_profile, weekly_ma55_observation
from .technical import period_volume_observation
from .kd_state import classify_kd
from .data import DataError, SHANGHAI
from .scoring import finite, qualified_macd, balanced_score, financial_sector

VERSION = "right_side_reversal_v1"
POLICY_PATH = Path(__file__).resolve().parents[1] / "reversal-opportunity-config.json"
STAGES = {1: "长期弱势修复观察", 2: "周线趋势转强观察", 3: "日线整理与二次启动观察",
          4: "符合既有执行条件（研究确认）", 5: "结构失效"}
PARAMETERS = {"macd": [12, 26, 9], "macd_histogram_multiplier": 2, "kdj": [9, 3, 3], "adjust": "qfq"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate_policy(policy):
    if not isinstance(policy,dict) or type(policy.get('schema')) is not int or policy.get("schema") != 1 or type(policy.get("enabled")) is not bool:
        raise ValueError("Route C配置版本/开关无效")
    if any(policy.get(k) != PARAMETERS[k] for k in ("macd", "kdj", "macd_histogram_multiplier")):
        raise ValueError("Route C须统一为现有MACD(12,26,9)×2及KDJ(9,3,3)，不混用截图参数")
    if policy.get("structure_months") != [6, 12, 18]:
        raise ValueError("Route C结构窗口须同时保留6/12/18个完整月")
    minimum = policy.get("minimum_bars", {})
    if not isinstance(minimum,dict) or any(type(minimum.get(k)) is not int or minimum[k] < n for k, n in (("daily", 250), ("weekly", 60), ("monthly", 24))):
        raise ValueError("Route C不得降低原历史长度")
    integers = {"minimum_decline_months": (2, 12), "pivot_right_months": (2, 6),
                "first_leg_sessions": (30, 120), "consolidation_sessions": (5, 20), "risk_review_max_age_days": (1, 225)}
    for key, (lower, upper) in integers.items():
        if type(policy.get(key)) is not int or not lower <= policy[key] <= upper:
            raise ValueError("Route C参数无效："+key)
    for key in ("minimum_decline_pct", "kd_min_change", "low_tolerance_pct", "first_leg_advance_pct",
                "maximum_pullback_pct", "maximum_ma_gap_pct", "maximum_extension_pct", "maximum_extension_atr",
                "breakout_rvol", "breakout_clv_location", "retest_rvol"):
        value = policy.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 100:
            raise ValueError("Route C阈值无效："+key)
    if not 0 < policy["breakout_clv_location"] <= 1 or policy["breakout_rvol"] < 1 or policy["retest_rvol"] < 1:
        raise ValueError("Route C量价确认阈值无效")
    return policy


def load_policy(path=POLICY_PATH):
    return validate_policy(json.loads(Path(path).read_text(encoding="utf-8")))


def unavailable(reason):
    return {"version": VERSION, "status": "unavailable", "reason": str(reason), "label": "数据不足，无法判断",
            "parameters": PARAMETERS, "stage": None, "influences_ranking": False, "orders_authorized": False}


def observe(daily, weekly, monthly, session, cutoff, policy, parameters=None):
    """Inputs are calculated once by strategic_technical; never request prices.

    Explicitly reject mismatched calculated columns and forming periods. A
    missing monthly MA55 stays unknown; it is not fabricated from 24 months.
    """
    policy = validate_policy(policy)
    if parameters is not None and parameters != PARAMETERS:
        raise DataError('MACD/KDJ参数不一致')
    frames = {"daily": daily, "weekly": weekly, "monthly": monthly}
    session, cutoff = pd.Timestamp(session), pd.Timestamp(cutoff)
    from .technical import indicators
    for period, frame in frames.items():
        if len(frame) < policy["minimum_bars"][period] or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise DataError(period+"完整历史不足/时间重复乱序")
        limit = session if period == "daily" else cutoff
        if frame.index[-1].normalize() > limit or period == "daily" and frame.index[-1].normalize() != session:
            raise DataError(period+"含尚未完成或未覆盖目标日的K线")
        if frame.volume.iloc[-1]<=0 or period_volume_observation(frame)['status']!='ok':
            raise DataError(period+'成交量缺失/零成交，无法判断Route C')
        if not all(finite(frame.iloc[-1].get(k)) is not None for k in ('open','close','high','low','atr14','k','d','j','dif','dea','macd')) or frame.atr14.iloc[-1]<=0:
            raise DataError(period+'指标缺失/非有限或ATR为零，无法判断')
        if period != "daily" and (frame.index[-1] != frame.index[-1].to_period("M").end_time.normalize() if period == "monthly" else frame.index[-1].weekday() != 4):
            raise DataError("完整周/月标签不符合既有聚合口径")
        # Compare only the indicator tail; provenance is explicit and not inferred
        # from a screenshot or supplied MACD label.
        if parameters is None:
            recomputed = indicators(frame)
            if not all(np.allclose(frame[k].tail(3), recomputed[k].tail(3), rtol=1e-10, atol=1e-10, equal_nan=True)
                       for k in ("dif", "dea", "macd", "k", "d", "j")):
                raise DataError("MACD/KDJ参数或数值口径不一致")
    a, b, c = monthly.iloc[-3], monthly.iloc[-2], monthly.iloc[-1]
    windows = []
    for length in policy["structure_months"]:
        part = monthly.tail(length)
        low_at = int(part.low.to_numpy().argmin())
        prior = part.iloc[:low_at]
        peak_at = int(prior.high.to_numpy().argmax()) if len(prior) else None
        decline = (1-float(part.low.iloc[low_at])/float(prior.high.max()))*100 if len(prior) else 0.
        right = len(part)-1-low_at
        windows.append({"months": length, "decline_pct": decline, "decline_months": low_at-peak_at if peak_at is not None else 0,
                        "low_at": part.index[low_at].date().isoformat(), "low_qfq": float(part.low.iloc[low_at]),
                        "low_confirmed_at": part.index[low_at+policy["pivot_right_months"]].date().isoformat() if right >= policy["pivot_right_months"] else None,
                        "right_completed_months": right,
                        "relative_position": float((c.close-part.low.min())/(part.high.max()-part.low.min())) if part.high.max()>part.low.min() else None})
    valid = [x for x in windows if x["decline_pct"] >= policy["minimum_decline_pct"] and
             x["decline_months"] >= policy["minimum_decline_months"] and x["low_confirmed_at"]]
    anchor = max(valid, key=lambda x: x["months"]) if valid else None
    low_stable = bool(monthly.low.tail(3).min() >= monthly.low.iloc[-6:-3].min()*(1-policy["low_tolerance_pct"]/100))
    kd_up = bool(c.k > c.d and c.k-b.k >= policy["kd_min_change"] and b.k >= a.k and c.d >= b.d-policy["kd_min_change"])
    monthly_macd = bool(c.dif > b.dif and (c.dif > c.dea or c.macd > b.macd))
    m_profile = macro_profile(monthly, "monthly")
    monthly_repair = bool(anchor and low_stable and kd_up and monthly_macd and c.close > c.ma5)
    ref = weekly_ma55_observation(weekly)
    if ref["status"] != "ok":
        raise DataError("周MA55完整轨迹不可用")
    w, wp = weekly.iloc[-1], weekly.iloc[-2]
    weekly_conditions = {"above_ma5_13": bool(w.close>w.ma5 and w.close>w.ma13),
        "ma5_13_up": bool(w.ma5>wp.ma5 and w.ma13>=wp.ma13),
        "higher_lows": bool(weekly.low.tail(3).min()>weekly.low.iloc[-6:-3].min()),
        "macd_improving": bool(w.dif>w.dea and w.macd>0 and w.dif>=wp.dif),
        "dif_above_zero": bool(w.dif>0), "kd_bullish": bool(w.k>w.d and w.k>=wp.k-policy["kd_min_change"])}
    weekly_turn = ref["latest_week_above"] and weekly_conditions["above_ma5_13"] and weekly_conditions["macd_improving"] and weekly_conditions["kd_bullish"]
    weekly_strong = bool(weekly_turn and ref["direction"] in ("rising", "flat") and ref["above_count"]>=3 and ref["consecutive_above"]>=2 and
                         weekly_conditions["higher_lows"] and weekly_conditions["ma5_13_up"] and weekly_conditions['dif_above_zero'])
    d, dp = daily.iloc[-1], daily.iloc[-2]
    history = daily.iloc[-policy["first_leg_sessions"]:-3]
    peak_at = int(history.high.to_numpy().argmax())
    before = history.iloc[:peak_at+1]
    advance = float((history.high.iloc[peak_at]/before.low.min()-1)*100)
    peak_date = history.index[peak_at]
    after_peak = daily.loc[daily.index>peak_date]
    recent = daily.tail(policy["consolidation_sessions"])
    prior = daily.iloc[-policy["consolidation_sessions"]-1:-1]
    low_support = float(daily.low.iloc[-policy['consolidation_sessions']-2:-2].min())
    volume = period_volume_observation(daily)
    if volume["status"] != "ok":
        raise DataError("日线量比不可判断，不能填零")
    location = float((d.close-d.low)/(d.high-d.low)) if d.high>d.low else None
    clv = 2*location-1 if location is not None else None
    upper_shadow = (daily.high-daily[["open", "close"]].max(axis=1))/(daily.high-daily.low).replace(0, np.nan)
    ma_gap = float(abs(d.ma5/d.ma13-1)*100)
    pullback = float((1-after_peak.low.min()/history.high.iloc[peak_at])*100)
    stable = bool(recent.low.tail(3).min()>=recent.low.iloc[:-3].min()*(1-policy["low_tolerance_pct"]/100))
    quiet = float(daily.volume.iloc[-4:-1].mean()/daily.volume.iloc[-24:-4].mean()) if daily.volume.iloc[-24:-4].mean()>0 else None
    d_profile = macro_profile(daily, "daily")
    consolidation = bool(advance>=policy["first_leg_advance_pct"] and 3<=len(after_peak)<=policy["first_leg_sessions"] and
        0<=pullback<=policy["maximum_pullback_pct"] and stable and ma_gap<=policy["maximum_ma_gap_pct"] and d.close>=d.ma13-.5*d.atr14 and
        25<=d.k<=80)
    breakout = bool(d.close>prior.high.max() and volume["ratio20"]>=policy["breakout_rvol"] and location is not None and location>=policy["breakout_clv_location"])
    retest = bool(d_profile.get("pullback_reclaimed") and quiet is not None and quiet<=1 and volume["ratio20"]>=policy["retest_rvol"])
    extension = bool((d.close/d.ma13-1)*100>=policy["maximum_extension_pct"] and (d.close-d.ma13)/d.atr14>=policy["maximum_extension_atr"])
    stagnation = bool(advance>=policy["first_leg_advance_pct"] and d.close>=history.high.max()*.95 and volume["ratio20"]>=policy["breakout_rvol"] and
                      (upper_shadow.tail(5)>=.5).sum()>=2)
    support_lost = bool(d.close<low_support and dp.close<low_support and d.close<d.ma13)
    weekly_broken = bool(not ref["latest_week_above"] and ref["direction"]=="falling" and not weekly_conditions["ma5_13_up"])
    old_platform=float(daily.high.iloc[-policy['consolidation_sessions']-2:-2].max())
    failed_breakout = bool(dp.close>old_platform and d.close<old_platform)
    momentum_recovering=bool(d.macd>dp.macd+.02*d.atr14 and d.dif>=dp.dif)
    kd_recovering=bool(d.k>d.d and d.k>dp.k and daily.k.iloc[-6:-1].between(25,75).any())
    invalid = bool(support_lost or weekly_broken)
    stage = (5 if invalid and anchor else 3 if monthly_repair and weekly_strong and (consolidation or breakout or retest) else
             2 if monthly_repair and weekly_turn else 1 if monthly_repair else None)
    # Reuse the exact K/D classifier with column iteration instead of pandas
    # iterrows across every full-market history. No new classifier or indicator.
    kd = {period: classify_kd([
        {"date":t.isoformat(),"k":finite(k),"d":finite(v),"j":finite(3*k-2*v)}
        for t,k,v in zip(frame.index,frame.k,frame.d)],period,"production_calculated_qfq",frame.index[-1].isoformat())
        for period,frame in frames.items()}
    def brief(frame):
        row, prev = frame.iloc[-1], frame.iloc[-2]
        return {"last_bar": frame.index[-1].isoformat(), "bar_count": len(frame), "close_qfq": float(row.close),
                "k": float(row.k), "d": float(row.d), "j": float(row.j), "dif": float(row.dif), "dea": float(row.dea),
                "histogram": float(row.macd), "previous_histogram": float(prev.macd),
                "ma_qfq": {str(n): finite(row.get('ma'+str(n))) for n in (5,13,55)},
                "rvol20": period_volume_observation(frame), "kd_state": kd[{id(daily):'daily',id(weekly):'weekly',id(monthly):'monthly'}[id(frame)]]}
    return {"version": VERSION, "status": "ok", "parameters": PARAMETERS, "policy_sha256": digest(policy),
        "data_session": session.date().isoformat(), "complete_through": cutoff.date().isoformat(), "stage": stage,
        "episode": anchor["low_at"] if anchor else None, "structure_windows": windows, "long_decline_repair": monthly_repair,
        "monthly": {**brief(monthly), "kd_repair": kd_up, "macd_repair": monthly_macd, "lows_stable": low_stable,
                    "ma55_distance_pct": float((c.close/c.ma55-1)*100) if finite(c.ma55) is not None and c.ma55>0 else None,
                    "long_ma55_missing": finite(c.ma55) is None, "profile": m_profile},
        "weekly": {**brief(weekly), "ma55": ref, "conditions": weekly_conditions, "turn": bool(weekly_turn), "strong": weekly_strong,
            "structure_label": "下降MA55上方的早期反转" if ref["direction"]=="falling" and ref["latest_week_above"] else
                "上升MA55与回踩确认" if ref["direction"]=="rising" and ref["retest"] else "MA55站稳修复" if weekly_strong else "MA55尚未站稳"},
        "daily": {**brief(daily), "first_leg_advance_pct": advance, "first_peak_at": peak_date.date().isoformat(),
            "consolidation": consolidation, "pullback_pct": pullback, "ma5_13_gap_pct": ma_gap, "lows_stable": stable,
            "pullback_volume_ratio": quiet, "breakout": breakout, "retest": retest, "failed_breakout": failed_breakout,
            "momentum_recovering":momentum_recovering,"kd_recovering":kd_recovering,
            "support_qfq": low_support, "resistance_qfq": float(prior.high.max()), "clv": clv,
            "signed_atr_per_rvol": float((d.close-dp.close)/d.atr14/max(volume['ratio20'],.25)),
            "efficiency_basis": "方向性ATR推进/有下限的自身RVOL；辅助描述，不是资金净流入、换手率或Amihud",
            "chase_risk": extension, "supply_disagreement": stagnation, "support_lost": support_lost, "weekly_broken": weekly_broken},
        "missing": ["monthly.ma55"] if finite(c.ma55) is None else [],
        "monthly_chase_risk":bool(m_profile.get('chase_risk')),
        "influences_ranking": False, "orders_authorized": False}


def safe_observe(*args, **kwargs):
    try:
        return observe(*args, **kwargs)
    except Exception as exc:
        # Optional feature failure cannot discard an otherwise valid A/B card.
        return unavailable(type(exc).__name__+": "+str(exc))


def company_check(row, session, config, study=None, review=None, policy=None):
    """Unknown debt/goodwill/litigation is pending, never a healthy-company claim."""
    from .strategy import base_checks
    policy = policy or load_policy()
    missing, adverse = [], []
    for key in ("revenue_ytd", "profit_ytd", "core_profit_ytd", "cfo_ytd", "profit_ttm", "core_profit_ttm", "cfo_ttm", "previous_same_cfo_ytd"):
        if finite(row.get(key)) is None: missing.append(key)
    period, announced = pd.to_datetime(row.get("period"), errors="coerce"), pd.to_datetime(row.get("announced_at"), errors="coerce")
    if pd.isna(period) or pd.isna(announced) or announced.date()>date.fromisoformat(session):
        missing.append("known_financial_publication")
    if row.get("quality_statement_matched") is not True: missing.append("statement_version_match")
    reviewed = review or {}
    sources = reviewed.get("sources", [])
    required = ["business_continuity", "debt", "goodwill", "litigation", "operating_improvement"]
    pharmacy = any(s in str(row.get('name',''))+str(row.get('industry','')) for s in ('药房','药店','医药商业'))
    if pharmacy: required += ["store_productivity", "lease_liabilities"]
    valid_review = False
    verified_sources={(s.get('url'),s.get('sha256')) for s in row.get('leading_evidence',{}).get('records',[]) if s.get('verification')=='verified'}
    try:
        review_date = date.fromisoformat(reviewed["as_of"])
        valid_review = (0<=(date.fromisoformat(session)-review_date).days<=policy['risk_review_max_age_days'] and bool(sources) and
            all(s.get('verification')=='verified' and s.get('url','').startswith('https://') and
                re.fullmatch('[0-9a-f]{64}',s.get('sha256','')) and (s.get('url'),s.get('sha256')) in verified_sources and
                date.fromisoformat(s['published_at'][:10])<=review_date for s in sources))
    except (KeyError,ValueError,TypeError): pass
    for key in required:
        if not valid_review or type(reviewed.get(key)) is not bool: missing.append("review."+key)
        elif reviewed[key] is False: adverse.append(key)
    valuation = (study or {}).get("valuation", {})
    if valuation.get("status") != "complete": missing.append("evidence_intrinsic_valuation")
    overvalued = str(valuation.get('price_judgment','')).startswith('高估') or (finite(row.get('pe_ttm')) is not None and row['pe_ttm']>config['max_pe_ttm'])
    if overvalued: adverse.append('valuation_expensive')
    a_reasons = base_checks(row,config,date.fromisoformat(session))
    b = row.get('financial_routes',{}).get('route_b_passed') is True
    if financial_sector(row):
        # Preserve the production model's sector-specific review requirement.
        missing.append('fundamental.financial_sector_risk_review')
    if row.get('liquidity_deferred',False):
        average=finite(row.get('technical',{}).get('average_turnover20_cny'))
        if average is None: missing.append('average_turnover20_cny')
        elif average<config['min_turnover_cny']: adverse.append('最近20完整交易日流动性不足')
    if a_reasons and not b:
        for reason in a_reasons:
            (missing if any(s in reason for s in ('缺失','过旧')) else adverse).append(reason)
    margin=finite(valuation.get('safety_margin'))
    if valuation.get('status')=='complete' and (margin is None or margin<config['min_forward_margin_pct']/100 or valuation.get('model_disagreement')):
        adverse.append('估值安全边际不足或模型分歧')
    ready = not missing and not adverse and str(valuation.get('price_judgment','')).startswith(('低估','合理'))
    return {"status": "verified" if ready else "blocked" if adverse else "pending", "ready":ready,
            "fundamental_route":"A" if not a_reasons else "B" if b else None,
            "missing":sorted(set(missing)),"adverse":adverse,"pharmacy_review":pharmacy,
            "metrics":{k:finite(row.get(k)) for k in ('revenue_yoy','profit_ttm','core_profit_ttm','cfo_ttm','pe_ttm','pb','industry_pe_ratio')},
            "valuation":valuation.get('price_judgment','无法判断'),"sources":sources if valid_review else [],
            "basis":"财报/估值/经营风险分别核验；相对PE不替代内在价值；技术形态不能证明业务改善"}


def evaluate_row(row, report, policy, review=None, valuation_study=None):
    f=row.get('technical',{}).get('reversal_features') or unavailable('未计算Route C完整周期特征')
    if f.get('status')!='ok' or f.get('version')!=VERSION or f.get('parameters')!=PARAMETERS or f.get('policy_sha256')!=digest(policy):
        return {"code":row['code'],"name":row.get('name'),"status":"unavailable","reason":f.get('reason','Route C特征版本/口径未知')}
    stage=f['stage']
    if stage is None:
        return {'code':row['code'],'name':row.get('name'),'status':'not_detected','stage':None}
    study=valuation_study or (report.get('investment_research') or {}).get('rows',{}).get(row['code'])
    company=company_check(row,report['session'],report['config'],study,review,policy)
    if f.get('episode') and company['adverse'] and any(x in company['adverse'] for x in ('business_continuity','litigation')):
        stage=5
    ref=f['weekly']['ma55']
    high_cycle_top=any(qualified_macd(s,'bearish') for p in ('monthly','weekly') for s in row.get('technical',{}).get('timeframes',{}).get(p,{}).get('signals',[]))
    original_ma55=(ref['direction'] in ('rising','flat') and ref['latest_week_above'] and f['daily']['close_qfq']>ref['ma55_qfq'] and
        ref['above_count']>=report['config']['weekly_ma55_min_above_count'] and ref['consecutive_above']>=report['config']['weekly_ma55_min_consecutive'])
    financial_guard_ok=report.get('financial_integrity',{}).get('status')=='healthy'
    # C must not reapply the A/B monthly-J window: that is precisely the gap it
    # studies. Reuse production input validation read-only, never write its
    # scores/eligibility back into the original card or trading/T permissions.
    inputs=balanced_score(row,row.get('technical',{}),company['fundamental_route'],row.get('leading'))
    original_structure=(row.get('technical',{}).get('weekly_structure',{}).get('confirmed') is True and
                        row.get('technical',{}).get('daily_trend') is True)
    entry=(inputs['score_inputs_complete'] and original_structure and financial_guard_ok and company['ready'] and original_ma55 and not f['missing'] and not f['monthly_chase_risk'] and not high_cycle_top and
           f['daily']['momentum_recovering'] and f['daily']['kd_recovering'] and
           (f['daily']['breakout'] or f['daily']['retest']) and not any(f['daily'][k] for k in ('chase_risk','supply_disagreement','failed_breakout')))
    if stage==3 and entry: stage=4
    blockers=[]
    if f['monthly_chase_risk']: blockers.append('月线扩张并伴随动能转弱')
    if high_cycle_top: blockers.append('已有原引擎确认的月/周MACD顶背离，暂不确认启动')
    if not financial_guard_ok: blockers.append('财报保护尚未通过，不能确认阶段4')
    if not inputs['score_inputs_complete']: blockers.append('原数据完整性要求待齐：'+','.join(inputs['score_missing_inputs']))
    if not original_structure: blockers.append('原周线结构/日线趋势确认未通过')
    if f['missing']: blockers.append('长期均线历史待齐：'+','.join(f['missing']))
    if f['weekly']['ma55']['direction']=='falling': blockers.append('周MA55仍下降；站上线不代表均线已经上行')
    if not f['weekly']['strong']: blockers.append('周线站稳及趋势证据不足')
    if not (f['daily']['breakout'] or f['daily']['retest']): blockers.append('日线突破/真实回踩确认尚未完成')
    if not f['daily']['momentum_recovering'] or not f['daily']['kd_recovering']: blockers.append('日MACD/KD尚未完成中性整理后的转强；柱体缩短本身不是启动确认')
    for key,label in [('chase_risk','日线过度延伸，存在追高风险'),('supply_disagreement','高位供需分歧观察'),('failed_breakout','日线突破失败')]:
        if f['daily'][key]: blockers.append(label)
    if row.get('strategic_eligible') is not True: blockers.append('原A/B资格仍独立保留；C研究确认不授予原交易/做T资格')
    blockers += ['财报/风险待核验：'+x for x in company['missing']]+['基本面/估值限制：'+x for x in company['adverse']]
    risk_watch=any(f['daily'][k] for k in ('chase_risk','supply_disagreement','failed_breakout')) or f['monthly_chase_risk'] or high_cycle_top
    return {"code":row['code'],"name":row.get('name'),"price":finite(row.get('price')),"status":"risk_watch" if stage in (1,2,3) and risk_watch else "research" if stage in (1,2,3,4) else "invalidated" if stage==5 else "not_detected",
            "stage":stage,"label":STAGES.get(stage,'未形成修复结构'),"features":f,"company":company,"confirmation_missing":blockers,
            "discovery_reasons":["6/12/18月的先跌后低点确认，月KD及MACD开始修复"]+([f['weekly']['structure_label']] if stage and stage>=2 else [])+
                (["首轮上涨后整理/回踩或突破观察"] if stage in (3,4) else []),
            "production_eligible":row.get('eligible') is True,"production_reasons":row.get('reasons',[]),
            "execution_permission":False,"influences_ranking":False,"orders_authorized":False}


def build_pool(report, policy=None, reviews=None, assumptions=None, valuation_policy=None):
    policy=validate_policy(policy) if policy is not None else load_policy()
    if report.get('status') not in ('complete','partial'):
        return {**unavailable('核心扫描尚未完成，Route C无法判断'), 'top20':[], 'rows':[], 'candidate_count':0}
    rows=report.get('rankings',[])+report.get('observations',[])
    evaluated=[]
    if policy['enabled']:
        from .intrinsic_value import evaluate_intrinsic
        for row in rows:
            try:
                study=None
                if row.get('technical',{}).get('reversal_features',{}).get('stage') is not None:
                    study={'valuation':evaluate_intrinsic(row,(assumptions or {}).get(row['code'],{}).get('models',[]),
                                 row.get('leading_evidence',{}),report['session'],valuation_policy)}
                evaluated.append(evaluate_row(row,report,policy,(reviews or {}).get(row['code']),study))
            except Exception as exc:
                evaluated.append({'code':row['code'],'name':row.get('name'),'status':'unavailable','reason':str(exc)})
    opportunities=[r for r in evaluated if r.get('stage') in (1,2,3,4) and r['status']=='research']
    def order(r):
        f=r['features']; w=f['weekly']['ma55']
        return (-r['stage'], 0 if r['company']['ready'] else 1 if r['company']['status']=='pending' else 2,
                -w['consecutive_above'],-w['above_count'],r['code'])
    opportunities.sort(key=order)
    return {"version":VERSION,"policy":policy,"policy_sha256":digest(policy),"parameters":PARAMETERS,
            "session":report.get('session'),"checked_at":datetime.now(SHANGHAI).isoformat(),"source_finished_at":report.get('finished_at'),
            "status":"disabled" if not policy['enabled'] else "partial" if report.get('status')=='partial' or any(r['status']=='unavailable' for r in evaluated) or report.get('coverage',{}).get('technical_failed',0) or report.get('coverage',{}).get('unscanned',0) or any(not r['company']['ready'] for r in opportunities) else "complete",
            "evaluated_count":len(evaluated),"technical_unavailable_count":sum(r['status']=='unavailable' for r in evaluated),
            "core_technical_missing_count":report.get('coverage',{}).get('technical_failed',0),
            "core_unscanned_count":report.get('coverage',{}).get('unscanned',0),
            "candidate_count":len(opportunities),"display_limit":20,"top20":opportunities[:20],
            "company_verified_count":sum(r['company']['ready'] for r in opportunities),
            "rows":evaluated,"ranking_method":"stage_then_verified_company_then_ma55_holding_then_code; no additive score",
            "review_inputs":reviews or {},"valuation_assumptions":assumptions or {},"valuation_policy":valuation_policy,
            "validation_status":"unvalidated_out_of_sample","influences_ranking":False,"orders_authorized":False}


def attach_pool(report, policy=None, reviews=None):
    try:
        root=Path(__file__).resolve().parents[1]
        old=report.get('reversal_opportunities') or {}
        def read_inputs(path):
            file=root/path
            return json.loads(file.read_text()) if file.exists() else {'schema':1,'companies':{}}
        review_inputs=reviews if reviews is not None else read_inputs('operations/reversal-reviews.json').get('companies',{})
        inputs=read_inputs('operations/investment-research.json').get('companies',{})
        investment=root/'investment-config.json'
        valuation_policy=json.loads(investment.read_text()) if investment.exists() else None
        report['reversal_opportunities']=build_pool(report,policy,review_inputs,inputs,valuation_policy)
    except Exception as exc:
        report['reversal_opportunities']={**unavailable(type(exc).__name__+': '+str(exc)),"top20":[],"rows":[],"candidate_count":0}
    return report['reversal_opportunities']
