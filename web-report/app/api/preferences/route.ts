import { and, eq } from "drizzle-orm";
import { getChatGPTUser } from "@/app/chatgpt-auth";
import { getDb } from "@/db";
import { reportPreferences } from "@/db/schema";
import { applyAction, emptyPreferences } from "@/lib/controls.mjs";
import { resolveWatchAction } from "@/lib/market-data.mjs";
import { latestReport } from "@/lib/report";

export const dynamic = "force-dynamic";
const headers = { "Cache-Control": "no-store", Vary: "Cookie" };
const json = (value: unknown, status = 200) => Response.json(value, { status, headers });

export async function GET() {
  const user = await getChatGPTUser();
  if (!user) return json({ error: "请先登录你的ChatGPT账号。" }, 401);
  try {
    const [row] = await getDb().select().from(reportPreferences).where(eq(reportPreferences.userId, user.userId)).limit(1);
    return json({ revision: row?.revision ?? 0, preferences: row ? JSON.parse(row.payload) : emptyPreferences() });
  } catch {
    return json({ error: "私人设置暂时无法读取，未清空原名单。" }, 503);
  }
}

export async function POST(request: Request) {
  const user = await getChatGPTUser();
  if (!user) return json({ error: "请先登录你的ChatGPT账号。" }, 401);
  if (request.headers.get("origin") !== new URL(request.url).origin || !request.headers.get("content-type")?.startsWith("application/json")) return json({ error: "请求来源不匹配。" }, 403);
  const raw = await request.text();
  if (raw.length > 12000) return json({ error: "输入超出长度限制。" }, 413);
  let body;
  try { body = JSON.parse(raw); } catch { return json({ error: "输入格式错误。" }, 400); }
  if (!body || typeof body !== "object" || !Number.isSafeInteger(body.revision) || body.revision < 0) return json({ error: "请刷新后再保存。" }, 409);
  try {
    const db = getDb();
    const [old] = await db.select().from(reportPreferences).where(eq(reportPreferences.userId, user.userId)).limit(1);
    if ((old?.revision ?? 0) !== body.revision) return json({ error: "另一窗口已修改名单，请刷新后重试；未覆盖新设置。" }, 409);
    const preferences = old ? JSON.parse(old.payload) : emptyPreferences();
    if (["add_holding_watch", "exclude_watch"].includes(body.action)) {
      let choices;
      try {
        let tracking: any[] = [];
        if (body.action === "exclude_watch") {
          try { tracking = ((await latestReport()) as any).tracking; } catch { /* Existing personal identities remain removable. */ }
        }
        choices = await resolveWatchAction(body.query || body.code, body.action, [...tracking, ...preferences.holdings, ...preferences.manual_watch]);
      }
      catch { return json({ error: "股票名称接口暂未核验，请稍后重试；未改动原名单。" }, 503); }
      if (choices.length !== 1) return json({ error: choices.length ? "名称有多个匹配，请选定代码再确认。" : "没有找到匹配的A股，请核对名称或填写六位代码。", choices }, 422);
      body.code = choices[0].code; body.name = choices[0].name;
    }
    let next;
    try { next = applyAction(preferences, body); }
    catch (error) { return json({ error: error instanceof Error ? error.message : "输入不正确。" }, 400); }
    const record = { userId: user.userId, revision: body.revision + 1, payload: JSON.stringify(next), updatedAt: new Date().toISOString() };
    const saved = old
      ? await db.update(reportPreferences).set(record).where(and(eq(reportPreferences.userId, user.userId), eq(reportPreferences.revision, body.revision))).returning({ revision: reportPreferences.revision })
      : await db.insert(reportPreferences).values(record).onConflictDoNothing().returning({ revision: reportPreferences.revision });
    if (!saved.length) return json({ error: "名单已被另一窗口更新，请刷新后重试。" }, 409);
    return json({ revision: record.revision, preferences: next });
  } catch {
    return json({ error: "保存暂未完成，未提交至公开仓库。请稍后重试。" }, 503);
  }
}
