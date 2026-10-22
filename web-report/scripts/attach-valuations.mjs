// Optional, isolated report enrichment: never reorder/re-score research candidates.
import { readFile, writeFile, mkdtemp, readdir, rename, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fetchValuation } from "../lib/market-data.mjs";

const enrichmentFields = new Set(['valuations', 'valuation_coverage', 'technical_reviews']);
function frozenFields(value) {
  return JSON.stringify(Object.fromEntries(Object.keys(value).filter(k => !enrichmentFields.has(k)).sort().map(k => [k, value[k]])));
}

// Prepare all machine/human outputs away from the live scan directory. A failed
// optional render must leave the original publishable report byte-for-byte intact.
export async function persistEnrichedReport(root, report, refresh = stage => spawnSync(
  process.env.TOMMY_PYTHON || "python", ["-m", "tommy_capital.valuation_view", "--out", stage], { stdio: "inherit" }
)) {
  const stage = await mkdtemp(path.join(path.dirname(path.resolve(root)), ".valuation-stage-"));
  try {
    const originalText = await readFile(path.join(root, "report.json"), "utf8");
    if (frozenFields(JSON.parse(originalText)) !== frozenFields(report)) throw new Error("附加观察意外改变冻结研究输入，保留原始扫描报告");
    // JS stringify changes 2.0 to 2, changing the Python snapshot hash even
    // though numbers compare equal. Merge annotations in Python, preserving
    // the original report's numeric representations and its frozen digest.
    await writeFile(path.join(stage, "report.json"), originalText);
    await writeFile(path.join(stage, "enrichment.json"), JSON.stringify(Object.fromEntries(
      Object.entries(report).filter(([key]) => enrichmentFields.has(key))
    )));
    const result = refresh(stage);
    if (result.error || result.status !== 0) throw new Error("估值附加渲染失败，保留原始扫描报告");
    const summary = JSON.parse(await readFile(path.join(stage, "summary.json"), "utf8"));
    if (JSON.stringify(summary.valuations ?? null) !== JSON.stringify(report.valuations ?? null) || summary.generated_at !== report.generated_at) throw new Error("附加估值机器摘要不匹配，保留原始报告");
    for (const name of await readdir(stage)) if (name !== 'enrichment.json') await rename(path.join(stage, name), path.join(root, name));
  } finally { await rm(stage, { recursive: true, force: true }); }
}

async function main() {
const root = process.argv[2] || "reports/latest";
if (root === "--help") { console.log("Usage: node web-report/scripts/attach-valuations.mjs [reports/latest]"); process.exit(0); }
const report = JSON.parse(await readFile(path.join(root, "report.json"), "utf8"));
if (report.scan_phase !== "closing" || !["complete", "partial"].includes(report.status)) {
  console.log("估值附加仅用于正式收盘结果，已跳过。"); process.exit(0);
}
const identity = JSON.stringify([report.rankings.map(r => [r.code, r.score, r.fundamental_score, r.technical_score]), report.coverage, report.scan_complete, report.top10_history]);
const publicRows = new Map([...(report.rankings || []), ...(report.observations || [])].map(r => [r.code, r]));
const summary = JSON.parse(await readFile(path.join(root, "summary.json"), "utf8"));
const codes = [...new Set([...(summary.top_candidates || []).map(r => r.code), ...(report.top10_tracking?.rows || []).map(r => r.code), ...(report.macd_monitor?.scope || []).map(r => r.code)])];
const values = {}; let cursor = 0; const deadline = Date.now() + 120000;
async function worker() {
  while (cursor < codes.length) {
    const code = codes[cursor++];
    if (Date.now() > deadline) { values[code] = { schema: 1, code, session: report.session, status: "unavailable", label: "估值待核验", reasons: ["本轮附加估值已达时间预算，未延迟原市场报告或填充假值"] }; continue; }
    try { values[code] = await fetchValuation(code, report.session, publicRows.get(code)); }
    catch (error) { values[code] = { schema: 1, code, session: report.session, checked_at: new Date().toISOString(), status: "unavailable", label: "估值待核验", reasons: [error.message] }; }
  }
}
await Promise.all([worker(), worker()]);
report.valuations = values;
report.valuation_coverage = { requested: codes.length, completed: Object.values(values).filter(v => v.status === "complete").length, not_applicable: Object.values(values).filter(v => v.status === "not_applicable").length, incomplete: Object.values(values).filter(v => v.status === "unavailable").length, checked_at: new Date().toISOString(), influences_ranking: false };
if (identity !== JSON.stringify([report.rankings.map(r => [r.code, r.score, r.fundamental_score, r.technical_score]), report.coverage, report.scan_complete, report.top10_history])) throw new Error("估值附加意外改变原研究数据，拒绝保存");
await persistEnrichedReport(root, report);
console.log(JSON.stringify({ valuation_coverage: report.valuation_coverage }));

}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
