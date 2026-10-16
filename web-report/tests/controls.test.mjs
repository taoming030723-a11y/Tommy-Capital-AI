import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { applyAction, codeValue, emptyPreferences, observationScope, splitReport, safeLink } from "../lib/controls.mjs";

test("phone ignore and restore persist explicit overrides without modifying formal tracking", () => {
  const history = [{ code: "000001", name: "平安银行", appearance_count: 3 }];
  const original = structuredClone(history);
  const ignored = applyAction(emptyPreferences(), { action: "ignore", code: "000001" });
  assert.equal(observationScope(history, ignored).length, 0);
  const restored = applyAction(ignored, { action: "restore", code: "000001" });
  assert.equal(observationScope(history, restored, ["000001"]).length, 1);
  assert.deepEqual(history, original);
});

test("manual observation and holdings retain independent roles, one analysis identity", () => {
  let value = applyAction(emptyPreferences(), { action: "add", code: "000001" });
  value = applyAction(value, { action: "save_holding", code: "000001" });
  value = applyAction(value, { action: "ignore", code: "000001" });
  assert.deepEqual(observationScope([{ code: "000001", appearance_count: 4 }], value)[0].sources, ["手动观察", "持仓"]);
  value = applyAction(value, { action: "delete_holding", code: "000001" });
  assert.deepEqual(observationScope([], value)[0].sources, ["手动观察"]);
  value = applyAction(value, { action: "remove", code: "000001" });
  assert.equal(observationScope([], value).length, 0);
});

test("adding observation cannot impersonate a holding or increment appearances", () => {
  const value = applyAction(emptyPreferences(), { action: "add", code: "603127", name: "昭衍新药" });
  assert.deepEqual(observationScope([], value)[0].sources, ["手动观察"]);
  assert.equal(value.holdings.length, 0);
  assert.throws(() => applyAction(value, { action: "add", code: "603127" }), /已经/);
});

test("cost and quantity are optional, edited explicitly, invalid values rejected", () => {
  const empty = emptyPreferences();
  const value = applyAction(empty, { action: "save_holding", code: "600089", cost: "10.5", shares: "100", opened_on: "2026-10-16" });
  assert.equal(value.holdings[0].cost, 10.5); assert.equal(value.holdings[0].shares, 100);
  assert.equal(empty.holdings.length, 0);
  const edited = applyAction(value, { action: "save_holding", code: "600089", note: "用户自行确认", cost: "", shares: "" });
  assert.equal(edited.holdings.length, 1); assert.equal(edited.holdings[0].cost, null);
  for (const item of [{ cost: -1 }, { shares: 1.5 }, { cost: "NaN" }, { opened_on: "2026-02-30" }, { opened_on: "2026-1-1" }]) assert.throws(() => applyAction(value, { action: "save_holding", code: "600089", ...item }));
});

test("six ASCII digits including leading zeros only", () => {
  assert.equal(codeValue("000001"), "000001");
  for (const code of [1, "1", "０００００１", "600000/", "<img>", null]) assert.throws(() => codeValue(code));
});

test("formal report content is preserved and local loopback link is never emitted", () => {
  const markdown = "# Tommy Capital · 2026-10-16 · 收盘\n\n<!-- tommy-report-controls:start -->\n[旧入口](http://127.0.0.1:8501)\n<!-- tommy-report-controls:end -->\n\n## 扫描概况\n\n原始5576\n\n## 综合前20\n\n原始15只\n\n## 综合前10累计至少3次\n\n旧观察表\n\n## 前20入选理由\n\n原始理由\n\n## 多周期MACD背离监控\n\n旧监控";
  const result = splitReport(markdown, "2026-10-16");
  assert.ok(result.market.includes("原始5576")); assert.ok(result.market.includes("原始15只"));
  assert.ok(!result.market.includes("127.0.0.1")); assert.ok(!result.reasons.includes("旧监控"));
  assert.ok(result.reasons.includes("原始理由"));
  assert.throws(() => splitReport(markdown, "2026-10-15"), /日期不一致/);
});

test("unsafe and device-local links cannot execute in report markup", () => {
  for (const url of ["javascript:alert(1)", "http://127.0.0.1:8501", "https://github.com.evil.test", "/local"]) assert.equal(safeLink(url), null);
  assert.ok(safeLink("https://github.com/taoming030723-a11y/Tommy-Capital-AI"));
});

test("write route enforces authentication, same origin, revision CAS and private storage", () => {
  const source = readFileSync(new URL("../app/api/preferences/route.ts", import.meta.url), "utf8");
  assert.ok(source.includes("if (!user)")); assert.ok(source.includes('request.headers.get("origin")'));
  assert.ok(source.includes('eq(reportPreferences.userId, user.userId)'));
  assert.ok(source.includes('eq(reportPreferences.revision, body.revision)'));
  assert.ok(source.includes(".onConflictDoNothing()")); assert.ok(source.includes('"Cache-Control": "no-store"'));
  assert.ok(!source.includes("console.log") && !source.includes("github_create_commit"));
});
