export type PlotMarker = {
  time: number; position: "aboveBar" | "belowBar"; color: string;
  shape: "arrowUp" | "arrowDown" | "circle"; size: number; text: string;
};

export function fillLoader(load: (signal: AbortSignal) => Promise<any[]>, publish: (rows: any[]) => void) {
  let active = true, inFlight = false, controller: AbortController | null = null;
  return {
    async refresh() {
      if (!active || inFlight) return;
      inFlight = true; controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 15000);
      try {
        const rows = await load(controller.signal);
        if (active && Array.isArray(rows)) publish(rows);
      } catch { /* Retain broker-confirmed fills through reconnects. */ }
      finally { clearTimeout(timeout); inFlight = false; }
    },
    stop() { active = false; controller?.abort(); },
  };
}

// Signals share a candle; confirmed fills remain separate and cannot be deduplicated away.
export function chartMarkers(decisions: any[], fills: any[], symbol: string, tf: string,
  times: Set<number>, up: string, down: string): PlotMarker[] {
  const seconds = ({ D: 86400, W: 604800 } as Record<string, number>)[tf] || Number(tf) * 60;
  if (!(seconds > 0)) return [];
  const candleTimes = Array.from(times).sort((a,b) => a-b);
  const bucket = (at: number) => {
    let lo=0, hi=candleTimes.length;
    while (lo<hi) { const mid=(lo+hi)>>>1; if (candleTimes[mid]<=at) lo=mid+1; else hi=mid; }
    const time=candleTimes[lo-1];
    return time!==undefined && at<time+seconds ? time : NaN;
  };
  const signals = new Map<number, { at: number; priority: number; marker: PlotMarker }>();
  for (const d of decisions) {
    if (d.symbol !== symbol || d.error_kind || d.probe || !Number.isFinite(d.ts)) continue;
    const action = String(d.action || "").toLowerCase();
    if (!["long", "short", "exit", "hold", "flat", "pending"].includes(action)) continue;
    const time = bucket(d.ts);
    if (!times.has(time)) continue;
    const priority = ["long", "short", "exit"].includes(action) ? 2 : 0;
    const previous = signals.get(time);
    if (previous && (previous.priority > priority || previous.priority === priority && previous.at > d.ts)) continue;
    signals.set(time, { at: d.ts, priority, marker: {
      time, position: action === "short" ? "aboveBar" : "belowBar",
      color: action === "long" ? up : action === "short" ? down : "#6a6a6a", shape: "circle",
      size: priority ? 1 : .3, text: priority ? `${action.toUpperCase()} SIGNAL` : "",
    } });
  }
  const events: PlotMarker[] = [];
  const seen = new Set<string>();
  for (const f of fills) {
    if (f.symbol !== symbol || !f.id || seen.has(f.id) || !Number.isFinite(f.at) || !(f.qty > 0) || !(f.price > 0)) continue;
    const time = bucket(f.at);
    if (!times.has(time)) continue;
    seen.add(f.id);
    const long = f.position_side === "long", entry = f.effect === "entry";
    events.push({ time, position: entry && !long || !entry && long ? "aboveBar" : "belowBar",
      color: entry ? long ? up : down : "#43b5ff", shape: entry ? long ? "arrowUp" : "arrowDown" : "circle",
      size: 1.8, text: entry ? `${long ? "LONG" : "SHORT"} FILL` : "EXIT FILL" });
  }
  return [...Array.from(signals.values(), s => s.marker), ...events].sort((a,b) => a.time-b.time);
}
