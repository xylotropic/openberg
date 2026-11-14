"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";

const stamp = (v?: number | null) => v ? new Date(v * 1000).toLocaleString() : "—";
const cash = (v?: number | null) => v == null ? "—" : v.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const number = (v?: number | null, digits = 2) => v == null ? "INSUFFICIENT DATA" : v.toFixed(digits);
const short = (v?: string | null) => v ? v.slice(0, 12) : "—";

function Nav({ rows }: { rows: any[] }) {
  if (!rows.length) return <div className="empty">No completed forward sessions.</div>;
  const vals = rows.flatMap(r => [r.gross_nav, r.net_nav]);
  const min = Math.min(...vals) * .999, max = Math.max(...vals) * 1.001;
  const line = (key: string) => rows.map((r, i) => `${25 + i * 950 / Math.max(1, rows.length - 1)},${160 - 140 * (r[key] - min) / (max - min || 1)}`).join(" ");
  return <svg className="alpha-nav-chart" viewBox="0 0 1000 190" role="img" aria-label="Daily broker-booked and estimated economic strategy NAV"><line x1="25" x2="975" y1="160" y2="160" stroke="#333" /><polyline points={line("gross_nav")} fill="none" stroke="#4da3ff" strokeWidth="2" /><polyline points={line("net_nav")} fill="none" stroke="#ff9b00" strokeWidth="2" /><text x="25" y="185" fill="#888" fontSize="13">{rows[0].session}</text><text x="975" y="185" textAnchor="end" fill="#888" fontSize="13">{rows[rows.length - 1].session}</text></svg>;
}

