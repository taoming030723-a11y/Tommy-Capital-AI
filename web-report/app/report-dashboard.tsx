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
  if (!response.ok) { const error: any = new Error(value.error || "暂时无法完成，请稍后重试。"); error.choices = value.choices; throw error; }
  return value;
}

export default function ReportDashboard() {
  const [data, setData] = useState<Row | null>(null);
  const [personal, setPersonal] = useState<Personal | null>(null);
  const [page, setPage] = useState("report");
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [valuations, setValuations] = useState<Record<string, Row>>({});
  const [choices, setChoices] = useState<{action: string; rows: Row[]} | null>(null);
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
      setPersonal(result); setChoices(null); setNotice(action === "add_holding_watch" ? "持仓已加入每日持续观察；真实Top10累计次数保持原样。" : action === "exclude_watch" ? "已从Top10观察中剔除，后续报告继续生效；如仍有实际持仓，独立风险观察会保留。" : "已保存到你的私人网页名单。");
      if (action === "save_holding" || action === "delete_holding") setEdit(null);
    } catch (e: any) { setError(e instanceof Error ? e.message : "保存失败，请重试。"); if (e.choices?.length) setChoices({action, rows:e.choices}); }
    finally { setSaving(false); }
  }

  const preferences = personal?.preferences ?? emptyPreferences();
  const tracking: Row[] = data?.tracking ?? [];
  const ignored = effectiveIgnored(data?.public_ignored ?? [], preferences);
  const scope: Row[] = observationScope(tracking, preferences, data?.public_ignored ?? []);
  const watch = scope;
  const valuationCodes = Array.from(new Set([...(data?.top_candidates ?? []).map((r: Row) => r.code), ...scope.map(r => r.code)])).sort().join(",");
  useEffect(() => {
    let cancelled = false; const codes = valuationCodes.split(",").filter(Boolean); let cursor = 0;
    setValuations(data?.valuations ?? {});
    async function worker() {
      while (cursor < codes.length && !cancelled) {
        const code = codes[cursor++]; if (data?.valuations?.[code]) continue;
        try { const value = await readJson("/api/valuation?code=" + code); if (!cancelled) setValuations(old => ({...old,[code]:value})); }
        catch { if (!cancelled) setValuations(old => ({...old,[code]:{label:"估值待核验",reasons:["真实数据源暂不可用"]}})); }
      }
    }
    void worker(); void worker(); return () => { cancelled = true; };
  }, [data?.report_sha256, valuationCodes]);
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

  function QuickWindows() {
    return <section className="quick-windows" aria-label="每日报告互动窗口">
      <div className="panel"><span className="eyebrow">加入每日观察</span><h2>填写持仓股票</h2><p className="muted">确认后立即加入下面的持续观察名单，以后每天自动保留。</p><form onSubmit={event => submit(event, "add_holding_watch")}><label>股票名称或六位代码<input name="query" required maxLength={80} placeholder="例如 昭衍新药 或 603127" /></label><button className="primary" disabled={disabled} type="submit">确认加入持仓观察</button></form></div>
      <div className="panel"><span className="eyebrow">调整观察范围</span><h2>移出Top10观察</h2><p className="muted">填写名称或代码，确认后移出Top10观察并在以后报告中保留。</p><form onSubmit={event => submit(event, "exclude_watch")}><label>要移出的股票名称或代码<input name="query" required maxLength={80} placeholder="例如 基蛋生物 或 603387" /></label><button disabled={disabled} type="submit">确认移出观察名单</button></form></div>
      {choices && <div className="panel choices" role="status"><h3>请选择具体A股后确认</h3>{choices.rows.map(row => <button key={row.code} disabled={disabled} onClick={() => void save(choices.action,{query:row.code})}>{row.name}（{row.code}）</button>)}</div>}
    </section>;
  }

  function ValuationTable() {
    const all = new Map<string, Row>();
    for (const row of data?.top_candidates ?? []) all.set(row.code,{...row,identity:"综合研究榜"});
    for (const row of scope) all.set(row.code,{...all.get(row.code),...row,identity:row.sources.join("／")});
    const number = (value: unknown) => value === null || value === undefined || !Number.isFinite(Number(value)) ? "—" : Number(value).toFixed(2);
    return <section className="panel"><div className="section-heading"><div><span className="eyebrow">FUNDAMENTALS × HISTORICAL PE</span><h2>逐股当前估值</h2></div><span className="count">{all.size}只</span></div><p className="muted">历史PE位置与基本面质量各占50%，仅作相对估值观察，不改变综合评分。使用报告日完整收盘数据。</p><div className="table-scroll"><table><thead><tr><th>股票／来源</th><th>收盘价／日期</th><th>PE(TTM)／PB</th><th>近三年PE中位／分位</th><th>基本面兑现</th><th>综合判断</th></tr></thead><tbody>{[...all.values()].map(row => {const v=valuations[row.code];return <tr key={row.code}><td><b>{row.name || row.code}</b><small>{row.code} · {row.identity}</small></td><td>{number(v?.price)}元<small>{v?.price_as_of || "核对中"}</small></td><td>{number(v?.current_pe_ttm)} / {number(v?.current_pb)}</td><td>{number(v?.historical?.median_pe_ttm)}倍<small>{number(v?.historical?.percentile)}%分位</small></td><td>扣非/归母 {number(v?.fundamental?.core_profit_share_ttm)}<small>经营现金流/归母 {number(v?.fundamental?.cash_conversion_ttm)}</small></td><td><b>{v?.label || "正在核对真实估值…"}</b></td></tr>})}</tbody></table></div>{[...all.values()].map(row => {const v=valuations[row.code];return v ? <details key={row.code}><summary>{row.name || row.code} · {v.label}的依据</summary><p>{v.reasons?.join("；")}</p>{v.reference_price_range && <p>当前盈利不变的倍数换算区间：{v.reference_price_range.join("–")}元（不是目标价）。</p>}<p className="fineprint">检查 {v.checked_at}；历史区间 {v.historical?.start || "缺失"} 至 {v.historical?.end || "缺失"}，{v.historical?.samples || 0}个正PE交易日，排除当日。<a href={"https://data.eastmoney.com/gzfx/detail/" + row.code + ".html"} target="_blank" rel="noopener noreferrer">原始估值来源 ↗</a></p></details>:null})}<p className="fineprint">TTM亏损、少于120个正PE交易日、财报质量数据不足或来源价格不一致时，不硬判低估或高估。移出Top10观察不等于清仓，持仓仍独立观察。</p></section>;
  }

  function WatchControls() {
    return <section className="panel" id="watchlist">
      <div className="section-heading"><div><span className="eyebrow">CONTINUED OBSERVATION</span><h2>每日持续观察名单</h2></div><span className="count">{watch.length} 只</span></div>
      <p className="muted">包含累计三个不同正式收盘日进入Top10的股票，以及你手动确认的持仓。两种来源分别标记，不增加或伪造累计次数。</p>
      <div className="table-scroll"><table><thead><tr><th>股票</th><th>来源／真实累计次数</th><th>最近入榜</th><th>日量 / 前20日</th><th>操作</th></tr></thead><tbody>{watch.map(row => <tr key={row.code}><td><b>{row.name}</b><small>{row.code}</small></td><td>{row.sources.join("／")}<small>{row.appearance_count ? row.appearance_count + "次" : "手动加入，未达三次"}</small></td><td>{row.last_session || "手动持仓"}</td><td>{row.daily_volume?.status === "ok" ? Number(row.daily_volume.ratio20).toFixed(2) + "倍" : "数据不足"}</td><td><button className="subtle" disabled={disabled} onClick={() => void save("exclude_watch", { query: row.code })} aria-label={"忽略" + row.name}>忽略</button></td></tr>)}</tbody></table></div>
      {!watch.length && <p className="empty">暂无满足条件且未忽略的观察股。</p>}
      <details><summary>已忽略的股票 · {ignored.size}只</summary><div className="restore-list">{[...ignored].map(code => <div key={String(code)}><span>{names.get(String(code)) || "股票"} <small>{String(code)}</small></span><button disabled={disabled} onClick={() => void save("restore", { code })}>恢复观察</button></div>)}</div></details>
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
      <p className="fineprint">成本、数量、备注仅存于受登录保护的私人数据库，不写入公开仓库。持仓会自动出现在每日网页观察和估值表；完整五周期即时刷新继续使用电脑程序。</p>
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
    {page === "report" && <>{QuickWindows()}{data && <section className="panel market"><Markdown value={data.market} /></section>}{WatchControls()}{ValuationTable()}<details className="panel holdings-fold"><summary>我的实际持仓 · {preferences.holdings.length}只（添加、编辑、删除）</summary>{Holdings()}</details>{data && <details className="panel reasons" open><summary>前20入选理由 · 实际{data.display_count}只</summary><Markdown value={data.reasons.replace(/^## [^\n]+\n/, "")} /></details>}{Monitor()}</>}
    {page === "watch" && <>{QuickWindows()}{WatchControls()}{ValuationTable()}{Monitor()}</>}
    {page === "holdings" && <>{QuickWindows()}{Holdings()}{ValuationTable()}{Monitor()}</>}
    <footer><span>Tommy Capital · 保留原有模型与真实数据时间</span><div><button disabled={!personal} onClick={exportBackup}>导出私人名单</button><a href={REPO + "/blob/main/docs/report-controls.md"} target="_blank" rel="noopener noreferrer">使用说明 ↗</a><a href="/signout-with-chatgpt?return_to=%2F" target="_top">退出登录</a></div></footer>
    </main></>;
}
