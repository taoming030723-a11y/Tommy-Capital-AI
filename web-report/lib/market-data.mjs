import { codeValue } from "./controls.mjs";
import { evaluateValuation, finite, valuationPolicy } from "./valuation.mjs";

async function json(url, timeout = 18000) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(timeout), headers: { Accept: "application/json", "User-Agent": "Tommy-Capital-Research" } });
      if (!response.ok) throw new Error(`真实数据源暂不可用（HTTP ${response.status}）`);
      return await response.json();
    } catch (error) { if (attempt) throw error; }
  }
}

export async function searchStocks(query) {
  if (typeof query !== "string" || !query.trim() || query.length > 80) throw new Error("请输入股票名称或六位代码");
  const target = query.trim(), url = new URL("https://searchapi.eastmoney.com/api/suggest/get");
  url.search = new URLSearchParams({ input: target, type: "14", count: "20" }).toString();
  const raw = await json(url), seen = new Set();
  const rows = (raw.QuotationCodeTable?.Data || []).filter(r => r.Classify === "AStock" && /^[0-9]{6}$/.test(r.Code) && r.Name).map(r => ({ code: r.Code, name: r.Name })).filter(r => !seen.has(r.code) && seen.add(r.code));
  const exact = rows.filter(r => r.code === target || r.name.replace(/\s+/g, "") === target.replace(/\s+/g, ""));
  return exact.length ? exact : rows;
}

async function records(reportName, code, extra = {}, pageSize = 12) {
  const url = new URL("https://datacenter-web.eastmoney.com/api/data/v1/get");
  url.search = new URLSearchParams({ reportName, columns: "ALL", filter: `(SECURITY_CODE="${code}")`, pageSize: String(pageSize), pageNumber: "1", sortColumns: reportName === "RPT_LICO_FN_CPD" ? "REPORTDATE" : "REPORT_DATE", sortTypes: "-1", source: "WEB", client: "WEB", ...extra }).toString();
  const raw = await json(url);
  if (raw.code !== 0 || !Array.isArray(raw.result?.data) || !raw.result.data.length) throw new Error(`真实数据源没有返回可核验的${reportName}记录`);
  if (raw.result.data.some(r => r.SECURITY_CODE && r.SECURITY_CODE !== code)) throw new Error("源返回了其他股票的财务记录");
  return raw.result;
}

const date = value => typeof value === "string" ? value.slice(0, 10) : "";
function validStatements(rows, periodField, session) {
  return rows.filter(r => /^\d{4}-\d{2}-\d{2}$/.test(date(r[periodField])) && date(r[periodField]) <= session && date(r.NOTICE_DATE) >= date(r[periodField]) && date(r.NOTICE_DATE) <= session).sort((a, b) => date(b[periodField]).localeCompare(date(a[periodField])));
}
function ttm(rows, periodField, field, period) {
  const value = p => finite(rows.find(r => date(r[periodField]) === p)?.[field]);
  const current = value(period);
  if (period.endsWith("12-31")) return current;
  const year = Number(period.slice(0, 4)) - 1, annual = value(year + "-12-31"), prior = value(year + period.slice(4));
  return [current, annual, prior].every(v => v !== null) ? current + annual - prior : null;
}

export async function fetchFinancialContext(code, session) {
  const [financeRaw, incomeRaw, cashRaw] = await Promise.all([
    records("RPT_LICO_FN_CPD", code), records("RPT_DMSK_FN_INCOME", code), records("RPT_DMSK_FN_CASHFLOW", code),
  ]);
  const finance = validStatements(financeRaw.data, "REPORTDATE", session), income = validStatements(incomeRaw.data, "REPORT_DATE", session), cash = validStatements(cashRaw.data, "REPORT_DATE", session);
  const latest = finance[0]; if (!latest) throw new Error("没有估值日前已公告的财务报告");
  const period = date(latest.REPORTDATE), priorPeriod = (Number(period.slice(0, 4)) - 1) + period.slice(4);
  if ((Date.parse(session) - Date.parse(period)) / 86400000 > valuationPolicy.maximum_finance_age_days) throw new Error("最新已公告财报超过225天，不能把过时基本面当作当前估值依据");
  const same = income.find(r => date(r.REPORT_DATE) === period);
  const prior = income.find(r => date(r.REPORT_DATE) === priorPeriod);
  const reported = finite(latest.PARENT_NETPROFIT), checked = finite(same?.PARENT_NETPROFIT);
  if (reported === null || checked === null || Math.abs(reported - checked) > Math.max(.011, Math.abs(reported) * 1e-6)) throw new Error("扣非利润表与业绩报表同期版本不一致，未补造质量指标");
  const core = finite(same?.DEDUCT_PARENT_NETPROFIT), previousCore = finite(prior?.DEDUCT_PARENT_NETPROFIT), revenue = finite(same?.TOTAL_OPERATE_INCOME), previousRevenue = finite(prior?.TOTAL_OPERATE_INCOME);
  return { code, name: latest.SECURITY_NAME_ABBR, period, announced_at: date(latest.NOTICE_DATE), industry: latest.PUBLISHNAME, profit_ttm: ttm(finance, "REPORTDATE", "PARENT_NETPROFIT", period), core_profit_ttm: ttm(income, "REPORT_DATE", "DEDUCT_PARENT_NETPROFIT", period), cfo_ttm: ttm(cash, "REPORT_DATE", "NETCASH_OPERATE", period), core_margin_improving: [core, previousCore, revenue, previousRevenue].every(v => v !== null) && revenue > 0 && previousRevenue > 0 ? core >= previousCore && core / revenue >= previousCore / previousRevenue : null, fundamental_source: "东方财富已公告同期业绩报表、扣非利润表、现金流量表；TTM=本期累计+上年全年-上年同期" };
}

export async function fetchValuation(code, session, context = null) {
  codeValue(code);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(session)) throw new Error("估值日期错误");
  const start = new Date(session + "T00:00:00Z"); start.setUTCFullYear(start.getUTCFullYear() - valuationPolicy.history_years);
  const data = await records("RPT_VALUEANALYSIS_DET", code, { columns: "SECURITY_CODE,TRADE_DATE,CLOSE_PRICE,PE_TTM,PB_MRQ,PS_TTM", filter: `(SECURITY_CODE="${code}")(TRADE_DATE>='${start.toISOString().slice(0, 10)}')(TRADE_DATE<='${session}')`, sortColumns: "TRADE_DATE" }, 1000);
  if (Number(data.count) !== data.data.length || Number(data.pages) !== 1) throw new Error("历史PE分页未完整覆盖三年，未用半份历史判定高低估");
  let financial = context;
  if (!financial || (!financial.fundamental_quality && financial.core_profit_ttm === undefined)) {
    try { financial = await fetchFinancialContext(code, session); }
    catch (error) { financial = { fundamental_source: "财报未齐：" + error.message }; }
  }
  const value = evaluateValuation({ code, session, history: data.data, context: financial || {} });
  value.fundamental_context = financial;
  return value;
}
