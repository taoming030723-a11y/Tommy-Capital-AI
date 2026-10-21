import policy from "./valuation-config.json" with { type: "json" };
export { policy as valuationPolicy };
export const finite = value => value === null || value === undefined || value === "" || typeof value === "boolean" ? null : Number.isFinite(Number(value)) ? Number(value) : null;
const day = value => typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(0, 10) : null;
const ratio = (a, b) => finite(a) !== null && finite(b) > 0 ? Number(a) / Number(b) : null;
const rounded = value => finite(value) === null ? null : Number(Number(value).toFixed(4));

export function quantile(values, p) {
  const sorted = [...values].sort((a, b) => a - b), i = (sorted.length - 1) * p;
  return sorted[Math.floor(i)] + (sorted[Math.ceil(i)] - sorted[Math.floor(i)]) * (i % 1);
}

export function evaluateValuation({ code, session, history, context = {}, checkedAt = new Date().toISOString() }) {
  const result = { schema: 1, version: policy.version, code, session, checked_at: checkedAt, status: "unavailable", label: "估值待核验", current_pe_ttm: null, current_pb: null, price: null, historical: null, fundamental: null, reasons: [], sources: [{ name: "东方财富历史估值", url: `https://data.eastmoney.com/gzfx/detail/${code}.html` }], basis: "相对估值观察，不是DCF内在价值或交易指令；历史与基本面各占50%，不计入原选股评分" };
  if (!/^[0-9]{6}$/.test(code) || !day(session) || session !== day(session) || !Array.isArray(history)) throw new Error("估值代码、日期或历史格式错误");
  const seen = new Set(), cleaned = [];
  for (const row of history) {
    const date = day(row.TRADE_DATE);
    if (!date || (row.SECURITY_CODE && row.SECURITY_CODE !== code)) throw new Error("历史估值代码或日期不匹配");
    if (date > session) continue;
    if (seen.has(date)) throw new Error("历史估值含重复交易日，未重复加权");
    seen.add(date); cleaned.push({ ...row, date });
  }
  cleaned.sort((a, b) => a.date.localeCompare(b.date));
  const latest = cleaned.at(-1);
  if (!latest) { result.reasons.push("未取得指定日期之前的真实估值记录"); return result; }
  result.price_as_of = latest.date;
  result.price = finite(latest.CLOSE_PRICE);
  result.current_pe_ttm = finite(latest.PE_TTM);
  result.current_pb = finite(latest.PB_MRQ);
  result.current_ps = finite(latest.PS_TTM);
  if (latest.date !== session || !(result.price > 0)) { result.reasons.push("行情未更新至报告日，不能把旧PE当作当前估值"); return result; }
  if (finite(context.price) > 0 && Math.abs(Number(context.price) - result.price) > Math.max(.021, result.price * .001)) { result.reasons.push("估值源收盘价与扫描源不一致，未授予低估/高估结论"); return result; }
  const quality = context.fundamental_quality || {};
  const coreShare = finite(quality.core_profit_share_ttm) ?? ratio(context.core_profit_ttm, context.profit_ttm);
  const conversion = finite(quality.cash_conversion_ttm) ?? ratio(context.cfo_ttm, context.profit_ttm);
  const marginImproving = typeof quality.core_margin_improving === "boolean" ? quality.core_margin_improving : context.core_margin_improving;
  result.fundamental = { core_profit_share_ttm: rounded(coreShare), cash_conversion_ttm: rounded(conversion), core_margin_improving: marginImproving ?? null, report_period: context.period ?? null, announced_at: context.announced_at ?? null, source: context.fundamental_source || "原扫描已公告财报及同报告期现金流、扣非盈利" };
  if (context.announced_at && day(String(context.announced_at)) > session) { result.reasons.push("财务公告晚于估值日，拒绝未来信息"); return result; }
  const profit = finite(context.profit_ttm);
  if ((profit !== null && profit <= 0) || (result.current_pe_ttm !== null && result.current_pe_ttm <= 0)) {
    result.status = "not_applicable"; result.label = "TTM亏损，PE不适用";
    result.reasons.push("负PE/缺失PE不能与正盈利期PE直接比较，也不因亏损收窄直接认定低估；需有审阅证据的正常化盈利、SOTP或EV-Sales估值");
    return result;
  }
  if (result.current_pe_ttm === null) {
    result.reasons.push("当前PE缺失，不能据此推断亏损或授予高低估结论"); return result;
  }
  const start = new Date(session + "T00:00:00Z"); start.setUTCFullYear(start.getUTCFullYear() - policy.history_years);
  const baseline = cleaned.filter(r => r.date >= start.toISOString().slice(0, 10) && r.date < session && finite(r.PE_TTM) > 0);
  const values = baseline.map(r => Number(r.PE_TTM));
  const span = baseline.length ? (Date.parse(baseline.at(-1).date) - Date.parse(baseline[0].date)) / 86400000 : 0;
  if (values.length < policy.minimum_positive_sessions || span < policy.minimum_history_span_days) { result.reasons.push(`正PE历史不足：${values.length}个交易日、跨度${span}天；至少需${policy.minimum_positive_sessions}日及${policy.minimum_history_span_days}天，不以少量样本硬判高低估`); return result; }
  const median = quantile(values, .5), percentile = 100 * (values.filter(p => p < result.current_pe_ttm).length + .5 * values.filter(p => p === result.current_pe_ttm).length) / values.length;
  result.historical = { years: policy.history_years, samples: values.length, start: baseline[0].date, end: baseline.at(-1).date, median_pe_ttm: rounded(median), p25: rounded(quantile(values, .25)), p75: rounded(quantile(values, .75)), percentile: rounded(percentile), excludes_current_session: true, excluded_nonpositive: cleaned.filter(r => r.date >= start.toISOString().slice(0, 10) && !(finite(r.PE_TTM) > 0)).length };
  const sector = quality.financial_sector === true || /银行|证券|保险|多元金融/.test(String(context.industry || ""));
  if (sector || coreShare === null || conversion === null || typeof marginImproving !== "boolean") { result.reasons.push(sector ? "金融行业需专门资本与风险估值，未套用普通企业现金流折算" : "扣非盈利、现金流兑现或利润率改善证据未齐，只有历史PE位置，暂不作综合高低估结论"); return result; }
  const healthy = coreShare >= policy.healthy_core_share && conversion >= policy.healthy_cash_conversion;
  const factor = Math.min(1.1, Math.max(.6, .85 + .1 * Number(coreShare >= .8) + .1 * Number(conversion >= 1) + .05 * Number(marginImproving) - .15 * Number(coreShare < .7) - .15 * Number(conversion < .8)));
  const peer = finite(context.industry_pe_median), peerValid = peer > 0 && Number(context.industry_pe_samples) >= policy.minimum_industry_samples;
  const fundamentalReference = (peerValid ? peer : median) * factor;
  const reference = policy.historical_weight * median + policy.fundamental_weight * fundamentalReference;
  result.fundamental.quality_factor = rounded(factor);
  result.fundamental.reference_basis = peerValid ? "同业正PE中位数按扣非/现金流质量修正" : "同业样本不足，历史PE中位数按扣非/现金流质量修正";
  result.reference_pe = rounded(reference);
  result.reference_price_range = [rounded(result.price / result.current_pe_ttm * reference * policy.discount_ratio), rounded(result.price / result.current_pe_ttm * reference * policy.premium_ratio)];
  result.reference_price_basis = "假设当前TTM盈利不变的倍数换算区间，不是目标价或盈利预测";
  result.status = "complete";
  if (healthy && percentile <= policy.low_percentile && result.current_pe_ttm <= reference * policy.discount_ratio) result.label = "相对低估";
  else if (percentile >= policy.high_percentile && result.current_pe_ttm >= reference * policy.premium_ratio) result.label = "相对高估";
  else if (!healthy && percentile <= policy.low_percentile) result.label = "历史PE偏低，兑现待观察";
  else if ((percentile <= policy.low_percentile && result.current_pe_ttm > reference) || (percentile >= policy.high_percentile && result.current_pe_ttm < reference)) result.label = "历史与基本面估值分歧";
  else result.label = "相对合理";
  result.reasons.push(`当前PE ${result.current_pe_ttm.toFixed(2)}倍，处于近三年正PE样本${percentile.toFixed(1)}%分位；历史中位数${median.toFixed(2)}倍`);
  result.reasons.push(`扣非/归母${coreShare.toFixed(2)}、经营现金流/归母${conversion.toFixed(2)}、同期扣非利润率${marginImproving ? "改善或持平" : "未改善"}；综合参考PE ${reference.toFixed(2)}倍`);
  if (!healthy) result.reasons.push("基本面质量未达到健康门槛，不把历史低PE直接认定为便宜");
  return result;
}
