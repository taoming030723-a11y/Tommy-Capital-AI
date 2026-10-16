"use client";

import { Fragment, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { emptyPreferences, effectiveIgnored, observationScope, safeLink } from "@/lib/controls.mjs";

type Row = Record<string, any>;
type Personal = { revision: number; preferences: ReturnType<typeof emptyPreferences> };
const REPO = "https://github.com/taoming030723-a11y/Tommy-Capital-AI";
const periods = [["daily", "日线"], ["120", "120分钟"], ["60", "60分钟"], ["30", "30分钟"], ["15", "15分钟"]];

function inline(text: string): ReactNode[] {
  return text.split(/(\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*)/g).filter(Boolean).map((part, i) => {
    const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (link) { const href = safeLink(link[2]); return href ? <a key={i} href={href} target="_blank" rel="noopener noreferrer">{link[1]}</a> : <span key={i}>{link[1]}</span>; }
    return part.startsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> : <Fragment key={i}>{part.replace(/\\\|/g, "|").replace(/\\`/g, "`")}</Fragment>;
  });
}

function Markdown({ value }: { value: string }) {
  const lines = value.split("\n"), blocks: ReactNode[] = [];
  const cells = (line: string) => line.trim().slice(1, -1).split(/(?<!\\)\|/).map(s => s.trim());
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim() || line.startsWith("<!--") || line.startsWith("# ")) continue;
    if (line.startsWith("| ")) {
      const table = [line]; while (i + 1 < lines.length && lines[i + 1].startsWith("| ")) table.push(lines[++i]);
      blocks.push(<div className="table-scroll" key={i} tabIndex={0} aria-label="可左右滑动的报告表格"><table><thead><tr>{cells(table[0]).map((c, j) => <th key={j}>{inline(c)}</th>)}</tr></thead><tbody>{table.slice(2).map((row, r) => <tr key={r}>{cells(row).map((c, j) => <td key={j}>{inline(c)}</td>)}</tr>)}</tbody></table></div>);
    } else if (line.startsWith("## ")) blocks.push(<h2 key={i}>{line.slice(3)}</h2>);
    else if (line.startsWith("### ")) blocks.push(<h3 key={i}>{line.slice(4)}</h3>);
    else blocks.push(<p key={i}>{inline(line)}</p>);
  }
  return <div className="original-report">{blocks}</div>;
}

async function readJson(url: string, options?: RequestInit): Promise<any> {
  const response = await fetch(url, { ...options, cache: "no-store" });
  let value: any; try { value = await response.json(); } catch { throw new Error("连接中断或登录已过期，请重新登录后刷新。"); }
  if (!response.ok) throw new Error(value.error || "暂时无法完成，请稍后重试。");
  return value;
}

export default function ReportDashboard() {
  const [data, setData] = useState<Row | null>(null);
  const [personal, setPersonal] = useState<Personal | null>(null);
  const [page, setPage] = useState("report");
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [edit, setEdit] = useState<Row | null>(null), [alertsOnly, setAlertsOnly] = useState(false);

  async function refresh() {
    setLoading(true); setError("");
    const results = await Promise.allSettled([readJson("/api/report"), readJson("/api/preferences")]);
    if (results[0].status === "fulfilled") setData(results[0].value);
    if (results[1].status === "fulfilled") setPersonal(results[1].value);
    setError(results.filter((r): r is PromiseRejectedResult => r.status === "rejected").map(r => r.reason.message).join(" "));
    setLoading(false);
  }
  useEffect(() => { void refresh(); }, []);

  async function save(action: string, values: Row) {
    if (!personal) return;
    setSaving(true); setError(""); setNotice("");
    try {
      const result = await readJson("/api/preferences", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...values, action, revision: personal.revision }) });
      setPersonal(result); setNotice("已保存到你的私人网页名单。");
      if (action === "save_holding" || action === "delete_holding") setEdit(null);
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败，请重试。"); }
    finally { setSaving(false); }
  }

  const preferences = personal?.preferences ?? emptyPreferences();
  const tracking: Row[] = data?.tracking ?? [];
  const ignored = effectiveIgnored(data?.public_ignored ?? [], preferences);
  const watch = tracking.filter(r => r.appearance_count >= 3 && !ignored.has(r.code));
  const scope: Row[] = observationScope(tracking, preferences, data?.public_ignored ?? []);
  const names = new Map<string, string>([...tracking, ...(data?.top_candidates ?? []), ...preferences.manual_watch, ...preferences.holdings].map((r: Row) => [r.code, r.name]));
  const disabled = saving || !personal;

  function submit(event: FormEvent<HTMLFormElement>, action: string) {
    event.preventDefault();
    const values: Row = Object.fromEntries(new FormData(event.currentTarget).entries());
    values.name = values.name || names.get(values.code) || "";
    void save(action, values);
  }

  function exportBackup() {
    const url = URL.createObjectURL(new Blob([JSON.stringify({ schema: 1, exported_at: new Date().toISOString(), preferences }, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "tommy-private-watchlist.json"; link.click(); URL.revokeObjectURL(url);
  }

  function WatchControls() {
    return <section className="panel" id="watchlist">
      <div className="section-heading"><div><span className="eyebrow">CONTINUED OBSERVATION</span><h2>累计Top10观察池</h2></div><span className="count">{watch.length} 只</span></div>
      <p className="muted">累计三个不同正式收盘日入选，不要求连续。忽略只隐藏网页观察身份，保留原次数及排名。</p>
      <div className="table-scroll"><table><thead><tr><th>股票</th><th>累计次数</th><th>最近入榜</th><th>日量 / 前20日</th><th>操作</th></tr></thead><tbody>{watch.map(row => <tr key={row.code}><td><b>{row.name}</b><small>{row.code}</small></td><td>{row.appearance_count}次</td><td>{row.last_session}</td><td>{row.daily_volume?.status === "ok" ? Number(row.daily_volume.ratio20).toFixed(2) + "倍" : "数据不足"}</td><td><button className="subtle" disabled={disabled} onClick={() => void save("ignore", { code: row.code })} aria-label={"忽略" + row.name}>忽略</button></td></tr>)}</tbody></table></div>
      {!watch.length && <p className="empty">暂无满足条件且未忽略的观察股。</p>}
      <details><summary>已忽略的股票 · {ignored.size}只</summary><div className="restore-list">{[...ignored].map(code => <div key={String(code)}><span>{names.get(String(code)) || "股票"} <small>{String(code)}</small></span><button disabled={disabled} onClick={() => void save("restore", { code })}>恢复观察</button></div>)}</div></details>
      <div className="divider" />
      <h3>手动添加观察股</h3>
      <form onSubmit={event => submit(event, "add")} className="watch-form">
        <label>六位股票代码<input name="code" required pattern="[0-9]{6}" maxLength={6} inputMode="numeric" placeholder="例如 603127" /></label>
        <label>名称（可空）<input name="name" maxLength={80} placeholder="榜内代码自动匹配" /></label>
        <label className="wide">观察备注（可空）<input name="note" maxLength={4000} placeholder="记录关注的原因或待确认条件" /></label>
        <button type="submit" disabled={disabled} className="primary">＋ 加入观察</button>
      </form>
      <div className="manual-list">{preferences.manual_watch.map((r: Row) => <div key={r.code}><span><b>{r.name || names.get(r.code) || "名称待核验"}</b><small>{r.code}{r.note ? " · " + r.note : ""}</small></span><button className="subtle" disabled={disabled} onClick={() => void save("remove", { code: r.code })}>移除观察</button></div>)}</div>
      <p className="fineprint">网页偏好私有保存，同一账号跨设备可用；不自动覆盖电脑本机名单或GitHub云端设置。<a href={REPO + "/actions/workflows/watchlist.yml"} target="_blank" rel="noopener noreferrer">管理云端忽略／恢复 ↗</a></p>
    </section>;
  }

  function Holdings() {
    return <section className="panel" id="holdings"><div className="section-heading"><div><span className="eyebrow">PRIVATE POSITIONS</span><h2>我的实际持仓</h2></div><span className="count">{preferences.holdings.length} 只</span></div>
      <p className="muted">你手动确认的持仓独立保存。被忽略的Top10股票若仍有持仓，会继续显示持仓身份。</p>
      <div className="manual-list">{preferences.holdings.map((r: Row) => <div key={r.code}><span><b>{r.name || names.get(r.code) || "名称待核验"}</b><small>{r.code} · 成本 {r.cost ?? "未填"} · 股数 {r.shares ?? "未填"}</small></span><div className="row-actions"><button disabled={disabled} onClick={() => setEdit(r)}>编辑</button><button className="subtle" disabled={disabled} onClick={() => void save("delete_holding", { code: r.code })}>删除</button></div></div>)}</div>
      {!preferences.holdings.length && <p className="empty">尚未添加持仓。程序不会根据历史交易推断当前持仓。</p>}
      <h3>{edit ? "编辑持仓 " + edit.code : "添加持仓"}</h3>
      <form key={edit?.code ?? "new"} onSubmit={event => submit(event, "save_holding")} className="watch-form">
        <label>六位股票代码<input name="code" required pattern="[0-9]{6}" maxLength={6} inputMode="numeric" readOnly={!!edit} defaultValue={edit?.code ?? ""} /></label>
        <label>名称（可空）<input name="name" maxLength={80} defaultValue={edit?.name ?? ""} /></label>
        <label>持仓成本（可空）<input name="cost" type="number" step="any" min="0.000001" inputMode="decimal" defaultValue={edit?.cost ?? ""} /></label>
        <label>股数（可空）<input name="shares" type="number" step="1" min="1" inputMode="numeric" defaultValue={edit?.shares ?? ""} /></label>
        <label>建仓日期（可空）<input name="opened_on" type="date" defaultValue={edit?.opened_on ?? ""} /></label>
        <label>备注（可空）<input name="note" maxLength={4000} defaultValue={edit?.note ?? ""} /></label>
        <div className="row-actions"><button className="primary" disabled={disabled} type="submit">保存持仓</button>{edit && <button type="button" onClick={() => setEdit(null)}>取消编辑</button>}</div>
      </form>
      <p className="fineprint">成本、数量、备注仅存于受登录保护的私人数据库，不写入公开仓库。网页新增持仓不会自动传给电脑或云端行情扫描。完整五周期手动刷新继续使用电脑程序。</p>
    </section>;
  }

  function Monitor() {
    const published = new Map<string, Row>((data?.monitor?.rows ?? []).map((r: Row) => [r.code, r]));
    const rows = scope.filter(row => !alertsOnly || Object.values(published.get(row.code)?.frames ?? {}).some((f: any) => f.signals?.length));
    return <section className="panel"><div className="section-heading"><div><span className="eyebrow">PUBLISHED MACD MONITOR</span><h2>多周期MACD背离监控</h2></div></div>
      <p className="muted">读取原Python引擎已发布的结果。检查时间：{data?.monitor?.checked_at || "尚未发布"}；柱值=2×(DIF−DEA)。</p>
      <label className="check"><input type="checkbox" checked={alertsOnly} onChange={e => setAlertsOnly(e.target.checked)} />只看已有信号的股票</label>
      <div className="table-scroll"><table><thead><tr><th>股票／身份</th>{periods.map(p => <th key={p[0]}>{p[1]}</th>)}<th>综合状态</th></tr></thead><tbody>{rows.map(row => { const r = published.get(row.code); return <tr key={row.code}><td><b>{row.name || "名称待核验"}</b><small>{row.code} · {row.sources.join("／")}</small></td>{periods.map(p => <td key={p[0]}>{r?.frames?.[p[0]]?.label || "未有已发布结果"}</td>)}<td>{r?.combined?.label || "等待独立监控"}</td></tr>; })}</tbody></table></div>
      {!rows.length && <p className="empty">暂无符合当前筛选条件的监控结果。</p>}
      {rows.map(row => { const r = published.get(row.code); return r ? <details key={row.code}><summary>{row.name || row.code} · 行情时间、来源与信号依据</summary><div className="monitor-detail">{periods.map(([p, label]) => { const f = r.frames?.[p]; return <div key={p}><h3>{label} · {f?.label || "数据不足，无法判断"}</h3><p className="muted">行情截至 {f?.last_bar || f?.source_last_bar || "缺失"} · {f?.source || "来源未取得"}</p>{f?.error && <p className="error">{f.error}</p>}{f?.signals?.map((s: Row) => <div className="signal" key={s.id}><b>{s.label}</b><p>{s.reason}</p><p>价格 {s.price_previous} → {s.price_current}；DIF {s.dif_previous} → {s.dif_current}</p><p>柱峰 {s.histogram_peaks?.join(" → ")}；确认 {s.confirmed_at || "尚未确认"}</p><details><summary>完整信号记录</summary><pre>{JSON.stringify(s, null, 2)}</pre></details></div>)}</div>; })}</div></details> : null; })}
      <p className="fineprint">刷新本页只读取最新已发布报告，不重新运行全市场或私有持仓行情。新增手动观察／持仓若不在已发布监控范围内，会明确显示“未有已发布结果”；完整刷新可在电脑交互版进行。</p>
    </section>;
  }

  const running = ["queued", "in_progress", "running", "waiting", "pending", "requested"].includes(data?.scan_status?.status);
  return <><header className="topbar"><div className="brand"><span className="logo">TC</span><div><b>Tommy Capital</b><small>研究报告 · 私人观察</small></div></div><span className="private-badge">◈ 私有访问</span></header>
    <main className="layout"><section className="hero"><span className="eyebrow">A-SHARE RESEARCH JOURNAL</span><h1>把研究，接到观察。</h1><div className="hero-bottom"><p>{data ? <>最新数据日 <b>{data.session}</b><span className="status-badge">{data.scan_complete ? "完整报告" : "部分完成"}</span></> : "正在连接原仓库的真实报告…"}</p><button className="primary" onClick={() => void refresh()} disabled={loading}>{loading ? "正在读取…" : "↻ 刷新最新报告"}</button></div>
      {data && <p className="fineprint">扫描完成 {data.finished_at} · {data.scoring_version} · {data.commit ? "固定版本 " + data.commit.slice(0, 8) : "报告校验 " + data.report_sha256.slice(0, 8)}</p>}
    </section>
    <nav className="tabs" aria-label="报告视图">{[["report", "最新报告"], ["watch", "我的观察"], ["holdings", "实际持仓"]].map(([id, title]) => <button key={id} aria-selected={page === id} className={page === id ? "active" : ""} onClick={() => setPage(id)}>{title}</button>)}</nav>
    {error && <div role="alert" className="message error">{error}<a href="/signin-with-chatgpt?return_to=%2F" target="_top">重新登录</a></div>}
    {notice && <div role="status" className="message success">{notice}</div>}
    {running && <div className="message">有新的扫描正在处理。以下仍是已发布的 {data?.session} 报告。<a href={data?.scan_status?.run_url} target="_blank" rel="noopener noreferrer">查看实际运行</a></div>}
    {!data && !loading && <section className="panel empty">最新报告暂未取得。请稍后刷新，或<a href={REPO + "/blob/main/results/latest-report.md"} target="_blank" rel="noopener noreferrer">查看GitHub原报告</a>。</section>}
    {page === "report" && <>{data && <section className="panel market"><Markdown value={data.market} /></section>}<WatchControls /><details className="panel holdings-fold"><summary>我的实际持仓 · {preferences.holdings.length}只（添加、编辑、删除）</summary><Holdings /></details>{data && <details className="panel reasons" open><summary>前20入选理由 · 实际{data.display_count}只</summary><Markdown value={data.reasons.replace(/^## [^\n]+\n/, "")} /></details>}<Monitor /></>}
    {page === "watch" && <><WatchControls /><Monitor /></>}
    {page === "holdings" && <><Holdings /><Monitor /></>}
    <footer><span>Tommy Capital · 保留原有模型与真实数据时间</span><div><button disabled={!personal} onClick={exportBackup}>导出私人名单</button><a href={REPO + "/blob/main/docs/report-controls.md"} target="_blank" rel="noopener noreferrer">使用说明 ↗</a><a href="/signout-with-chatgpt?return_to=%2F" target="_top">退出登录</a></div></footer>
    </main></>;
}
