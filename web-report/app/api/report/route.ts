import { latestReport } from "@/lib/report";

export const dynamic = "force-dynamic";
export async function GET() {
  try {
    return Response.json(await latestReport(), { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return Response.json({ error: error instanceof Error ? error.message : "报告读取失败，请稍后重试。" }, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}
