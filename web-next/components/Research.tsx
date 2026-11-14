"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";
import { fx, px } from "@/lib/format";
import { loadOverview } from "@/lib/data";
import { useStore } from "@/lib/store";

type Data = Record<string, any>;
const ErrorLine = ({ text }: { text: string }) => text ? <div className="alert crit" role="alert">{text}</div> : null;
const Link = ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href} target="_blank" rel="noopener noreferrer">{children} ↗</a>;

export function NewsroomView() {
  const [symbol, setSymbol] = useState("");
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pages, setPages] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState(0);
  const load = async (token: string | null = null) => {
    setBusy(true); setError("");
    try { setData(await api(`/api/research/news?symbol=${encodeURIComponent(symbol)}${token ? `&page_token=${encodeURIComponent(token)}` : ""}`)); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, []);
  return <main className="view on research-view">
    <div className="toolbar"><b>Newsroom</b><span className="tag long">ALPACA NEWS PLUGIN</span>
      <input aria-label="News symbols" placeholder="All markets, or AAPL,BTCUSD" value={symbol} onChange={e => setSymbol(e.target.value.toUpperCase())} />
      <button className="kbtn amber" disabled={busy} onClick={() => { setPage(0); setPages([null]); void load(); }}>{busy ? "LOADING…" : "LOAD NEWS"}</button>
      <span className="muted">Source and publication time are retained on every headline.</span></div>
    <ErrorLine text={error} />
    <section className="pnl"><div className="ph"><span className="code">WIRE</span>Stocks · crypto · market news<span className="ph-r">{data?.items.length ?? 0} articles · page {page + 1}</span></div>
      <div className="newsroom-list">{data?.items.map((n: Data) => <article key={n.id}>
        <div className="news-meta">{n.source} · {new Date(n.created_at).toLocaleString()} <span className="amb">{n.symbols?.join(" · ")}</span></div>
        <h3><Link href={n.url}>{n.headline}</Link></h3><p>{n.summary}</p>
      </article>)}</div>
      {!busy && !error && !data?.items.length && <div className="empty">No articles for this filter.</div>}
    </section>
    <div className="research-pagination"><button className="kbtn" disabled={busy || !page} onClick={() => { const p = page - 1; setPage(p); void load(pages[p]); }}>PREVIOUS</button>
      <span>PAGE {page + 1}</span><button className="kbtn" disabled={busy || !data?.next_page_token} onClick={() => { const token = data!.next_page_token; setPages([...pages.slice(0, page + 1), token]); setPage(page + 1); void load(token); }}>NEXT</button></div>
  </main>;
}

