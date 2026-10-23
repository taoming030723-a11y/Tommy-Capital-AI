"""Chinese monitor explanations, separate from ranking and order permissions."""

PERIOD_LABELS={"daily":"日线","120":"120分钟","60":"60分钟","30":"30分钟","15":"15分钟"}
CONDITIONS={"pivot_spacing":"枢轴间距","price_extension":"价格创新高/低幅度","price_extension_atr":"价格ATR幅度",
            "dif_improvement_atr":"DIF的ATR改善","same_zero_region":"DIF同侧零轴过滤","right_complete":"右侧完整K线",
            "confirmation_rebound":"右侧反弹/回落幅度","not_invalidated":"后续价格未破坏枢轴"}


def render_monitor_section(result, valuations=None, include_kd=True):
    from .reporting import table,number,cell,valuation_cells
    lines=["## 多周期MACD背离监控","","检查时间："+str(result.get("checked_at","未执行"))+
           "；柱值=2×(DIF−DEA)，仅完整K线确认。","","监控状态："+str(result.get("status","未执行"))+
           "；monitor_complete="+str(result.get("monitor_complete",False)).lower()+"。",""]
    if result.get("error"):
        lines += ["监控数据未齐："+cell(result["error"]),""]
    rows=[]
    for row in result.get("rows",[]):
        rows.append([str(row.get("name") or "名称待核验")+"（"+row["code"]+"）","／".join(row["sources"]),
            *[row["frames"].get(p,{}).get("label","数据不足，无法判断") for p in ("daily","120","60","30","15")],
            row.get("holding_risk",{}).get("label") or row["combined"]["label"], *valuation_cells((valuations or {}).get(row["code"]))])
    lines += table(["股票","来源","日线","120分钟","60分钟","30分钟","15分钟","综合状态","PE(TTM) / PB","三年PE中位 / 当前分位","综合估值"],
        rows or [["暂无已核验的重点监控股票",*["—"]*10]])
    cutoffs=[]
    for p,label in PERIOD_LABELS.items():
        dates=sorted({r["frames"].get(p,{}).get("last_bar") for r in result.get("rows",[]) if r["frames"].get(p,{}).get("last_bar")})
        if dates:
            cutoffs.append(label+" "+(dates[0] if len(dates)==1 else dates[0]+"至"+dates[-1]))
    if cutoffs:
        lines += ["实际行情截止："+"；".join(cutoffs)+"。逐股来源与缺失状态保存在同份JSON。",""]
    if include_kd:
        from .kd_reporting import render_kd_rows
        lines += render_kd_rows(result.get("rows", []))
    for row in result.get("rows",[]):
        if not any(f.get("signals") for f in row["frames"].values()) and row.get("data_complete"):
            continue
        lines += ["**"+cell(row.get("name") or row["code"])+"（"+row["code"]+"）**",""]
        for period,frame in row["frames"].items():
            lines += [f"{PERIOD_LABELS.get(period,period)}：{cell(frame.get('label'))}；行情截止{cell(frame.get('last_bar') or frame.get('source_last_bar'))}，来源{cell(frame.get('source'))}。",""]
            if frame.get("error"):
                lines += [cell(frame["error"]),""]
            for signal in frame.get("signals",[]):
                peaks,dates=signal.get("histogram_peaks",[None,None]),signal.get("peak_dates",[None,None])
                price_dates=signal.get("price_extreme_dates") or [signal.get("previous_pivot"),signal.get("current_pivot")]
                price_label="波段价格极值（非确认枢轴）" if signal.get("kind")=="histogram_decay" else "价格枢轴"
                methods="／".join({"DIF":"DIF经典","histogram":"柱体动能"}.get(m,m) for m in signal.get("confirmed_methods",[])) or "尚未确认"
                lines += [f"{cell(signal['label'])}：{price_label}{cell(price_dates[0])}/{cell(price_dates[1])}，"
                    f"价格{number(signal.get('price_previous'))}→{number(signal.get('price_current'))}；"
                    f"同向柱峰{number(peaks[0])}（{cell(dates[0])}）→{number(peaks[1])}（{cell(dates[1])}）；"
                    f"DIF {number(signal.get('dif_previous'))}→{number(signal.get('dif_current'))}；"
                    f"历史首次可计算{cell(signal.get('first_seen_at'))}，K线确认{cell(signal.get('confirmed_at'))}，"
                    f"首次实际观察{cell(signal.get('first_observed_at'))}。", "",
                    "依据："+cell(signal.get("reason"))+"；确认类型："+methods+"；待满足："+
                    cell("、".join(CONDITIONS.get(k,k) for k in signal.get("next_conditions",[])) or
                         ("等待价格形成可比较新高/新低及右侧确认" if signal.get("state")=="momentum_change" else "继续核对枢轴是否失效"))+"。",""]
                if signal.get("conditions"):
                    lines += ["逐项核验："+"；".join(CONDITIONS.get(k,k)+( "通过" if v else "未通过") for k,v in signal["conditions"].items())+"。DIF与柱体按各自条件分别确认。",""]
        combined=row["combined"]
        lines += ["综合："+cell(combined["label"])+"；同向周期"+cell("／".join(combined["resonating_periods"]))+
                  "；冲突周期"+cell(str(combined.get("conflicting_periods") or "无"))+"。",""]
        if row.get("holding_risk"):
            risk=row["holding_risk"]
            lines += [cell(risk["label"])+"："+cell("；".join(risk["reasons"]))+"。"+cell(risk["message"]),""]
    return lines