export function AlphaView() {
  const [data, setData] = useState<any>(null), [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<{ name: string; data: any } | null>(null);
  const load = async () => { try { setData(await api("/api/alpha")); setError(""); } catch (e: any) { setError(e.message); } };
  useEffect(() => { void load(); const id = setInterval(() => { if (!document.hidden) void load(); }, 5000); return () => clearInterval(id); }, []);
  const control = async (action: string) => {
    if (action === "close_owned" && !confirm("Close only economic-link strategy positions and pause its new entries?")) return;
    setBusy(true);
    try { setData(await post("/api/alpha/control", { action })); setError(""); }
    catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  };
  const evidence = async (id: string) => { try { setDetail({ name: "Source evidence", data: await api(`/api/alpha/evidence/${id}`) }); } catch (e: any) { setError(e.message); } };
  const classification = async (id: string) => { try { setDetail({ name: "Drex classification", data: await api(`/api/alpha/classification/${id}`) }); } catch (e: any) { setError(e.message); } };
  if (!data) return <main className="view on alpha-view"><div className="toolbar"><b>Economic links</b></div><div className={error ? "alert crit" : "empty"}>{error || "Loading strategy…"}</div></main>;
  const v = data.version, metrics = data.metrics, nav = data.live_nav;
  return <main className="view on alpha-view">
    <div className="toolbar"><b>Economic links</b><span className="tag flat">EXPERIMENTAL · PAPER</span><span>{v.id}</span><span className={v.enabled ? "up" : "amb"}>{v.state}</span>
      <button className="kbtn amber" disabled={busy || !!v.enabled} onClick={() => void control(v.armed_at ? "resume" : "arm")}>{v.armed_at ? "RESUME ENTRIES" : "ARM PAPER"}</button>
      <button className="kbtn" disabled={busy || !v.enabled} onClick={() => void control("pause")}>PAUSE ENTRIES</button>
      <button className="kbtn" disabled={busy} onClick={() => void control("cancel_entries")}>CANCEL OWNED ENTRIES</button>
      <button className="kbtn red" disabled={busy} onClick={() => void control("close_owned")}>CLOSE STRATEGY BOOK</button>
    </div>
    {error && <div className="alert crit" role="alert">{error}</div>}
    {v.reason && <div className="alert">{v.reason}</div>}
    {data.accounting_gap && <div className="alert crit">ACCOUNTING GAP · {data.accounting_gap.reason}</div>}
    {data.ownership_quarantine && <div className="alert crit">OWNERSHIP QUARANTINE · {data.ownership_quarantine.reason}</div>}
    <div className="alpha-stats">{[
      ["ACCOUNT", data.account_id], ["CAPITAL", cash(v.allocation)], ["ENTRY GATE", data.entry_gate], ["NEXT WINDOW", stamp(data.next_window)],
      ["BROKER-BOOKED NAV", cash(nav?.gross_nav)], ["ESTIMATED NET NAV", cash(nav?.net_nav)], ["GROSS EXPOSURE", cash(nav?.gross_exposure)],
      ["ESTIMATED BETA", number(nav?.beta, 3)], ["SOURCES / DECISIONS", `${data.counts.sources} / ${data.counts.decisions}`],
      ["ORDER ATTEMPTS / FILLS", `${data.counts.orders_attempted} / ${data.counts.fills}`], ["DREX MONTH", `${cash(data.budget.month_spent_usd)} / ${cash(data.budget.month_cap_usd)}`],
      ["SHARED REQUESTS / HOUR", `${data.budget.shared_hour_used} / ${data.budget.shared_hour_limit}`]
    ].map(([label, value]) => <div key={label}><label>{label}</label><strong>{value}</strong></div>)}</div>
    <div className="alpha-columns">
      <section className="pnl"><div className="ph"><span className="code">NAV</span>Marked capital<span className="ph-r"><span style={{ color: "#4da3ff" }}>BROKER-BOOKED</span> · <span className="amb">ECONOMIC NET</span></span></div><Nav rows={data.nav} />
        <div className="alpha-metrics"><span>SHARPE <b>{number(metrics.sharpe)}</b></span><span>SORTINO <b>{number(metrics.sortino)}</b></span><span>DAILY ALPHA <b>{number(metrics.alpha?.alpha_daily, 5)}</b></span><span>DRAWDOWN <b>{metrics.max_drawdown == null ? "—" : `${(metrics.max_drawdown * 100).toFixed(2)}%`}</b></span><span>SESSIONS <b>{metrics.observations ?? 0}</b></span><span>INDEPENDENT EVENTS <b>{metrics.independent_events ?? 0}</b></span></div>
        <details className="alpha-notes"><summary>Measurement conventions</summary><p>Daily marked returns include idle sessions and open losses. Cash benchmark and Sortino target: 0%. Annualization: 252 sessions. Sharpe uncertainty: 10-session block bootstrap. Alpha controls: SPY and XLK when 60 aligned sessions exist.</p><p>Borrow: assumed 3% per year. Financing: assumed 6%. Extra latency/slippage: 2 bp per fill. Fill prices include the spread. Corporate-action gaps remain visible.</p></details>
      </section>
      <section className="pnl"><div className="ph"><span className="code">OPS</span>Workers<span className="ph-r">READINESS {data.readiness?.ready ? "CHECKED" : "PENDING"}</span></div><div className="alpha-scroll"><table><thead><tr><th>WORKER</th><th>LAST SUCCESS</th><th>NEXT</th><th>STATE</th></tr></thead><tbody>{data.heartbeats.map((h: any) => <tr key={h.id}><td>{h.id}</td><td>{stamp(h.last_success)}</td><td>{stamp(h.next_run)}</td><td className={h.error || h.stale ? "dn" : "up"} title={h.error || ""}>{h.stale ? "STALE" : h.error || "READY"}</td></tr>)}</tbody></table></div><details className="alpha-notes"><summary>Coverage and host</summary>{data.limitations.map((x: string) => <p key={x}>{x}</p>)}<p>Next forward review: {stamp(v.next_review)}</p><p>Shared account: manual, intraday and economic-link orders use exclusive symbol/hedge reservations.</p></details></section>
    </div>
    <section className="pnl"><div className="ph"><span className="code">BOOK</span>Owned positions and exit obligations</div><div className="alpha-scroll"><table><thead><tr><th>SYMBOL</th><th>QUANTITY</th><th>BASIS</th><th>REALIZED</th><th>EXIT</th><th>GROUP</th></tr></thead><tbody>{data.positions.map((p: any) => <tr key={p.id}><td>{p.symbol}</td><td>{p.qty}</td><td>{cash(p.average)}</td><td>{cash(p.realized)}</td><td>{stamp(p.exit_deadline)}</td><td>{short(p.group_id)}</td></tr>)}</tbody></table>{!data.positions.length && <div className="empty">No strategy-owned positions.</div>}<table><thead><tr><th>GROUP</th><th>STATE</th><th>ENTRY CUTOFF</th><th>RECOVERY CUTOFF</th><th>EXIT</th><th>REASON</th></tr></thead><tbody>{data.groups.map((g: any) => <tr key={g.id}><td>{short(g.id)}</td><td>{g.state}</td><td>{stamp(g.entry_deadline)}</td><td>{stamp(g.recovery_deadline)}</td><td>{stamp(g.exit_deadline)}</td><td>{g.reason || "—"}</td></tr>)}</tbody></table></div></section>
    <section className="pnl"><div className="ph"><span className="code">GRAPH</span>Documented relationships</div><div className="alpha-scroll"><table><thead><tr><th>CUSTOMER → SUPPLIER</th><th>REVENUE EXPOSURE</th><th>STATE</th><th>FIRST OBSERVED</th><th>EXPIRES</th><th>EVIDENCE</th><th>DREX</th></tr></thead><tbody>{data.relationships.map((r: any) => <tr key={r.id}><td>{r.customer} → {r.supplier}</td><td>{r.exposure_pct != null ? `${r.exposure_pct}%` : r.exposure_floor_pct != null ? `≥${r.exposure_floor_pct}%` : "UNKNOWN"}</td><td className={r.state === "verified" ? "up" : "amb"}>{r.state}</td><td>{stamp(r.observed_at)}</td><td>{stamp(r.expires_at)}</td><td><button className="mini" onClick={() => void evidence(r.source_id)}>SOURCE</button></td><td>{r.classification_id && <button className="mini" onClick={() => void classification(r.classification_id)}>PROBABILITIES</button>}</td></tr>)}</tbody></table></div></section>
    <section className="pnl"><div className="ph"><span className="code">DEC</span>Decisions and abstentions</div><div className="alpha-scroll"><table><thead><tr><th>DECIDED</th><th>ACTION</th><th>REASON</th><th>ORIGINAL PUBLICATION</th><th>OBSERVED</th><th>SOURCE</th><th>MODEL</th></tr></thead><tbody>{data.decisions.map((d: any) => <tr key={d.id}><td>{stamp(d.decided_at)}</td><td>{d.action}</td><td>{d.reason || "Frozen semantic gates passed"}</td><td>{stamp(d.evidence_json.original_publication)}</td><td>{stamp(d.evidence_json.observed_at)}</td><td><button className="mini" onClick={() => void evidence(d.source_id)}>SOURCE</button></td><td>{d.classification_id && <button className="mini" onClick={() => void classification(d.classification_id)}>DREX</button>}</td></tr>)}</tbody></table>{!data.decisions.length && <div className="empty">Waiting for source collection.</div>}</div></section>
    <section className="pnl"><div className="ph"><span className="code">ORD</span>Strategy order journal</div><div className="alpha-scroll"><table><thead><tr><th>CREATED</th><th>SYMBOL</th><th>ROLE</th><th>SIDE</th><th>QTY</th><th>STATE</th><th>CLIENT ID</th><th>ERROR</th></tr></thead><tbody>{data.orders.map((o: any) => <tr key={o.id}><td>{stamp(o.created_at)}</td><td>{o.symbol}</td><td>{o.role}</td><td>{o.request_json.side}</td><td>{o.request_json.qty}</td><td>{o.status}</td><td>{o.client_order_id}</td><td>{o.error || "—"}</td></tr>)}</tbody></table>{!data.orders.length && <div className="empty">No strategy orders.</div>}</div></section>
    <div className="alpha-columns"><section className="pnl"><div className="ph"><span className="code">RISK</span>Frozen limits and entry rejections</div><div className="pb"><div className="alpha-metrics"><span>COMPANY 2.5%</span><span>EVENT GROSS 10%</span><span>SECTOR GROSS 25%</span><span>GROSS 100%</span><span>NET ±20%</span><span>BETA ±0.15</span><span>QUOTE ≤5s</span><span>SPREAD ≤15 bp</span><span>HOLD 10 SESSIONS</span></div>{data.rejections.map((r: any) => <p key={r.id}>{stamp(r.updated_at)} · {r.value_json.reason}</p>)}</div></section>
      <section className="pnl"><div className="ph"><span className="code">LAB</span>Registered shadow experiments</div><div className="alpha-scroll"><table><thead><tr><th>VARIANT</th><th>STATE</th><th>RESULT</th></tr></thead><tbody>{data.experiments.map((x: any) => <tr key={x.id}><td>{x.name}</td><td>{x.state}</td><td>{x.result_json?.status || "Awaiting prospective labels"}</td></tr>)}</tbody></table></div></section></div>
    {detail && <section className="pnl alpha-detail"><div className="ph"><span className="code">AUDIT</span>{detail.name}<span className="ph-r"><button className="mini" onClick={() => setDetail(null)}>CLOSE</button></span></div>{detail.data.source?.url && <a href={detail.data.source.url} target="_blank" rel="noreferrer">Original source ↗</a>}<pre>{JSON.stringify(detail.data, null, 2)}</pre></section>}
  </main>;
}
