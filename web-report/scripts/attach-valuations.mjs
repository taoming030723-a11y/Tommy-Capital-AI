// Optional, isolated report enrichment: never reorder/re-score research candidates.
import { readFile, writeFile, mkdtemp, readdir, rename, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fetchValuation } from "../lib/market-data.mjs";

// Prepare all machine/human outputs away from the live scan directory. A failed
// optional render must leave the original publishable report byte-for-byte intact.
export async function persistEnrichedReport(root, report, refresh = stage => spawnSync(
  process.env.TOMMY_PYTHON || "python", ["-m", "tommy_capital.valuation_view", "--out", stage], { stdio: "inherit" }
)) {
  const stage = await mkdtemp(path.join(path.dirname(path.resolve(root)), ".valuation-stage-"));
  try {
    await writeFile(path.join(stage, "report.json"), JSON.stringify(report, null, 2));
    const result = refresh(stage);
    if (result.error || result.status !== 0) throw new Error("估值附加渲染失败，保留原始扫描报告");
    const summary = JSON.parse(await readFile(path.join(stage, "summary.json"), "utf8"));
    if (JSON.stringify(summary.valuations) !== JSON.stringify(report.valuations) || summary.generated_at !== report.generated_at) throw new Error("附加估值机器摘要不匹配，保留原始报告");
    for (const name of await readdir(stage)) await rename(path.join(stage, name), path.join(root, name));
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
