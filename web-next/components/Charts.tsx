"use client";
import { useEffect, useRef, useState } from "react";
import {
  createChart, LineStyle, type IChartApi, type IPriceLine, type ISeriesApi, type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { api as requestApi } from "@/lib/api";
import { getState, setBarSink, useStore, type Bar } from "@/lib/store";
import { cssVar, decimalsOf, fx, ud, usd } from "@/lib/format";
import { chartMarkers, fillLoader, longEntryPrice } from "@/lib/chart-markers";

type Candle = { time: number; open: number; high: number; low: number; close: number; volume: number };

function ema(vals: number[], p: number) {
  const k = 2 / (p + 1);
  const out: (number | null)[] = [];
  let e: number | null = null;
  vals.forEach((v, i) => {
    e = e === null ? v : v * k + e * (1 - k);
    out.push(i >= p - 1 ? e : null);
  });
  return out;
}

const baseOpts = () => ({
  autoSize: true,
  layout: { background: { color: "#000" }, textColor: cssVar("--muted"), fontFamily: cssVar("--mono"), fontSize: 11 },
  grid: { vertLines: { color: "#111" }, horzLines: { color: "#111" } },
  timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#242424", rightOffset: 4 },
  rightPriceScale: { borderColor: "#242424" },
  crosshair: { mode: 0, vertLine: { color: "#5a5a5a", labelBackgroundColor: "#2b1a05" }, horzLine: { color: "#5a5a5a", labelBackgroundColor: "#2b1a05" } },
  localization: { timeFormatter: (t: number) => new Date(t * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) },
});

export function PriceChart() {
  const host = useRef<HTMLDivElement>(null);
  const api = useRef<{ chart: IChartApi; candles: ISeriesApi<"Candlestick">; e20: ISeriesApi<"Line">; e50: ISeriesApi<"Line">; vol: ISeriesApi<"Histogram"> } | null>(null);
  const bars = useRef<Candle[]>([]);
  const byTime = useRef(new Map<number, Candle>());
  const key = useRef("");
  const [ohlc, setOhlc] = useState<Candle | null>(null);
  const [pd, setPd] = useState(2);
  const mkt = useStore((s) => s.mkt);
  const marketError = useStore((s) => s.marketError);
  const symbol = useStore((s) => s.symbol);
  const tf = useStore((s) => s.tf);
  const decisions = useStore((s) => s.decisions);
  const positions = useStore((s) => s.ov?.account?.positions);
  const entryLine = useRef<IPriceLine | null>(null);
  const [fillState, setFills] = useState<{symbol:string;rows:any[]}>({symbol:"",rows:[]});
  const tradeRev = useStore((s) => s.rev.trades);
  useEffect(() => {
    if (!symbol) return;
    const loader = fillLoader(signal => requestApi<any[]>(`/api/chart-fills?symbol=${encodeURIComponent(symbol)}`, {signal}),
      rows => setFills({symbol,rows}));
    void loader.refresh();
    const timer = setInterval(() => { if (!document.hidden) void loader.refresh(); }, 5000);
    return () => { loader.stop(); clearInterval(timer); };
  }, [symbol, tradeRev]);

  useEffect(() => {
    const chart = createChart(host.current!, baseOpts() as any);
    const up = cssVar("--up"), dn = cssVar("--down");
    const candles = chart.addCandlestickSeries({ upColor: up, downColor: dn, borderVisible: false, wickUpColor: up, wickDownColor: dn, priceLineColor: cssVar("--amber") });
    const lineOpts = { lineWidth: 1 as const, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    const e20 = chart.addLineSeries({ color: cssVar("--amber"), ...lineOpts });
    const e50 = chart.addLineSeries({ color: cssVar("--s1"), ...lineOpts });
    const vol = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    chart.subscribeCrosshairMove((p) => {
      const b = p?.time ? byTime.current.get(p.time as number) : null;
      setOhlc(b || bars.current[bars.current.length - 1] || null);
    });
    api.current = { chart, candles, e20, e50, vol };
    return () => {
      setBarSink(null);
      chart.remove();
      api.current = null;
      entryLine.current = null;
    };
  }, []);

  const drawEmas = () => {
    const a = api.current!, b = bars.current, closes = b.map((x) => x.close);
    const f = (vals: (number | null)[]) => b.map((x, i) => (vals[i] === null ? { time: x.time as UTCTimestamp } : { time: x.time as UTCTimestamp, value: vals[i] as number }));
    a.e20.setData(f(ema(closes, 20)) as any);
    a.e50.setData(f(ema(closes, 50)) as any);
  };
  const volPoint = (x: Candle) => ({ time: x.time as UTCTimestamp, value: x.volume, color: x.close >= x.open ? "rgba(58,208,138,.35)" : "rgba(255,92,92,.35)" });

  // full reload when the symbol/timeframe (or its REST snapshot) changes
  useEffect(() => {
    const a = api.current;
    if (!a) return;
    if (!mkt?.bars?.length) {
      setBarSink(null);
      key.current = "";
      bars.current = [];
      byTime.current.clear();
      a.candles.setMarkers([]);
      [a.candles, a.e20, a.e50, a.vol].forEach(series => series.setData([]));
      setOhlc(null);
      return;
    }
    const k = `${mkt.symbol}:${mkt.tf}`;
    const reset = k !== key.current;
    key.current = k;
    const d = Math.max(decimalsOf(mkt.ticker.lastPrice), 2);
    setPd(d);
    const pf = { type: "price" as const, precision: d, minMove: Math.pow(10, -d) };
    a.candles.applyOptions({ priceFormat: pf });
    a.e20.applyOptions({ priceFormat: pf });
    a.e50.applyOptions({ priceFormat: pf });
    // keep any newer live bar the socket already delivered
    const live = !reset ? bars.current[bars.current.length - 1] : undefined;
    let b: Candle[] = mkt.bars;
    if (live && b.length && live.time >= b[b.length - 1].time) b = [...b.filter((x) => x.time < live.time), live];
    bars.current = b;
    byTime.current = new Map(b.map((x) => [x.time, x]));
    a.candles.setData(b.map((x) => ({ time: x.time as UTCTimestamp, open: x.open, high: x.high, low: x.low, close: x.close })));
    a.vol.setData(b.map(volPoint));
    drawEmas();
    if (reset) {
      a.chart.priceScale("right").applyOptions({ autoScale: true });
      a.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, b.length - 140), to: b.length + 4 });
    }
    setOhlc(b[b.length - 1]);

    setBarSink((bar: Bar) => {
      if (bar.symbol !== mkt.symbol || bar.tf !== mkt.tf || !api.current) return;
      if (bar.symbol !== getState().symbol || bar.tf !== getState().tf) return;
      const arr = bars.current, last = arr[arr.length - 1];
      if (last && bar.time < last.time) return;
      const c: Candle = { time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: bar.volume };
      const isNew = !last || bar.time > last.time;
      if (isNew) arr.push(c);
      else arr[arr.length - 1] = c;
      byTime.current.set(c.time, c);
      api.current.candles.update({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close });
      api.current.vol.update(volPoint(c));
      if (isNew) drawEmas();
      setOhlc((o) => (!o || o.time === c.time || o.time === last?.time ? c : o));
    });
  }, [mkt]);

  // Plot decisions at their actual as-of time and broker fills at filled_at.
  useEffect(() => {
    const a = api.current;
    if (!a || !mkt?.bars?.length) return;
    const up = cssVar("--up"), dn = cssVar("--down");
    a.candles.setMarkers(chartMarkers(decisions, fillState.symbol === mkt.symbol ? fillState.rows : [], mkt.symbol, String(mkt.tf), new Set(byTime.current.keys()), up, dn) as SeriesMarker<Time>[]);
  }, [decisions, fillState, mkt]);

  useEffect(() => {
    const a = api.current;
    if (!a) return;
    const price = symbol && mkt?.symbol === symbol && mkt?.bars?.length ? longEntryPrice(positions, symbol) : null;
    if (price === null) {
      if (entryLine.current) a.candles.removePriceLine(entryLine.current);
      entryLine.current = null;
      return;
    }
    const options = { price, color: cssVar("--up"), lineWidth: 1 as const, lineStyle: LineStyle.Solid,
      axisLabelVisible: true, title: "LONG ENTRY" };
    if (entryLine.current) entryLine.current.applyOptions(options);
    else entryLine.current = a.candles.createPriceLine(options);
  }, [positions, symbol, mkt]);

  const chg = ohlc ? ohlc.close - ohlc.open : 0;
  return (
    <div className="pb chart-wrap" data-symbol={mkt?.symbol} data-timeframe={mkt?.tf}>
      <div ref={host} className="chart-host" role="img" aria-label={`${symbol} ${tf} minute candlestick chart`} />
      {(marketError || !mkt?.bars?.length) && <div className={`chart-message${marketError ? " error" : ""}`} role="status">{marketError || (mkt ? `No bars for ${symbol}` : `Loading ${symbol}…`)}</div>}
      {ohlc && (
        <div className="ohlc">
          <b>O</b> {fx(ohlc.open, pd)} <b>H</b> {fx(ohlc.high, pd)} <b>L</b> {fx(ohlc.low, pd)} <b>C</b> <span className={ud(chg)}>{fx(ohlc.close, pd)}</span> <b>V</b> {usd(ohlc.volume)}{" "}
          <span className="muted">{new Date(ohlc.time * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</span>
        </div>
      )}
    </div>
  );
}

export function EquityChart() {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Area"> | null>(null);
  const [period, setPeriod] = useState("1D");
  const [data, setData] = useState<any>(null), [error, setError] = useState("");
  useEffect(() => {
    const c = createChart(host.current!, baseOpts() as any);
    chart.current = c;
    series.current = c.addAreaSeries({ lineColor: cssVar("--amber"), topColor: "rgba(255,155,0,.22)", bottomColor: "rgba(255,155,0,0)", lineWidth: 2,
      priceLineVisible: false, priceFormat: { type: "custom", minMove: .01, formatter: (v: number) => `$${fx(v, 2)}` } });
    return () => { c.remove(); chart.current = null; series.current = null; };
  }, []);
  useEffect(() => {
    let active = true;
    setData(null); setError("");
    const load = async () => {
      try { const response = await requestApi(`/api/equity-curve?period=${period}`); if (active) { setData(response); setError(""); } }
      catch (e: any) { if (active) setError(e.message); }
    };
    void load(); const timer = setInterval(() => { if (!document.hidden) void load(); }, 30000);
    return () => { active = false; clearInterval(timer); };
  }, [period]);
  const firstDraw = useRef("");
  useEffect(() => {
    if (!series.current || !chart.current) return;
    const points: [number, number][] = data?.period === period ? data.points : [];
    series.current.setData(points.map(([t, value]) => ({ time: t as UTCTimestamp, value })));
    if (points.length && firstDraw.current !== period) { chart.current.timeScale().fitContent(); firstDraw.current = period; }
  }, [data, period]);
  const last = data?.period === period ? data.points?.at(-1)?.[1] : null;
  return <>
    <div className="ph" data-tip={data?.basis}><span className="code">NAV</span>Paper account equity
      <span className="seg">{["1D", "1W", "1M"].map(p => <button key={p} className={p === period ? "on" : ""} onClick={() => setPeriod(p)}>{p}</button>)}</span>
      <span className="ph-r">{last != null ? `$${fx(last, 2)}` : "—"}</span>
    </div>
    <div className="pb chart-wrap"><div ref={host} className="chart-host" role="img" aria-label="Alpaca paper account equity curve in USD" />
      {(error || !data?.points?.length) && <div className={`chart-message${error ? " error" : ""}`} role="status">{error || (data ? "No account history yet" : "Loading account equity…")}</div>}
    </div>
  </>;
}
