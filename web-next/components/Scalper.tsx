"use client";
import { useEffect, useState } from "react";
import { api, post } from "@/lib/api";
import { loadOverview } from "@/lib/data";

const stamp = (v?: number) => v ? new Date(v * 1000).toLocaleTimeString() : "—";
export function ScalperView() {
  const [data, setData] = useState<any>(null), [error, setError] = useState("");
  const load = async () => { try { setData(await api("/api/scalper")); setError(""); } catch (e: any) { setError(e.message); } };
  useEffect(() => { void load(); const id = setInterval(() => { if (!document.hidden) void load(); }, 5000); return () => clearInterval(id); }, []);
  const toggle = async () => { try { await post("/api/trading", { enabled: !data.enabled }); await Promise.all([load(), loadOverview()]); } catch (e: any) { setError(e.message); } };
  return <main className="view on alpha-view">
    <div className="toolbar"><b>{data?.session || "Session strategy"}</b><span className="tag flat">ALPACA PAPER</span><span>{data?.active_symbols?.join(" · ")}</span><span className={data?.enabled ? "up" : "amb"}>{data?.enabled ? "ENTRIES ON" : "ENTRIES PAUSED"}</span><button className="kbtn amber" disabled={!data?.version} onClick={() => void toggle()}>{data?.enabled ? "PAUSE ENTRIES" : "RESUME ENTRIES"}</button></div>
    {(error || data?.error) && <div className="alert crit" role="alert">{error || data.error}</div>}
    {data?.version && <>
      <div className="alpha-stats">{[
        ["SCAN", "30 seconds"], ["DREX PACE", `${data.drex_interval_seconds} seconds`], ["NEXT DREX", stamp(data.next_call_at)],
        ["MONTHLY DREX", `$${data.month_spent_usd.toFixed(4)} / $${data.monthly_cap_usd.toFixed(2)}`],
        ["ALLOCATION", "5% / position"], ["STOP", "1.5 × ATR · floor applies"], ["TIME EXIT", "30 minutes"], ["LAST SCAN", stamp(data.last_scan_at)]
      ].map(([label, value]) => <div key={label}><label>{label}</label><strong>{value}</strong></div>)}</div>
      <section className="pnl"><div className="ph"><span className="code">DATA</span>Market readiness</div><table><thead><tr><th>SYMBOL</th><th>ACTIONS</th><th>STATE</th></tr></thead><tbody>{data.symbols.map((s: string) => <tr key={s}><td>{s}</td><td>{s.includes("/") ? "LONG · HOLD · EXIT" : "LONG · SHORT · HOLD · EXIT"}</td><td className={data.readiness[s] === "READY" ? "up" : "amb"}>{data.readiness[s] || "WARMING UP"}</td></tr>)}</tbody></table></section>
      <section className="pnl"><div className="ph"><span className="code">DREX</span>Last batch<span className="ph-r">{data.last_call?.state?.toUpperCase() || "WAITING"}</span></div><div className="pb">{data.last_call ? `${stamp(data.last_call.started_at)} · ${data.last_call.input_tokens ?? "unknown"} input tokens · $${(data.last_call.actual_usd ?? 0).toFixed(6)}` : "No model batch yet."}</div></section>
      <section className="pnl"><div className="ph"><span className="code">DEC</span>Decisions</div><div className="alpha-scroll"><table><thead><tr><th>TIME</th><th>SYMBOL</th><th>DREX TARGET</th><th>ACTION</th><th>REASON</th></tr></thead><tbody>{data.decisions.map((d: any) => <tr key={d.id}><td>{stamp(d.ts)}</td><td>{d.symbol}</td><td>{d.decision_json.model_action}</td><td>{d.action.toUpperCase()}</td><td>{d.decision_json.reason}</td></tr>)}</tbody></table>{!data.decisions.length && <div className="empty">No decisions yet.</div>}</div></section>
      <details className="alpha-notes"><summary>Packet and limits</summary><p>30 × 1-minute, 12 × 5-minute, 8 × 15-minute closed candles. EMA 9/21, RSI 7/14, ATR 14, 60-minute VWAP, Bollinger %B and width, MACD histogram, relative volume, returns, realized volatility and hour-range position.</p><p>US session: TSLA, AAPL, GOOGL, MSFT, NVDA, AMD and INTC. Closed session: BTC, ETH and SOL, long-only spot. Shorts require available borrow. IEX data covers one exchange.</p><p>Each new position targets 5% of current account equity, rounded down to the permitted quantity. Buying power and portfolio risk limits still apply. Drex calls slow to fit $2/month; local exits continue separately.</p></details>
    </>}
  </main>;
}
