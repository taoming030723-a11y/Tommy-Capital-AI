import test from "node:test";
import assert from "node:assert/strict";
import { evaluateValuation } from "../lib/valuation.mjs";
import { searchStocks, fetchValuation } from "../lib/market-data.mjs";
import { applyAction, emptyPreferences, observationScope } from "../lib/controls.mjs";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { persistEnrichedReport } from "../scripts/attach-valuations.mjs";

function frozen(pe = 10) {
  const rows = [];
  for (let t = Date.parse("2024-01-01"); t <= Date.parse("2026-10-19"); t += 86400000) {
    const d = new Date(t); if ([0,6].includes(d.getUTCDay())) continue;
    rows.push({ SECURITY_CODE: "600089", TRADE_DATE: d.toISOString().slice(0,10), CLOSE_PRICE: 10, PE_TTM: 30, PB_MRQ: 1.5 });
  }
  rows.at(-1).PE_TTM = pe; return rows;
}
const context = { price: 10, profit_ttm: 1e8, core_profit_ttm: 9e7, cfo_ttm: 1e8, core_margin_improving: true, period: "2026-06-30", announced_at: "2026-08-30" };
const run = (history, extra = {}) => evaluateValuation({ code:"600089", session:"2026-10-19", history, context, ...extra });

test("low and high PE require both history and financial quality; classification does not alter inputs", () => {
  const history = frozen(); const before = structuredClone(history);
  assert.equal(run(history).label, "相对低估"); assert.equal(run(frozen(80)).label, "相对高估");
  assert.deepEqual(history,before); assert.equal(run(history).historical.excludes_current_session,true);
});
test("negative earnings and negative PE are never an undervaluation shortcut", () => {
  assert.equal(run(frozen(-5)).status,"not_applicable");
  assert.equal(run(frozen(),{context:{...context,profit_ttm:-1}}).status,"not_applicable");
});
test("missing PE does not invent a loss or a valuation conclusion", () => {
  const rows = frozen(); rows.at(-1).PE_TTM = null;
  const result = run(rows);
  assert.equal(result.status, "unavailable"); assert.equal(result.label, "估值待核验");
});
test("insufficient positive history is unavailable, never fair or no signal", () => {
  assert.equal(run(frozen().slice(-60)).status,"unavailable");
  assert.equal(run(frozen(),{context:{}}).status,"unavailable");
});
test("low historical PE cannot override poor cash conversion", () => {
  assert.equal(run(frozen(),{context:{...context,cfo_ttm:1e7}}).label,"历史PE偏低，兑现待观察");
});
test("future prices and future announcements cannot leak into the result", () => {
  const base=run(frozen());
  const extra={SECURITY_CODE:"600089",TRADE_DATE:"2026-10-20",CLOSE_PRICE:1000,PE_TTM:1000};
  assert.deepEqual({...run([...frozen(),extra]),checked_at:null},{...base,checked_at:null});
  assert.equal(run(frozen(),{context:{...context,announced_at:"2026-10-20"}}).status,"unavailable");
});
test("wrong codes, duplicate sessions, stale current data and mismatched scan prices are rejected", () => {
  assert.throws(()=>run([...frozen(),frozen()[0]]),/重复/);
  assert.throws(()=>run([{...frozen()[0],SECURITY_CODE:"000001"}]),/不匹配/);
  assert.equal(run(frozen().slice(0,-1)).status,"unavailable");
  assert.equal(run(frozen(),{context:{...context,price:11}}).status,"unavailable");
});
test("financial sector cash flow conversion is not treated as industrial valuation", () => {
  assert.equal(run(frozen(),{context:{...context,industry:"银行"}}).status,"unavailable");
});
test("holding quick-add preserves cost and quantity; exclusion retains actual holdings independently", () => {
  let pref=applyAction(emptyPreferences(),{action:"save_holding",code:"600089",cost:12,shares:100});
  pref=applyAction(pref,{action:"ignore",code:"600089"});
  pref=applyAction(pref,{action:"add_holding_watch",code:"600089",name:"特变电工"});
  assert.equal(pref.holdings[0].cost,12); assert.equal(pref.holdings[0].shares,100);
  let rows=observationScope([{code:"600089",appearance_count:3}],pref);
  assert.deepEqual(rows[0].sources,["Top10观察","持仓"]);
  pref=applyAction(pref,{action:"exclude_watch",code:"600089"});
  rows=observationScope([{code:"600089",appearance_count:3}],pref);
  assert.deepEqual(rows[0].sources,["持仓"]); assert.equal(rows[0].appearance_count,undefined);
});
test("name resolution excludes similarly named Hong Kong securities and preserves leading zeros", async () => {
  const original=globalThis.fetch;
  globalThis.fetch=async()=>Response.json({QuotationCodeTable:{Data:[{Classify:"HK",Code:"06127",Name:"昭衍新药"},{Classify:"AStock",Code:"603127",Name:"昭衍新药"},{Classify:"AStock",Code:"000001",Name:"平安银行"}]}});
  try {assert.deepEqual(await searchStocks("昭衍新药"),[{code:"603127",name:"昭衍新药"}]);assert.equal((await searchStocks("000001"))[0].code,"000001");}
  finally{globalThis.fetch=original;}
});
test("partially returned history pages cannot pass valuation checks", async () => {
  const original=globalThis.fetch;
  globalThis.fetch=async()=>Response.json({code:0,result:{count:9999,pages:2,data:frozen()}});
  try{await assert.rejects(fetchValuation("600089","2026-10-19",context),/分页/);}finally{globalThis.fetch=original;}
});
test("failed optional valuation rendering leaves the market report publishable and untouched", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "valuation-rollback-"));
  try {
    await writeFile(path.join(root, "report.json"), '{"original":true}');
    await writeFile(path.join(root, "summary.json"), '{"original":true}');
    await assert.rejects(persistEnrichedReport(root, { valuations: {} }, () => ({ status: 1 })), /保留原始扫描报告/);
    assert.equal(await readFile(path.join(root, "report.json"), "utf8"), '{"original":true}');
    assert.equal(await readFile(path.join(root, "summary.json"), "utf8"), '{"original":true}');
  } finally { await rm(root, { recursive: true, force: true }); }
});
