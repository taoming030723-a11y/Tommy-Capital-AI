"""Evidence-based B subtype for still-loss-making, improving financial statements."""
import math
import pandas as pd


def loss_recovery_check(row,config,today):
    reasons=[]
    def number(key):
        try:
            value=float(row.get(key))
            return value if math.isfinite(value) else None
        except (ValueError,TypeError):
            return None
    if not config.get("loss_recovery_enabled",False):
        reasons.append("亏损收窄子路线未启用")
    if any(word in str(row.get("name","")).upper() for word in ("ST","退")) or row.get("quote_clock_ok") is False:
        reasons.append("风险警示或行情时效未通过")
    profit=number("profit_ttm")
    if profit is None or profit>=0:
        reasons.append("非已核验TTM亏损")
    for field in ("price","market_cap","pb","revenue_ttm"):
        if number(field) is None or number(field)<=0:
            reasons.append(field+"缺失/非正")
    growth=number("revenue_yoy")
    if growth is None or growth<config.get("loss_recovery_min_revenue_growth_pct",0):
        reasons.append("营收同比未达收窄路线要求")
    if number("pb") and number("pb")>config["max_pb"]:
        reasons.append("PB超过风险上限")
    ps=number("market_cap")/number("revenue_ttm") if number("market_cap") and number("revenue_ttm") else None
    if ps is None or ps>config.get("loss_recovery_max_ps",3):
        reasons.append("真实市值/TTM营收估值未通过；不把PS冒充EV/Sales")
    if not row.get("liquidity_deferred",False) and (number("turnover") is None or number("turnover")<config["min_turnover_cny"]):
        reasons.append("流动性未通过")
    period=pd.to_datetime(row.get("period"),errors="coerce")
    if pd.isna(period) or not 0<=(today-period.date()).days<=config["max_finance_age_days"]:
        reasons.append("财报期缺失/过旧")
    periods=row.get("loss_recovery_periods",[])
    # A left-joined stock without financial history has NaN in this object column.
    if not isinstance(periods,list) or any(not isinstance(p,dict) for p in periods):
        periods=[]
    reductions=[]
    if len(periods)!=2:
        reasons.append("缺少连续两个报告期及各自上年同期的完整利润/扣非/现金流原始总额")
    else:
        dates=[pd.to_datetime(p.get("period"),errors="coerce") for p in periods]
        if any(pd.isna(d) or not d.is_quarter_end for d in dates) or dates[0]+pd.offsets.QuarterEnd()!=dates[1] or pd.isna(period) or dates[-1]!=period:
            reasons.append("两个报告期不连续或非最新报告期")
        for p in periods:
            try:
                current=pd.Timestamp(p["period"])
                if pd.Timestamp(p.get("same_period"))!=current-pd.DateOffset(years=1):
                    raise ValueError("未匹配上年同一报告期")
                values=[float(p[k]) for k in ("profit","prior_profit","core_profit","prior_core_profit","revenue","prior_revenue","cfo","prior_cfo")]
                if not all(math.isfinite(v) for v in values) or p["quality_matched"] is not True:
                    raise ValueError("财务质量总额未核验")
                net,old,core,old_core,revenue,old_revenue,cfo,old_cfo=values
                reduction=(net-old)/abs(old) if old<0 else -1
                core_reduction=(core-old_core)/abs(old_core) if old_core<0 else -1
                passed=(old<net<0 and old_core<core<0 and revenue>0 and old_revenue>0 and
                    reduction*100>=config.get("loss_recovery_min_narrowing_pct",10) and
                    core_reduction*100>=config.get("loss_recovery_min_narrowing_pct",10) and
                    core/revenue>old_core/old_revenue and cfo>=old_cfo)
                if not passed:
                    reasons.append(p["period"]+"归母/扣非亏损、利润率或同比现金流未同步改善")
                reductions.append(min(reduction,core_reduction))
            except (ValueError,TypeError,KeyError,ZeroDivisionError):
                reasons.append("亏损收窄财务输入缺失，不能给缺失值补零")
        for record_key,row_key in (("profit","profit_ytd"),("core_profit","core_profit_ytd"),("revenue","revenue_ytd"),("cfo","cfo_ytd")):
            value=number(row_key)
            try:
                matched=value is not None and math.isclose(float(periods[-1][record_key]),value,rel_tol=1e-6,abs_tol=.01)
            except (KeyError,ValueError,TypeError):
                matched=False
            if not matched:
                reasons.append("收窄证据与最新已核对财报总额不一致："+row_key)
    return {"passed":not reasons,"reasons":reasons,"variant":"loss_recovery","periods":periods,
            "min_reduction_fraction":min(reductions) if len(reductions)==2 else None,"ps_ttm":ps,
            "max_ps":config.get("loss_recovery_max_ps",3),"max_pb":config["max_pb"],
            "valuation_basis":"市值/实际TTM营收PS与PB限制；盈利尚未兑现，不声称正常化PE估值通过"}
