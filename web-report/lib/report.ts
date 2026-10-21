import { splitReport } from "./controls.mjs";

export const REPOSITORY = "taoming030723-a11y/Tommy-Capital-AI";
let cached: { at: number; data: unknown } | null = null;

async function read(url: string, json = true): Promise<any> {
  const response = await fetch(url, { headers: { "User-Agent": "Tommy-Capital-Report", Accept: "application/vnd.github+json" }, signal: AbortSignal.timeout(20000) });
  if (!response.ok) throw new Error("公开报告暂时无法获取（HTTP " + response.status + "）。请稍后刷新。");
  return json ? response.json() : response.text();
}

export async function latestReport() {
  if (cached && Date.now() - cached.at < 60000) return cached.data;
  // Public smart-HTTP refs avoid GitHub's shared unauthenticated REST quota.
  let commit: string | null = null;
  try {
    const refs = await read(`https://github.com/${REPOSITORY}.git/info/refs?service=git-upload-pack`, false);
    commit = refs.match(/([a-f0-9]{40}) refs\/heads\/main(?:\x00|\n)/)?.[1] ?? null;
  } catch { /* Raw files below still require a coherent, dated snapshot. */ }
  const root = `https://raw.githubusercontent.com/${REPOSITORY}/${commit || "main"}/`;
  const suffix = commit ? "" : "?check=" + Math.floor(Date.now() / 60000);
  const [summary, markdown, preferences, status] = await Promise.all([
    read(root + "results/latest-summary.json" + suffix), read(root + "results/latest-report.md" + suffix, false),
    read(root + "operations/watchlist-preferences.json" + suffix), read(root + "results/scan-status.json" + suffix),
  ]);
  if (summary.scan_phase !== "closing" || summary.scan_type !== "after_close" || summary.selection_rule !== "dual_route_monthly_recovery" || !["complete", "partial"].includes(summary.status) || !summary.finished_at || !summary.generated_at?.startsWith(summary.session) || !summary.finished_at?.startsWith(summary.session) || !summary.minute15_cutoff?.startsWith(summary.session + "T") || summary.minute15_cutoff.slice(11, 19) < "15:00:00" || !Array.isArray(summary.top_candidates) || summary.top_candidates.length > 20) throw new Error("当前公开文件尚不是已发布的正式收盘报告。");
  const parts = splitReport(markdown, summary.session);
  if (!commit) {
    const [again, archive] = await Promise.all([
      read(root + "results/latest-summary.json" + suffix),
      read(root + `results/${summary.session}/closing-report.md` + suffix, false),
    ]);
    const clocks = [summary.generated_at, summary.finished_at].map((t: string) => t.slice(0, 19).replace("T", " "));
    const archived = splitReport(archive, summary.session);
    if (JSON.stringify(summary) !== JSON.stringify(again) || clocks.some((t: string) => !parts.market.includes(t)) || parts.market !== archived.market || parts.reasons !== archived.reasons) throw new Error("报告仍在更新或缓存版本不一致，请稍后刷新；未混用日期或排名。");
  }
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(summary) + markdown));
  const report_sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  const data = {
    commit, report_sha256, read_consistency: commit ? "fixed_commit" : "validated_snapshot", fetched_at: new Date().toISOString(), session: summary.session,
    generated_at: summary.generated_at, finished_at: summary.finished_at, status: summary.status,
    scan_complete: summary.scan_complete, scoring_version: summary.scoring_version,
    candidate_count: summary.candidate_count, display_count: summary.top_candidates?.length ?? 0,
    coverage: summary.coverage, workflow_run_url: summary.workflow_run_url,
    top_candidates: summary.top_candidates || [],
    valuations: summary.valuations || {}, valuation_coverage: summary.valuation_coverage || null,
    technical_reviews: summary.technical_reviews || null,
    tracking: summary.top10_tracking?.all_rows ?? summary.top10_tracking?.rows ?? [],
    public_ignored: preferences.ignored_codes ?? [], monitor: summary.macd_monitor ?? null,
    scan_status: status, ...parts,
  };
  cached = { at: Date.now(), data }; return data;
}
