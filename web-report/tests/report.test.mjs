import test from "node:test";
import assert from "node:assert/strict";

function fixture(session = "2026-10-16") {
  return {
    session, scan_phase: "closing", scan_type: "after_close", selection_rule: "dual_route_monthly_recovery",
    generated_at: session + "T15:39:00+08:00", finished_at: session + "T16:10:00+08:00", minute15_cutoff: session + "T15:00:00",
    status: "partial", scan_complete: false, scoring_version: "weekly_monthly_only_50_50_v8", top_candidates: [],
    top10_tracking: { all_rows: [{ code: "600000", appearance_count: 3 }], rows: [] },
  };
}

for (const inconsistent of [false, true]) {
  test("REST-quota-independent snapshot fallback " + (inconsistent ? "rejects a mixed archive" : "validates both dates, scan clocks and the closing archive"), async () => {
    const original = globalThis.fetch, summary = fixture();
    const report = markdown.replace("实际范围", "实际范围 " + summary.generated_at.slice(0,19).replace("T", " ") + " " + summary.finished_at.slice(0,19).replace("T", " "));
    globalThis.fetch = async value => {
      const url = String(value);
      if (url.includes("/info/refs")) return new Response("", { status: 403 });
      assert.ok(url.includes("/main/") && url.includes("?check="));
      if (url.includes("latest-summary.json")) return Response.json(summary);
      if (url.includes("latest-report.md")) return new Response(report);
      if (url.includes("closing-report.md")) return new Response(inconsistent ? report.replace("原排名", "其他排名") : report);
      if (url.includes("watchlist-preferences.json")) return Response.json({ schema: 1, ignored_codes: [] });
      if (url.includes("scan-status.json")) return Response.json({ status: "partial" });
      throw Error("unexpected request");
    };
    try {
      const { latestReport } = await import("../lib/report.ts?fallback=" + inconsistent);
      if (inconsistent) await assert.rejects(latestReport(), /缓存版本不一致/);
      else { const result = await latestReport(); assert.equal(result.commit, null); assert.equal(result.read_consistency, "validated_snapshot"); assert.equal(result.report_sha256.length, 64); }
    } finally { globalThis.fetch = original; }
  });
}
const markdown = "# Tommy Capital · 2026-10-16 · 收盘\n\n## 扫描概况\n\n实际范围\n\n## 综合前20\n\n原排名\n\n## 综合前10累计至少3次\n\n原观察\n\n## 前20入选理由\n\n原理由";

for (const scenario of ["ok", "mixed dates", "model review", "incomplete close", "network failure"]) {
  test("pinned report fetch: " + scenario, async () => {
    const original = globalThis.fetch, urls = [], summary = fixture();
    if (scenario === "model review") summary.scan_phase = "model_review";
    if (scenario === "incomplete close") summary.minute15_cutoff = "2026-10-16T14:45:00";
    globalThis.fetch = async url => {
      urls.push(url);
      if (scenario === "network failure") return new Response("", { status: 503 });
      if (url.includes("/info/refs?service=git-upload-pack")) return new Response("003f" + "a".repeat(40) + " refs/heads/main\n");
      assert.ok(url.includes("/" + "a".repeat(40) + "/"));
      if (url.endsWith("latest-summary.json")) return Response.json(summary);
      if (url.endsWith("latest-report.md")) return new Response(scenario === "mixed dates" ? markdown.replaceAll("2026-10-16", "2026-10-15") : markdown);
      if (url.endsWith("watchlist-preferences.json")) return Response.json({ schema: 1, ignored_codes: [] });
      if (url.endsWith("scan-status.json")) return Response.json({ status: "partial" });
      throw Error("unexpected request");
    };
    try {
      const { latestReport } = await import("../lib/report.ts?case=" + encodeURIComponent(scenario));
      if (scenario === "ok") {
        const result = await latestReport(); assert.equal(result.session, "2026-10-16");
        assert.equal(result.scan_complete, false); assert.equal(result.tracking.length, 1);
        await latestReport(); assert.equal(urls.length, 5, "one-minute cache avoids repeated GitHub rate usage");
      } else await assert.rejects(latestReport());
    } finally { globalThis.fetch = original; }
  });
}