export function FilingsView() {
  const [query, setQuery] = useState("AAPL");
  const [data, setData] = useState<Data | null>(null);
  const [form, setForm] = useState("");
  const [archive, setArchive] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [offset, setOffset] = useState(0);
  const load = async (start = 0, selectedArchive = archive, selectedForm = form) => {
    setBusy(true); setError("");
    try { const d = await api(`/api/research/filings?query=${encodeURIComponent(query)}&offset=${start}&archive=${encodeURIComponent(selectedArchive)}&form=${encodeURIComponent(selectedForm)}`); setData(d); setOffset(start); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, []);
  return <main className="view on research-view">
    <div className="toolbar"><b>All filings</b><span className="tag long">SEC EDGAR PLUGIN · FREE</span>
      <input aria-label="Company ticker or CIK" value={query} onChange={e => setQuery(e.target.value.toUpperCase())} placeholder="Ticker or CIK" />
      <button className="kbtn amber" disabled={busy} onClick={() => { setArchive(""); setForm(""); void load(0, "", ""); }}>{busy ? "LOADING…" : "LOAD FILINGS"}</button>
      <select aria-label="Filing form" value={form} onChange={e => { setForm(e.target.value); void load(0, archive, e.target.value); }}><option value="">ALL FORM TYPES</option>{data?.forms.map((f: string) => <option key={f}>{f}</option>)}</select>
      <select aria-label="Filing history archive" value={archive} onChange={e => { setArchive(e.target.value); setForm(""); void load(0, e.target.value, ""); }}><option value="">CURRENT MANIFEST</option>{data?.archives.map((a: Data) => <option key={a.name} value={a.name}>{a.filingFrom} → {a.filingTo} · {a.filingCount} filings</option>)}</select>
    </div>
    <ErrorLine text={error} />
    <section className="pnl"><div className="ph"><span className="code">EDGAR</span>{data?.company || "Issuer filings"}<span className="ph-r">CIK {data?.cik || "—"} · {data?.total ?? 0} filings in this archive</span></div>
      <div className="research-note">All form types, amendments, ownership disclosures, prospectuses, exhibits and older manifests remain accessible. Select each history archive for the issuer’s complete available SEC submission history. This view covers SEC EDGAR.</div>
      <div className="research-table"><table className="t"><thead><tr><th>Filed</th><th>Form</th><th>Report date</th><th>Description</th><th>Accession</th><th>Documents</th></tr></thead><tbody>{data?.items.map((f: Data) => <tr key={f.accession}>
        <td>{f.filed}</td><td className="amb">{f.form}</td><td>{f.report_date || "—"}</td><td>{f.description || f.document || "—"}</td><td>{f.accession}</td>
        <td><Link href={f.url}>OPEN</Link> · <Link href={f.filing_url}>ALL DOCUMENTS</Link></td></tr>)}</tbody></table></div>
      {!busy && !error && !data?.items.length && <div className="empty">No filings for this selection.</div>}
    </section>
    <div className="research-pagination"><button className="kbtn" disabled={busy || !offset} onClick={() => void load(Math.max(0, offset - 100))}>PREVIOUS</button>
      <span>{data?.total ? `${offset + 1}–${Math.min(offset + 100, data.total)} OF ${data.total}` : "0 FILINGS"}</span>
      <button className="kbtn" disabled={busy || data?.next_offset == null} onClick={() => void load(data!.next_offset)}>NEXT</button>
      <span className="muted">Market-wide public archive: <Link href="https://www.sec.gov/Archives/edgar/daily-index/">Daily filing indexes</Link> · <Link href="https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip">Full submissions dataset</Link></span></div>
  </main>;
}

function optionDetails(symbol: string) {
  const m = symbol.match(/^(.*?)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/);
  return m ? { expiry: `20${m[2]}-${m[3]}-${m[4]}`, type: m[5] === "C" ? "CALL" : "PUT", strike: Number(m[6]) / 1000 } : { expiry: "—", type: "—", strike: 0 };
}

export function OptionsView() {
  const account = useStore(s => s.ov?.account);
  const [underlying, setUnderlying] = useState("SPY");
  const [expiry, setExpiry] = useState("");
  const [kind, setKind] = useState("");
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState("");
  const [limit, setLimit] = useState("");
  const [qty, setQty] = useState(1);
  const [result, setResult] = useState("");
  const load = async (token = "") => {
    setBusy(true); setError("");
    try { setData(await api(`/api/options?underlying=${encodeURIComponent(underlying)}&expiry=${expiry}&type=${kind}${token ? `&page_token=${encodeURIComponent(token)}` : ""}`)); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, []);
  const submit = async () => {
    const debit = Number(limit) * qty * 100;
    if (!confirm(`Submit Alpaca PAPER order: BUY ${qty} ${selected} at a limit premium of $${limit}? Maximum debit: $${debit.toFixed(2)}. Quotes shown are indicative. The whole premium is at risk.`)) return;
    setBusy(true); setError(""); setResult("");
    try { const r = await post("/api/options/order", { symbol: selected, qty, limit_price: Number(limit) }); setResult(`PAPER ORDER ${r.order_id} · ${r.status}`); await loadOverview(); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  const open = !!account?.market_clock?.is_open;
  return <main className="view on research-view">
    <div className="toolbar"><b>Options</b><span className="tag long">ALPACA PAPER · LEVEL {account?.options_trading_level ?? "—"}</span>
      <input aria-label="Options underlying" value={underlying} onChange={e => setUnderlying(e.target.value.toUpperCase())} />
      <input aria-label="Options expiration" type="date" value={expiry} onChange={e => setExpiry(e.target.value)} />
      <select aria-label="Option type" value={kind} onChange={e => setKind(e.target.value)}><option value="">CALLS + PUTS</option><option value="call">CALLS</option><option value="put">PUTS</option></select>
      <button className="kbtn amber" disabled={busy} onClick={() => void load()}>{busy ? "LOADING…" : "LOAD CHAIN"}</button></div>
    <div className="research-note amb">FREE INDICATIVE FEED · Trades are delayed and quotes are modified. Automated options entries are paused. The paper desk supports buying single calls and puts with a fixed premium limit.</div>
    <ErrorLine text={error} />
    <section className="pnl"><div className="ph"><span className="code">CHAIN</span>{data?.underlying || underlying} options<span className="ph-r">{open ? "US MARKET OPEN" : "US MARKET CLOSED"} · {Object.keys(data?.items || {}).length} contracts</span></div>
      <div className="research-table"><table className="t"><thead><tr><th>Contract</th><th>Expiry</th><th>Type</th><th className="r">Strike</th><th className="r">Bid</th><th className="r">Ask</th><th className="r">Last</th><th className="r">IV</th><th className="r">Delta</th><th className="r">Theta</th><th /></tr></thead><tbody>{Object.entries<Data>(data?.items || {}).map(([s, o]) => {
        const d = optionDetails(s), q = o.latestQuote || {}, g = o.greeks || {};
        return <tr key={s} className={s === selected ? "sel" : ""}><td className="sym">{s}</td><td>{d.expiry}</td><td className="amb">{d.type}</td><td className="r">{fx(d.strike)}</td><td className="r">{px(q.bp)}</td><td className="r">{px(q.ap)}</td><td className="r">{px(o.latestTrade?.p)}</td><td className="r">{fx(o.impliedVolatility, 3)}</td><td className="r">{fx(g.delta, 3)}</td><td className="r">{fx(g.theta, 3)}</td><td><button className="mini" onClick={() => { setSelected(s); setLimit(String(q.ap || o.latestTrade?.p || "")); setResult(""); }}>SELECT</button></td></tr>;
      })}</tbody></table></div>
      <div className="research-pagination"><button className="kbtn" disabled={busy || !data?.next_page_token} onClick={() => void load(data!.next_page_token)}>NEXT CONTRACTS</button></div>
    </section>
    <section className="pnl option-ticket"><div className="ph"><span className="code">ORDER</span>Long option · paper limit order</div>
      <div className="toolbar"><span className="amb">{selected || "Select a contract from the chain"}</span><label>CONTRACTS <input aria-label="Option contracts quantity" type="number" min="1" step="1" value={qty} onChange={e => setQty(Number(e.target.value))} /></label>
        <label>LIMIT PREMIUM $ <input aria-label="Option limit premium" type="number" min="0.01" step="0.01" value={limit} onChange={e => setLimit(e.target.value)} /></label><span>MAX DEBIT ${fx(Number(limit) * qty * 100)}</span>
        <button className="kbtn amber" disabled={busy || !open || !selected || !limit} onClick={() => void submit()}>REVIEW PAPER BUY</button></div>
      {!open && <p className="muted">The US market is closed. Stock and option order execution can be verified when it opens. Crypto remains available 24/7.</p>}
      {result && <p className="up">{result}</p>}
    </section>
  </main>;
}
