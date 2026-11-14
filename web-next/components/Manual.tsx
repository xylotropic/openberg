"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, post } from "@/lib/api";
import { loadOverview, selectSymbol } from "@/lib/data";
import { fx, px } from "@/lib/format";
import { getState, setState, useStore } from "@/lib/store";
import { BookPanel } from "./Monitor";
import { useNow } from "./TopBar";

type Data = Record<string, any>;
const final = new Set(["filled", "canceled", "expired", "rejected", "replaced"]);
const isOption = (s: string) => /^[A-Z]{1,6}\d{6}[CP]\d{8}$/.test(s);

export function ManualView() {
  const draft = useStore(s => s.manualDraft);
  const ov = useStore(s => s.ov);
  const [symbol, setSymbol] = useState(draft?.symbol || getState().symbol || "SPY");
  const [side, setSide] = useState<"buy" | "sell">(draft?.side || "buy");
  const [qty, setQty] = useState(draft?.qty || "1");
  const [type, setType] = useState(draft?.limit_price || isOption(symbol) ? "limit" : "market");
  const [limit, setLimit] = useState(draft?.limit_price || "");
  const [tif, setTif] = useState(symbol.includes("/") ? "gtc" : "day");
  const [review, setReview] = useState<Data | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ordersError, setOrdersError] = useState("");
  const [data, setData] = useState<Data | null>(null);
  const revision = useRef(0);
  const now = useNow();
  const option = isOption(symbol), crypto = symbol.includes("/");
  const edited = () => { revision.current++; setReview(null); setError(""); };
  useEffect(() => {
    if (!draft) return;
    revision.current++;
    setSymbol(draft.symbol); setSide(draft.side || "buy"); setQty(draft.qty || "1");
    setLimit(draft.limit_price || ""); setType(draft.limit_price || isOption(draft.symbol) ? "limit" : "market");
    setTif(draft.symbol.includes("/") ? "gtc" : "day"); setReview(null); setError("");
  }, [draft]);
  const load = async () => {
    try { setData(await api("/api/manual/orders")); setOrdersError(""); }
    catch (e: any) { setOrdersError(e.message); }
  };
  useEffect(() => {
    void load();
    const timer = setInterval(() => { if (!document.hidden) void load(); }, 5000);
    return () => clearInterval(timer);
  }, []);
  const preview = async (e: FormEvent) => {
    e.preventDefault(); if (busy) return;
    setBusy(true); setError(""); setReview(null);
    const version = revision.current;
    try {
      const r = await post("/api/manual/preview", { symbol, side, qty, type: option ? "limit" : type,
        limit_price: limit, time_in_force: crypto ? tif : "day" });
      if (version === revision.current) setReview(r);
      if ((ov?.settings.watchlist_symbols || ov?.settings.symbols).includes(symbol)) selectSymbol(symbol);
    } catch (e: any) { if (version === revision.current) setError(e.message); }
    finally { setBusy(false); }
  };
  const send = async () => {
    if (!review || busy) return;
    setBusy(true); setError("");
    try { setReview(await post("/api/manual/orders", { id: review.id })); await load(); await loadOverview(); }
    catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  };
  const cancel = async (id: string) => {
    setBusy(true); setOrdersError("");
    try { await post(`/api/manual/orders/${id}/cancel`); await load(); }
    catch (e: any) { setOrdersError(e.message); }
    finally { setBusy(false); }
  };
  const closePosition = async (p: Data) => {
    if (p.owned) {
      if (!confirm(`Close model-owned ${p.symbol} position on Alpaca PAPER?`)) return;
      setBusy(true);
      try { await post("/api/close", { symbol: p.symbol }); await load(); await loadOverview(); }
      catch (e: any) { setOrdersError(e.message); }
      finally { setBusy(false); }
    } else {
      setState({ manualDraft: { symbol: p.symbol, side: p.side === "long" ? "sell" : "buy", qty: p.available_qty || String(p.size) } });
      if ((ov?.settings.watchlist_symbols || ov?.settings.symbols).includes(p.symbol)) selectSymbol(p.symbol);
    }
  };
  const request = review?.request, details = review?.review;
  const expired = details && now / 1000 > details.expires_at;
  return <main className="view on research-view manual-view">
    <div className="toolbar"><b>Trade</b><span className="tag long">ALPACA PAPER</span><span className="muted">{data?.account?.account_id || ov?.account?.account_id}</span><span className="ph-r">BUYING POWER ${fx(data?.account?.wallet?.available ?? ov?.account?.wallet?.available)}</span></div>
    <div className="manual-grid">
      <section className="pnl manual-ticket"><div className="ph"><span className="code">ORDER</span>Manual order</div>
        <form onSubmit={preview}>
          <fieldset disabled={busy}>
            <label>SYMBOL<input aria-label="Trade symbol" list="trade-symbols" value={symbol} onChange={e => { edited(); const s = e.target.value.toUpperCase(); setSymbol(s); setTif(s.includes("/") ? "gtc" : "day"); if (isOption(s)) setType("limit"); }} onBlur={() => { if ((ov?.settings.watchlist_symbols || ov?.settings.symbols).includes(symbol)) selectSymbol(symbol); }} /></label>
            <datalist id="trade-symbols">{(ov?.settings.watchlist_symbols || ov?.settings.symbols).map((s: string) => <option key={s}>{s}</option>)}</datalist>
            <div className="seg manual-side"><button type="button" className={side === "buy" ? "on up" : ""} onClick={() => { edited(); setSide("buy"); }}>BUY</button><button type="button" className={side === "sell" ? "on dn" : ""} onClick={() => { edited(); setSide("sell"); }}>SELL</button></div>
            <label>{option ? "CONTRACTS" : "QUANTITY"}<input aria-label="Trade quantity" type="number" min="0" step="any" value={qty} onChange={e => { edited(); setQty(e.target.value); }} /></label>
            <label>ORDER TYPE<select aria-label="Trade order type" value={option ? "limit" : type} onChange={e => { edited(); setType(e.target.value); }}><option value="limit">LIMIT</option>{!option && <option value="market">MARKET</option>}</select></label>
            {(type === "limit" || option) && <label>{option ? "LIMIT PREMIUM $" : "LIMIT PRICE $"}<input aria-label="Trade limit price" type="number" min="0" step="any" value={limit} onChange={e => { edited(); setLimit(e.target.value); }} /></label>}
            <label>TIME IN FORCE<select aria-label="Trade time in force" value={crypto ? tif : "day"} onChange={e => { edited(); setTif(e.target.value); }}>{crypto ? <><option value="gtc">GTC</option><option value="ioc">IOC</option></> : <option value="day">DAY</option>}</select></label>
            <button className="kbtn amber" type="submit">{busy ? "WORKING…" : "REVIEW PAPER ORDER"}</button>
          </fieldset>
        </form>
        {error && <div className="alert crit" role="alert">{error}</div>}
        {review && <div className="order-review" role="status">
          <b className={request.side === "buy" ? "up" : "dn"}>{request.side.toUpperCase()} {request.qty} {request.symbol}</b>
          <div>{request.type.toUpperCase()}{request.limit_price ? ` $${request.limit_price}` : ""} · {request.time_in_force.toUpperCase()}</div>
          <div>EST. NOTIONAL <b>${fx(details.estimated_notional)}</b>{details.multiplier > 1 ? ` · ×${details.multiplier}` : ""}</div>
          <div className="muted">{details.feed} · {details.reducing ? "REDUCE POSITION" : "OPEN / ADD"}</div>
          <div className="amb">MANUAL POSITION · NO AUTOMATIC STOP</div>
          {option && <div className="amb">INDICATIVE QUOTES · LONG PREMIUM AT RISK · EXERCISE MAY CREATE STOCK POSITIONS</div>}
          {review.status === "preview" ? <>
            {!details.market_open && <div className="muted">US MARKET CLOSED</div>}
            {expired && <div className="dn">REVIEW EXPIRED</div>}
            <button className={`kbtn ${request.side === "buy" ? "amber" : "red"}`} disabled={busy || expired || !details.market_open} onClick={() => void send()}>SEND PAPER {request.side.toUpperCase()}</button>
          </> : <><div className={review.status === "rejected" || review.status === "unknown" ? "dn" : "up"}>PAPER ORDER · {review.status.toUpperCase()}</div>{review.error && <div className="dn">{review.error}</div>}{!final.has(review.status) && <button className="kbtn" disabled={busy} onClick={() => void send()}>CHECK STATUS</button>}</>}
        </div>}
      </section>
      <section className="pnl manual-book"><BookPanel /></section>
    </div>
    {ordersError && <div className="alert crit" role="alert">{ordersError}</div>}
    <section className="pnl"><div className="ph"><span className="code">POS</span>Positions</div><div className="research-table"><table className="t"><thead><tr><th>Symbol</th><th>Side</th><th className="r">Available qty</th><th className="r">Average</th><th className="r">Mark</th><th className="r">P/L</th><th>Management</th><th /></tr></thead><tbody>{data?.positions.map((p: Data) => <tr key={p.symbol}><td className="sym">{p.symbol}</td><td className={p.side === "long" ? "up" : "dn"}>{p.side === "long" ? "LONG" : "SHORT"}</td><td className="r">{p.available_qty || p.size}</td><td className="r">{px(p.entry)}</td><td className="r">{px(p.mark)}</td><td className={`r ${p.upnl >= 0 ? "up" : "dn"}`}>${fx(p.upnl)}</td><td>{p.owned ? "MODEL" : "MANUAL"}</td><td><button className="mini" disabled={busy} onClick={() => void closePosition(p)}>{p.owned ? "CLOSE MODEL" : "CLOSE / REDUCE"}</button></td></tr>)}</tbody></table></div>{!data?.positions.length && <div className="empty">No positions</div>}</section>
    <section className="pnl"><div className="ph"><span className="code">ORD</span>Manual orders</div><div className="research-table"><table className="t"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Type</th><th className="r">Qty</th><th className="r">Filled</th><th className="r">Average fill</th><th>Status</th><th /></tr></thead><tbody>{data?.orders.map((o: Data) => <tr key={o.id}><td>{new Date(o.created_ts * 1000).toLocaleString()}</td><td className="sym">{o.request.symbol}</td><td className={o.request.side === "buy" ? "up" : "dn"}>{o.request.side.toUpperCase()}</td><td>{o.request.type.toUpperCase()}{o.request.limit_price ? ` $${o.request.limit_price}` : ""}</td><td className="r">{o.request.qty}</td><td className="r">{o.broker?.filled_qty || "0"}</td><td className="r">{px(o.broker?.filled_avg_price)}</td><td title={o.error || undefined}>{o.status.toUpperCase()}{o.error && <div className="dn">{o.error}</div>}</td><td>{!final.has(o.status) && <button className="mini" disabled={busy} onClick={() => void cancel(o.id)}>CANCEL</button>}</td></tr>)}</tbody></table></div>{!data?.orders.length && <div className="empty">No manual orders</div>}</section>
  </main>;
}
