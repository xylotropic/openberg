"use client";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { cssVar, fx } from "@/lib/format";
import { getState } from "@/lib/store";

type Point = { symbol: string; expiry: string; dte: number; strike: number; iv: number | null; mid: number | null; delta: number | null; gamma: number | null; theta: number | null; vega: number | null; quote_ts: number };
type Surface = { underlying: string; kind: string; spot: number; spot_ts: number; points: Point[]; expiries: string[]; retrieved_at: number; truncated: boolean };
type Metric = "iv" | "mid" | "delta" | "gamma" | "theta" | "vega";
const labels: Record<Metric, string> = { iv: "IMPLIED VOLATILITY %", mid: "QUOTE MID $", delta: "DELTA", gamma: "GAMMA", theta: "THETA", vega: "VEGA" };
const value = (p: Point, metric: Metric) => p[metric] == null ? null : metric === "iv" ? p.iv! * 100 : p[metric];
const colors = [[0, "#193d66"], [.3, "#4da3ff"], [.55, "#00d084"], [.8, "#ff9b00"], [1, "#ff4d4f"]];

function Plot({ traces, layout, label }: { traces: any[]; layout: any; label: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let disposed = false, plot: typeof import("plotly.js-dist-min").default | null = null;
    const node = host.current!;
    const observer = new ResizeObserver(() => { if (plot && node.clientWidth) void plot.Plots.resize(node).catch(() => {}); });
    observer.observe(node);
    void import("plotly.js-dist-min").then(async module => {
      if (disposed) return;
      plot = module.default;
      const font = { family: cssVar("--mono"), color: "#a7a7a7", size: 11 };
      await plot.react(node, traces, { paper_bgcolor: "#000", plot_bgcolor: "#000", font, margin: { l: 55, r: 35, t: 20, b: 50 }, ...layout },
        { responsive: true, displaylogo: false, scrollZoom: true, modeBarButtonsToRemove: ["sendDataToCloud"] });
      if (disposed) plot.purge(node);
    }).catch(e => { if (!disposed) setError(e.message); });
    return () => { disposed = true; observer.disconnect(); plot?.purge(node); };
  }, [traces, layout]);
  return <>{error && <div className="alert crit" role="alert">{error}</div>}<div ref={host} className="surface-plot" role="img" aria-label={label} /></>;
}

