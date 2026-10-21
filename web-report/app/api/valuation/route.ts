import { latestReport } from "@/lib/report";
import { codeValue } from "@/lib/controls.mjs";
import { fetchValuation } from "@/lib/market-data.mjs";

export const dynamic = "force-dynamic";
const cache = new Map<string, { at: number; value: unknown }>();
export async function GET(request: Request) {
  let code;
  try { code = codeValue(new URL(request.url).searchParams.get("code")); }
  catch { return Response.json({ error: "请提供六位A股代码。" }, { status: 400 }); }
  try {
    const report: any = await latestReport();
    if (report.valuations?.[code]) return Response.json(report.valuations[code], { headers: { "Cache-Control": "no-store" } });
    const key = report.report_sha256 + ":" + code, old = cache.get(key);
    if (old && Date.now() - old.at < 600000) return Response.json(old.value, { headers: { "Cache-Control": "no-store" } });
    const context = report.top_candidates.find((r: { code: string }) => r.code === code);
    const value = await fetchValuation(code, report.session, context);
    if (cache.size > 200) cache.clear();
    cache.set(key, { at: Date.now(), value });
    return Response.json(value, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return Response.json({ code, status: "unavailable", label: "估值待核验", reasons: [error instanceof Error ? error.message : "真实估值源暂不可用"] }, { headers: { "Cache-Control": "no-store" } });
  }
}
