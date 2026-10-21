// Presentation and private-list controls only. No scoring or MACD implementation.
export function emptyPreferences() {
  return { schema: 1, ignored_codes: [], restored_codes: [], manual_watch: [], holdings: [] };
}

export function codeValue(value) {
  if (typeof value !== "string" || !/^[0-9]{6}$/.test(value)) throw new Error("请输入六位A股代码，保留前导0。");
  return value;
}

function text(value, limit) {
  if (value === undefined || value === null) return "";
  if (typeof value !== "string" || value.length > limit) throw new Error("输入文字过长或格式不正确。");
  return value.trim();
}

function number(value, integer = false) {
  if (value === "" || value === null || value === undefined) return null;
  if ((typeof value !== "string" && typeof value !== "number") || !/^\d+(\.\d+)?$/.test(String(value))) throw new Error("成本及股数应填写正数或留空。");
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0 || n > 1e12 || (integer && !Number.isInteger(n))) throw new Error("成本及股数格式不正确。");
  return n;
}

export function applyAction(current, action) {
  if (!current || current.schema !== 1) throw new Error("私人名单版本不匹配，未重置原记录。");
  if (!action || typeof action !== "object") throw new Error("操作格式不正确。");
  const next = structuredClone(current);
  const code = codeValue(action.code);
  const ignored = new Set(next.ignored_codes);
  const restored = new Set(next.restored_codes);
  switch (action.action) {
    case "add_holding_watch": {
      const old = next.holdings.find(r => r.code === code);
      next.holdings = next.holdings.filter(r => r.code !== code).concat([{ code, cost: null, shares: null, opened_on: null, note: "", ...old, name: old?.name || text(action.name, 80) }]);
      ignored.delete(code); restored.add(code);
      break;
    }
    case "exclude_watch":
      ignored.add(code); restored.delete(code);
      next.manual_watch = next.manual_watch.filter(r => r.code !== code);
      break;
    case "ignore": ignored.add(code); restored.delete(code); break;
    case "restore": ignored.delete(code); restored.add(code); break;
    case "add":
      if (next.manual_watch.some(r => r.code === code)) throw new Error("这只股票已经在手动观察名单中。");
      next.manual_watch.push({ code, name: text(action.name, 80), note: text(action.note, 4000) });
      break;
    case "remove": next.manual_watch = next.manual_watch.filter(r => r.code !== code); break;
    case "save_holding": {
      const date = text(action.opened_on, 10);
      if (date && (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(Date.parse(date)) || new Date(date).toISOString().slice(0, 10) !== date)) throw new Error("建仓日期应为真实的YYYY-MM-DD日期，或留空。");
      const row = { code, name: text(action.name, 80), note: text(action.note, 4000), cost: number(action.cost), shares: number(action.shares, true), opened_on: date || null };
      next.holdings = next.holdings.filter(r => r.code !== code).concat([row]);
      break;
    }
    case "delete_holding": next.holdings = next.holdings.filter(r => r.code !== code); break;
    default: throw new Error("不支持此操作。");
  }
  if (next.manual_watch.length > 6000 || next.holdings.length > 6000) throw new Error("名单数量超出上限。");
  next.ignored_codes = [...ignored].sort(); next.restored_codes = [...restored].sort();
  next.manual_watch.sort((a, b) => a.code.localeCompare(b.code));
  next.holdings.sort((a, b) => a.code.localeCompare(b.code));
  return next;
}

export function effectiveIgnored(publicCodes, preferences) {
  const ignored = new Set([...publicCodes, ...preferences.ignored_codes]);
  for (const code of preferences.restored_codes) ignored.delete(code);
  return ignored;
}

export function observationScope(tracking, preferences, publicIgnored = []) {
  const ignored = effectiveIgnored(publicIgnored, preferences);
  const scope = new Map();
  for (const row of tracking) {
    if (row.appearance_count >= 3 && !ignored.has(row.code)) scope.set(row.code, { ...row, sources: ["Top10观察"] });
  }
  for (const [rows, role] of [[preferences.manual_watch, "手动观察"], [preferences.holdings, "持仓"]]) {
    for (const row of rows) {
      const item = scope.get(row.code) || { code: row.code, name: row.name, sources: [] };
      scope.set(row.code, { ...item, name: row.name || item.name, sources: [...item.sources, role] });
    }
  }
  return [...scope.values()].sort((a, b) => a.code.localeCompare(b.code));
}

export function splitReport(markdown, session) {
  const clean = markdown.replace(/<!-- tommy-scan-status:start -->[\s\S]*?<!-- tommy-scan-status:end -->\s*/g, "")
    .replace(/<!-- tommy-report-controls:start -->[\s\S]*?<!-- tommy-report-controls:end -->\s*/g, "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(session) || !clean.startsWith("# Tommy Capital · " + session + " · ")) throw new Error("中文报告与机器摘要日期不一致，暂不显示混合版本。");
  const sections = clean.split(/^## /m);
  const market = sections.filter((s, i) => i === 0 || s.startsWith("扫描概况") || s.startsWith("综合前20"));
  const reasons = sections.filter(s => s.startsWith("前20入选理由"));
  if (market.length < 3 || reasons.length !== 1) throw new Error("正式报告章节不完整，未生成替代行情。");
  return { market: market.map((s, i) => i === 0 ? s : "## " + s).join(""), reasons: "## " + reasons[0] };
}

export function safeLink(url) {
  try {
    const value = new URL(url);
    return value.protocol === "https:" && ["github.com", "tommy-capital-report.stableokapi18.chatgpt.site"].includes(value.hostname) ? value.href : null;
  } catch { return null; }
}