export function SurfacesView() {
  const selected = getState().symbol;
  const [underlying, setUnderlying] = useState(getState().surfaceUnderlying || (selected && !selected.includes("/") ? selected : "SPY"));
  const [kind, setKind] = useState("call");
  const [days, setDays] = useState("90");
  const [band, setBand] = useState(".25");
  const [metric, setMetric] = useState<Metric>("iv");
  const [mode, setMode] = useState("surface");
  const [data, setData] = useState<Surface | null>(null);
  const [expiry, setExpiry] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const load = async () => {
    const version = ++generation.current;
    setBusy(true); setError(""); setData(null);
    try {
      const d = await api<Surface>(`/api/options/surface?underlying=${encodeURIComponent(underlying)}&type=${kind}&days=${days}&band=${band}`);
      if (version === generation.current) { setData(d); setExpiry(d.expiries[0] || ""); }
    } catch (e: any) { if (version === generation.current) setError(e.message); }
    finally { if (version === generation.current) setBusy(false); }
  };
  useEffect(() => { void load(); return () => { generation.current++; }; }, []);
  const points = data?.points || [];
  const strikes = [...new Set(points.map(p => p.strike))].sort((a, b) => a - b);
  const expiries = data?.expiries || [];
  const grid = new Map(points.map(p => [`${p.expiry}:${p.strike}`, p]));
  const z = expiries.map(e => strikes.map(s => { const p = grid.get(`${e}:${s}`); return p ? value(p, metric) : null; }));
  const custom = expiries.map(e => strikes.map(s => grid.get(`${e}:${s}`)?.symbol || ""));
  const dtes = expiries.map(e => points.find(p => p.expiry === e)!.dte);
  const valid = points.filter(p => value(p, metric) != null);
  const latestQuote = Math.max(0, ...points.map(p => p.quote_ts));
  const hover = `<b>%{customdata}</b><br>Strike $%{x}<br>DTE %{y}<br>${labels[metric]} %{z:.4f}<extra></extra>`;
  const traces = mode === "points" ? [{ type: "scatter3d", mode: "markers", x: valid.map(p => p.strike), y: valid.map(p => p.dte), z: valid.map(p => value(p, metric)), customdata: valid.map(p => p.symbol), marker: { size: 3, color: valid.map(p => value(p, metric)), colorscale: colors, showscale: true, colorbar: { title: { text: labels[metric] } } }, hovertemplate: hover }]
    : [{ type: "surface", x: strikes, y: dtes, z, customdata: custom, colorscale: colors, connectgaps: false, colorbar: { title: { text: labels[metric] } }, hovertemplate: hover, contours: { z: { show: true, usecolormap: true, project: { z: true } } } }];
  const axis = (title: string) => ({ title: { text: title }, gridcolor: "#272727", zerolinecolor: "#444", backgroundcolor: "#080808", showbackground: true });
  const surfaceLayout = { height: 560, uirevision: `${data?.underlying}:${data?.kind}`, scene: { xaxis: axis("STRIKE $"), yaxis: axis("DAYS TO EXPIRY"), zaxis: axis(labels[metric]), camera: { eye: { x: 1.6, y: -1.8, z: 1.1 } }, aspectmode: "auto" }, margin: { l: 10, r: 75, t: 15, b: 10 } };
  const smile = points.filter(p => p.expiry === expiry && p.iv != null);
  const term = expiries.map(e => points.filter(p => p.expiry === e && p.iv != null).sort((a, b) => Math.abs(a.strike - data!.spot) - Math.abs(b.strike - data!.spot))[0]).filter(Boolean);
  const twoD = { height: 300, xaxis: { ...axis("STRIKE $"), showbackground: false }, yaxis: { ...axis("IV %"), showbackground: false }, showlegend: false };
  return <main className="view on research-view surfaces-view">
    <div className="toolbar"><b>Option surfaces</b><span className="tag flat">INDICATIVE</span>
      <input aria-label="Surface underlying" value={underlying} onChange={e => setUnderlying(e.target.value.toUpperCase())} />
      <select aria-label="Surface option type" value={kind} onChange={e => setKind(e.target.value)}><option value="call">CALLS</option><option value="put">PUTS</option></select>
      <select aria-label="Surface expiry range" value={days} onChange={e => setDays(e.target.value)}><option value="30">30 DAYS</option><option value="90">90 DAYS</option><option value="180">180 DAYS</option><option value="365">365 DAYS</option></select>
      <select aria-label="Surface strike range" value={band} onChange={e => setBand(e.target.value)}><option value=".1">STRIKES ±10%</option><option value=".25">STRIKES ±25%</option><option value=".5">STRIKES ±50%</option></select>
      <button className="kbtn amber" disabled={busy} onClick={() => void load()}>{busy ? "LOADING…" : "LOAD SURFACE"}</button>
      <select aria-label="Surface metric" value={metric} onChange={e => setMetric(e.target.value as Metric)}>{Object.entries(labels).map(([m, label]) => <option key={m} value={m}>{label}</option>)}</select>
      <span className="seg"><button className={mode === "surface" ? "on" : ""} onClick={() => setMode("surface")}>SURFACE</button><button className={mode === "points" ? "on" : ""} onClick={() => setMode("points")}>POINTS</button></span>
    </div>
    {error && <div className="alert crit" role="alert">{error}</div>}
    {data && <>
      <section className="pnl"><div className="ph"><span className="code">3D</span>{data.underlying} · {data.kind.toUpperCase()} · {labels[metric]}<span className="ph-r">SPOT ${fx(data.spot)} · {valid.length} POINTS · {expiries.length} EXPIRIES{data.truncated ? " · PARTIAL CHAIN" : ""}</span></div>
        <div className="surface-stamp" title={`Loaded ${new Date(data.retrieved_at * 1000).toLocaleString()}`}>INDICATIVE · LATEST QUOTE {latestQuote ? new Date(latestQuote * 1000).toLocaleString() : "UNAVAILABLE"} · DRAG TO ROTATE · SCROLL TO ZOOM</div>
        {valid.length ? <Plot traces={traces} layout={surfaceLayout} label={`${data.underlying} option ${labels[metric]} surface across strike and expiry`} /> : <div className="empty">No {labels[metric].toLowerCase()} values in this chain.</div>}
      </section>
      <div className="surface-secondary">
        <section className="pnl"><div className="ph"><span className="code">SMILE</span>IV by strike<span className="ph-r"><select aria-label="Smile expiration" value={expiry} onChange={e => setExpiry(e.target.value)}>{expiries.map(e => <option key={e}>{e}</option>)}</select></span></div>
          <Plot traces={[{ type: "scatter", mode: "lines+markers", x: smile.map(p => p.strike), y: smile.map(p => p.iv! * 100), customdata: smile.map(p => p.symbol), line: { color: "#ff9b00", width: 2 }, marker: { size: 4 }, hovertemplate: "%{customdata}<br>Strike $%{x}<br>IV %{y:.2f}%<extra></extra>" }]} layout={twoD} label={`${data.underlying} implied volatility smile`} />
        </section>
        <section className="pnl"><div className="ph"><span className="code">TERM</span>Near-spot IV · nearest strike per expiry</div>
          <Plot traces={[{ type: "scatter", mode: "lines+markers", x: term.map(p => p.dte), y: term.map(p => p.iv! * 100), customdata: term.map(p => [p.expiry, p.strike]), line: { color: "#4da3ff", width: 2 }, hovertemplate: "%{customdata[0]}<br>Strike $%{customdata[1]}<br>DTE %{x}<br>IV %{y:.2f}%<extra></extra>" }]} layout={{ ...twoD, xaxis: axis("DAYS TO EXPIRY") }} label={`${data.underlying} implied volatility term structure`} />
        </section>
      </div>
    </>}
    {busy && <div className="empty">Loading option snapshots…</div>}
  </main>;
}
