"use client";
import { useEffect, useRef, useState } from "react";
import { api, post } from "@/lib/api";
import { loadOverview, selectTf } from "@/lib/data";
import { setState, useStore } from "@/lib/store";
import { age, dt, fx, hrs, sg, short, ud, usd } from "@/lib/format";

export function Dialog({ className, children }: { className?: string; children: React.ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current!;
    d.showModal();
    const onClose = () => setState({ modal: null });
    d.addEventListener("close", onClose);
    return () => d.removeEventListener("close", onClose);
  }, []);
  return (
    <dialog ref={ref} className={className} onClick={(e) => e.target === ref.current && ref.current?.close()}>
      <form method="dialog"><button className="kbtn close" aria-label="Close">ESC ✕</button></form>
      {children}
    </dialog>
  );
}

const J = ({ x }: { x: unknown }) => <pre className="code">{JSON.stringify(x, null, 1) ?? "—"}</pre>;

export function Modals() {
  const modal = useStore((s) => s.modal);
  const alpaca = useStore((s) => s.ov?.settings?.venue === "alpaca");
  if (!modal) return null;
  if (modal.kind === "decision") return <DecisionModal d={modal.data} />;
  if (modal.kind === "state") return <StateModal data={modal.data} />;
  if (modal.kind === "keys") return <KeysModal />;
  if (modal.kind === "help") return <HelpModal />;
  if (modal.kind === "tf") return <TfModal tf={modal.data} />;
  if (modal.kind === "universe") return alpaca ? <AlpacaUniverseModal /> : <UniverseModal />;
  if (modal.kind === "strategy") return <StrategyModal />;
  return null;
}

function DecisionModal({ d }: { d: any }) {
  return (
    <Dialog>
      <div className="ph"><span className="code">DEC</span>Decision #{d.id} · {d.symbol} · {dt(d.ts)}</div>
      <p>question set {d.qset_version} · {fx(d.latency_ms, 0)} ms · {d.request_bytes ?? "—"} bytes · attempts {d.attempts ?? "—"} · request_id {d.request_id || "—"}</p>
      {d.issues_json?.length > 0 && <p className="dn">Contract issues: {d.issues_json.join("; ")}</p>}
      <div className="mgrid">
        <div><h3>State sent</h3><J x={d.state_json} /></div>
        <div><h3>Raw answers</h3><J x={d.answers_json} /><h3>Usage · model metadata</h3><J x={{ usage: d.usage_json, meta: d.model_meta_json }} /></div>
        <div><h3>Policy decision</h3><J x={d.decision_json} /><h3>Realised label</h3><J x={d.label_json || "pending"} /></div>
        <div><h3>Questions</h3><J x={d.questions_json} />{d.probes?.length > 0 && <><h3>Probes</h3><J x={d.probes} /></>}</div>
      </div>
    </Dialog>
  );
}

function StateModal({ data }: { data: any }) {
  const p = data.preview, n = JSON.stringify(p.state).length;
  return (
    <Dialog>
      <div className="ph"><span className="code">STATE</span>Exact state sent to the model · {data.symbol}</div>
      <p>{n} chars ≈ {Math.round(n / 3.6)} tokens. Built {age(Date.now() / 1000 - p.ts)} ago.</p>
      <div><J x={p.state} /></div>
    </Dialog>
  );
}

function HelpModal() {
  const rows: [string, string][] = [
    ["BTC · ETH · SOL …", "Load a symbol into chart, model signal, book and stats"], ["ASK", "Ask the active model now for the loaded symbol (1 request)"],
    ["PRB", "Ask + repeat & mirror consistency probes (4 requests)"], ["1M 5M 15M 1H 4H", "Chart timeframe (view only)"],
    ["POS 10", "Max open positions (1–50); new entries only"], ["TF 1M · TF 5M · TF 15M · TF 1H", "Trading timeframe: how often the model decides and trades"], ["MODEL", "Drex Finance or Kairos: switch in the function-key bar (starts a new run)"],
    ["UNIV", "Choose the symbols the bot watches and trades"], ["STRAT", "Strategy: brackets, entries, mirror averaging, triggers, filters, sizing"],
    ["MON EVAL VALID TRD NEWS LOG", "Switch screen (F1 F2 F6 F3 F4 F5)"], ["KEYS · HELP", "API credentials · this list"], ["/", "Focus the command line from anywhere"],
  ];
  return (
    <Dialog className="keys">
      <div className="ph"><span className="code">HELP</span>Commands</div>
      <table className="t" style={{ marginTop: 6 }}><tbody>{rows.map(([k, v]) => <tr key={k}><td className="amb">{k}</td><td className="wrap">{v}</td></tr>)}</tbody></table>
    </Dialog>
  );
}

function TfModal({ tf }: { tf: string }) {
  const st = useStore((s) => s.ov?.settings);
  if (!st) return null;
  const cur = st.tf_profiles[st.decision_tf], nxt = st.tf_profiles[tf];
  if (!nxt) return null;
  if (tf === st.decision_tf) {
    setTimeout(() => setState({ modal: null, cmdHint: `ALREADY TRADING ${nxt.label}` }), 0);
    return null;
  }
  const budget = st.kairos_max_requests_per_hour, over = nxt.required_rph > budget;
  const tok = (rph: number) => `~${Math.round((rph * 14800) / 1e5) / 10}M`;
  const Row = ({ k, a, b }: { k: string; a: string; b: string }) => <><div className="h">{k}</div><div className="old">{a}</div><div className="new">{b}</div></>;
  const go = async (raise: boolean) => {
    try {
      await post("/api/decision-tf", { tf, raise_budget: raise });
      setState({ modal: null });
      await loadOverview();
      selectTf(tf);
    } catch (e: any) {
      alert(e.message);
    }
  };
  return (
    <Dialog className="keys">
      <div className="ph"><span className="code">TF</span>Switch trading timeframe</div>
      <div style={{ margin: "8px 10px" }}>
        <div className="tfgrid">
          <Row k="" a={"NOW · " + cur.label} b={"NEW · " + nxt.label} />
          <Row k="Model asked" a={`every ${cur.label} close`} b={`every ${nxt.label} close`} />
          <Row k="Max hold / time-stop" a={hrs(cur.horizon_minutes)} b={hrs(nxt.horizon_minutes)} />
          <Row k="Stop sized from" a={`${cur.atr_tf}m ATR × ${st.sl_atr} (≥ ${st.min_sl_bp}bp)`} b={`${nxt.atr_tf}m ATR × ${st.sl_atr} (≥ ${st.min_sl_bp}bp)`} />
          <Row k="Model requests / hour" a={`~${cur.required_rph}`} b={`~${nxt.required_rph}`} />
          <Row k="Input tokens / hour" a={tok(cur.required_rph)} b={tok(nxt.required_rph)} />
        </div>
        <p className="muted" style={{ margin: "8px 0 0" }}>NEXT BAR · EXISTING TIME-STOPS UNCHANGED</p>
        {over && <div className="warnbox"><b className="amb">BUDGET.</b> {nxt.label} needs ~{nxt.required_rph} requests/hour but the cap is {budget}. With the current cap, about {Math.round(100 * (1 - budget / nxt.required_rph))}% of bars would be skipped.</div>}

      </div>
      <div className="keyactions" style={{ margin: "8px 10px" }}>
        {over ? <><button className="kbtn amber" onClick={() => go(true)}>SWITCH + RAISE CAP TO {nxt.required_rph}/H</button><button className="kbtn" onClick={() => go(false)}>SWITCH, KEEP CAP {budget}</button></>
          : <button className="kbtn amber" onClick={() => go(false)}>SWITCH TO {nxt.label}</button>}
        <button className="kbtn" onClick={() => setState({ modal: null })}>CANCEL</button>
      </div>
    </Dialog>
  );
}

type Msg = { ok: boolean; text: string } | null;
function KeysModal() {
  const [k, setK] = useState<any>(null);
  const [kMsg, setKMsg] = useState<Msg>(null);
  const [bMsg, setBMsg] = useState<Msg>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const kairosKey = useRef<HTMLInputElement>(null), kairosUrl = useRef<HTMLInputElement>(null);
  const bybitKey = useRef<HTMLInputElement>(null), bybitSecret = useRef<HTMLInputElement>(null);
  const load = () => api("/api/keys").then((x) => { setK(x); if (x.bybit.error) setBMsg({ ok: false, text: x.bybit.error }); });
  useEffect(() => { load().catch(() => {}); }, []);
  const run = async (id: string, fn: () => Promise<void>) => { setBusy(id); try { await fn(); } finally { setBusy(null); } };
  const kairosResult = (r: any) => setKMsg(r.ok ? { ok: true, text: `Kairos reachable · ${r.latency_ms} ms · model ${r.model || "DMIND/Kairos"}` } : { ok: false, text: r.error });
  const [dMsg, setDMsg] = useState<Msg>(null);
  const drexKey = useRef<HTMLInputElement>(null);
  const drexResult = (r: any) => setDMsg(r.ok ? { ok: true, text: `Drex Finance reachable · ${r.latency_ms} ms · model ${r.model || "drex-max-finance"}` } : { ok: false, text: r.error });
  const Stat = ({ ok, txt }: { ok: boolean; txt: string }) => <span className={ok ? "up" : "dn"}>● {txt}</span>;
  return (
    <Dialog className="keys">
      <div className="ph"><span className="code">KEYS</span>API credentials</div>

      <section className="keybox">
        <div className="keyhead"><b>KAIROS · MINARA CLOUD</b>{k && (k.kairos.set ? <Stat ok txt={`SET ${k.kairos.hint}`} /> : <Stat ok={false} txt="NOT SET" />)}</div>
        <p><a href="https://minara.cloud" target="_blank" rel="noopener noreferrer">CREATE KAIROS KEY ↗</a></p>
        <label>API KEY<input ref={kairosKey} type="password" autoComplete="off" spellCheck={false} placeholder="paste Kairos API key" /></label>
        <label>BASE URL (OPTIONAL)<input ref={kairosUrl} type="text" autoComplete="off" spellCheck={false} placeholder={k?.kairos.base_url || "https://minara.cloud"} /></label>
        <div className="keyactions">
          <button className="kbtn amber" type="button" disabled={!!busy} onClick={() => run("ks", async () => {
            const key = kairosKey.current!.value.trim();
            if (!key) return setKMsg({ ok: false, text: "Paste a key first" });
            try { kairosResult(await post("/api/keys/kairos", { api_key: key, base_url: kairosUrl.current!.value.trim() || null })); kairosKey.current!.value = ""; } catch (e: any) { setKMsg({ ok: false, text: e.message }); }
            load(); loadOverview();
          })}>{busy === "ks" ? "WORKING…" : "SAVE & TEST"}</button>
          <button className="kbtn" type="button" disabled={!!busy} onClick={() => run("kt", async () => { try { kairosResult(await post("/api/keys/kairos/test")); } catch (e: any) { setKMsg({ ok: false, text: e.message }); } })}>{busy === "kt" ? "WORKING…" : "TEST CURRENT KEY"}</button>
          <span className="muted">1 REQUEST</span>
        </div>
        <div className={`keymsg ${kMsg ? (kMsg.ok ? "ok" : "bad") : ""}`}>{kMsg?.text}</div>
      </section>
      <section className="keybox">
        <div className="keyhead"><b>DREX FINANCE · NACE.AI</b>{k && (k.drex?.set ? <Stat ok txt={`SET ${k.drex.hint} · ${k.drex.model_id}`} /> : <Stat ok={false} txt="NOT SET" />)}</div>
        <p><a href="https://console.nace.ai/dashboard/api-keys" target="_blank" rel="noopener noreferrer">CREATE NACE KEY ↗</a></p>
        <label>API KEY<input ref={drexKey} type="password" autoComplete="off" spellCheck={false} placeholder="paste Nace API key" /></label>
        <div className="keyactions">
          <button className="kbtn amber" type="button" disabled={!!busy} onClick={() => run("ds", async () => {
            const key = drexKey.current!.value.trim();
            if (!key) return setDMsg({ ok: false, text: "Paste a key first" });
            try { drexResult(await post("/api/keys/drex", { api_key: key })); drexKey.current!.value = ""; } catch (e: any) { setDMsg({ ok: false, text: e.message }); }
            load(); loadOverview();
          })}>{busy === "ds" ? "WORKING…" : "SAVE & TEST"}</button>
          <button className="kbtn" type="button" disabled={!!busy} onClick={() => run("dt", async () => { try { drexResult(await post("/api/keys/drex/test")); } catch (e: any) { setDMsg({ ok: false, text: e.message }); } })}>{busy === "dt" ? "WORKING…" : "TEST CURRENT KEY"}</button>
          <span className="muted">1 REQUEST</span>
        </div>
        <div className={`keymsg ${dMsg ? (dMsg.ok ? "ok" : "bad") : ""}`}>{dMsg?.text}</div>
      </section>
      <section className="keybox">
        <div className="keyhead"><b>ALPACA · PAPER</b>{k && (k.alpaca.api_key.set ? <Stat ok={k.alpaca.connected} txt={`KEY ${k.alpaca.api_key.hint} · ${k.alpaca.connected ? "CONNECTED" : "ERROR"}`} /> : <Stat ok={false} txt="NOT SET" />)}</div>
        <p><a href="https://app.alpaca.markets/dashboard/overview" target="_blank" rel="noopener noreferrer">ALPACA PAPER ACCOUNT ↗</a></p>
        <label>API KEY<input ref={bybitKey} type="password" autoComplete="off" spellCheck={false} placeholder="Alpaca paper API key" /></label>
        <label>API SECRET<input ref={bybitSecret} type="password" autoComplete="off" spellCheck={false} placeholder="Alpaca paper API secret" /></label>
        <div className="keyactions">
          <button className="kbtn amber" type="button" disabled={!!busy} onClick={() => run("bs", async () => {
            const key = bybitKey.current!.value.trim(), secret = bybitSecret.current!.value.trim();
            if (!key || !secret) return setBMsg({ ok: false, text: "Both paper key and secret are required" });
            try { const r = await post("/api/keys/alpaca", { api_key: key, api_secret: secret }); bybitKey.current!.value = bybitSecret.current!.value = ""; setBMsg({ ok: true, text: `Saved · paper equity $${fx(r.equity)}` }); } catch (e: any) { setBMsg({ ok: false, text: e.message }); }
            load(); loadOverview();
          })}>{busy === "bs" ? "WORKING…" : "VERIFY & SAVE"}</button>
          <span className="muted">PAPER ONLY · AUTO-TRADE OFF</span>
        </div>
        <div className={`keymsg ${bMsg ? (bMsg.ok ? "ok" : "bad") : ""}`}>{bMsg?.text}</div>
      </section>
    </Dialog>
  );
}

/* ---------------------------------------------------------------- symbol universe */
function UniverseModal() {
  const [u, setU] = useState<any>(null);
  const [sel, setSel] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [restarting, setRestarting] = useState(false);
  useEffect(() => {
    api("/api/universe").then((x) => { setU(x); setSel(x.selected); }).catch((e) => setErr(e.message));
  }, []);
  if (restarting) return <div className="restarting">RESTARTING WITH {sel.length} SYMBOLS · reconnecting…</div>;
  const toggle = (sym: string) => setSel((cur) => (cur.includes(sym) ? cur.filter((x) => x !== sym) : cur.length >= (u?.max_symbols ?? 30) ? cur : [...cur, sym]));
  const need = u ? Math.round(sel.length * u.rph_per_symbol) : 0;
  const over = u && need > u.budget_per_hour;
  const added: string[] = u ? sel.filter((x) => !u.selected.includes(x)) : [];
  const dropped: string[] = u ? u.selected.filter((x: string) => !sel.includes(x)) : [];
  const changed = added.length + dropped.length > 0;
  const typed = q.trim().toUpperCase();
  const typedSym = typed && !typed.endsWith("USDT") ? typed + "USDT" : typed;
  const rows = (u?.candidates || []).filter((c: any) => !typed || c.symbol.includes(typed));
  const apply = async (raise: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      await post("/api/symbols", { symbols: sel, raise_budget: raise });
      setRestarting(true);
      // the service re-executes itself; wait for it to come back, then reload so every panel rebinds
      const t0 = Date.now();
      const wait = async () => {
        try {
          await api("/api/overview");
          location.reload();
        } catch {
          if (Date.now() - t0 < 90_000) setTimeout(wait, 1500);
          else setErr("service did not come back within 90 s");
        }
      };
      setTimeout(wait, 4000);
    } catch (e: any) {
      setErr(e.message);
      setBusy(false);
    }
  };
  return (
    <Dialog>
      <div className="ph"><span className="code">UNIV</span>Symbol universe · signals Bybit mainnet · orders Bybit testnet</div>
      <p>Pick the perps the bot watches and trades. Each needs ~{u ? fx(u.rph_per_symbol, 1) : "—"} model requests/hour at the current {u?.decision_tf}m trading timeframe (one per bar close, plus consistency probes). Only perps listed on both mainnet and testnet can be chosen. Entries take only testnet liquidity within {u?.max_entry_slip_bp}bp of the best price, so a thin testnet book means small or unfilled orders.</p>
      <div className="unibar">
        <input type="text" placeholder="filter or type a symbol" value={q} onChange={(e) => setQ(e.target.value)} />
        <span>SELECTED <b className="amb">{sel.length}</b>/{u?.max_symbols ?? 30}</span>
        <span className={over ? "dn" : ""}>KAIROS <b>~{need}</b> / {u?.budget_per_hour ?? "—"} req/h</span>
        {changed && <span className="muted">+{added.map(short).join(" +") || "0"} · −{dropped.map(short).join(" −") || "0"}</span>}
        <span style={{ marginLeft: "auto" }} />
        <button className="kbtn" onClick={() => u && setSel(u.selected)}>RESET</button>
      </div>
      {typedSym && u && !u.candidates.some((c: any) => c.symbol === typedSym) && (
        <div className="unibar">
          <span className="muted">{typedSym} is not in the top {u.candidates.length} by turnover.</span>
          <button className="kbtn" onClick={() => !sel.includes(typedSym) && setSel([...sel, typedSym])}>ADD {typedSym} (validated on apply)</button>
        </div>
      )}
      {!u ? <div className="empty">{err || "Loading the universe and testnet books…"}</div> : (
        <div className="unigrid">
          <table className="t">
            <thead><tr><th /><th>Symbol</th><th className="r">24h turnover $</th><th className="r">24h</th><th>Testnet</th><th className="r">Testnet bid / ask ≤20bp $</th><th className="r">Spread bp</th><th>Note</th></tr></thead>
            <tbody>
              {rows.map((c: any) => {
                const on = sel.includes(c.symbol), b = c.testnet_book;
                const can = c.mainnet && c.testnet;
                return (
                  <tr key={c.symbol} className={`click ${on ? "on sel" : ""}`} onClick={() => can && toggle(c.symbol)} style={can ? undefined : { opacity: 0.45, cursor: "not-allowed" }}>
                    <td><input type="checkbox" readOnly checked={on} disabled={!can} /></td>
                    <td className="sym">{short(c.symbol)}</td>
                    <td className="r">{usd(c.turnover24h)}</td>
                    <td className={`r ${ud(c.pct24h)}`}>{sg(c.pct24h, 2, "%")}</td>
                    <td>{c.testnet ? <span className="tag long">LISTED</span> : <span className="tag err">NOT LISTED</span>}</td>
                    <td className="r">{b?.ok ? `${usd(b.bid_usd_20bp)} / ${usd(b.ask_usd_20bp)}` : b ? <span className="muted">{b.reason}</span> : "—"}</td>
                    <td className="r">{b?.ok ? fx(b.spread_bp, 2) : "—"}</td>
                    <td>{c.asset_type !== "crypto" && <span className="tag amb" data-tip={`Bybit lists this as a ${c.asset_type} perp: it tracks a market with trading hours, so nights and weekends behave unlike crypto and the base rates mix open and closed sessions.`}>{String(c.asset_type).toUpperCase()}</span>} {b?.ok && b.thin ? <span className="tag part" data-tip={`Less than one max-size order ($${usd(u.thin_depth_usd)}) rests within 20bp on one side: entries will be partly filled`}>THIN BOOK</span> : ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {over && <div className="warnbox" style={{ margin: "6px 10px" }}><b className="amb">BUDGET.</b> {sel.length} symbols need ~{need} model requests/hour, above the cap of {u.budget_per_hour}. Remove symbols, switch to a slower trading timeframe, or raise the cap.</div>}
      {err && <div className="keymsg bad" style={{ margin: "6px 10px" }}>{err}</div>}
      <div className="keyactions" style={{ margin: "8px 10px" }}>
        {over
          ? <button className="kbtn amber" disabled={busy || !changed} onClick={() => apply(true)}>APPLY + RAISE CAP TO {need}/H</button>
          : <button className="kbtn amber" disabled={busy || !changed || !sel.length} onClick={() => apply(false)}>APPLY {sel.length} SYMBOLS</button>}
        <button className="kbtn" onClick={() => setState({ modal: null })}>CANCEL</button>
        <span className="muted">Applying saves to .env and restarts the service (~15 s). Open positions keep being managed; a symbol with an open bot trade cannot be removed. Auto-trade stays as it is.</span>
      </div>
    </Dialog>
  );
}

/* ---------------------------------------------------------------- strategy */
type Field = { k: string; label: string; help: string; kind: "choice" | "bool" | "num" | "list"; choices?: string[]; step?: number };
const FIELDS: Field[] = [
  { k: "bracket_mode", label: "Bracket", kind: "choice", choices: ["vol", "atr"], help: "vol: stop = SL σ × GARCH volatility over the horizon, never so tight that costs exceed MAX COST of it. atr: the original 1.5 × ATR stop with a 40bp floor." },
  { k: "sl_sigma", label: "Stop (σ over horizon)", kind: "num", step: 0.1, help: "Stop distance in standard deviations of the forecast move over the holding time." },
  { k: "reward_risk", label: "Reward : risk", kind: "num", step: 0.1, help: "Target = stop × this." },
  { k: "max_cost_r", label: "Max cost (share of stop)", kind: "num", step: 0.05, help: "Expected execution cost may be at most this share of the stop. 0.2 = fees and slippage use ≤ 20% of 1R." },
  { k: "auto_horizon", label: "Extend horizon", kind: "bool", help: "When the cost floor makes the stop wider than the volatility stop, hold longer so the target stays reachable." },
  { k: "entry_mode", label: "Entries", kind: "choice", choices: ["maker", "taker"], help: "maker: post-only limit at the best bid/ask (maker fee, no slippage), cancelled after the wait. taker: capped IOC." },
  { k: "maker_wait_s", label: "Maker wait (s)", kind: "num", step: 5, help: "How long a maker entry rests before it is cancelled." },
  { k: "symmetrize", label: "Mirror averaging", kind: "bool", help: "Ask every decision on the real and the mirrored market and average: cancels any built-in long/short bias. Doubles requests." },
  { k: "trade_trigger", label: "Trade trigger", kind: "choice", choices: ["events", "every_bar"], help: "events: trade only on bars with a market event (volume spike, range break, VWAP cross, flow burst, liquidation burst…). Decisions are still scored every bar." },
  { k: "direction_source", label: "Direction from", kind: "choice", choices: ["model", "flow", "agree"], help: "model: the calibrated EV picks the side. flow: order flow picks it. agree: trade only when both agree." },
  { k: "require_big_move", label: "Need a likely move", kind: "bool", help: "Trade only when the calibrated P(move ≥ 1 stop) is above its base rate." },
  { k: "require_validated", label: "Need validated skill", kind: "bool", help: "Strict trades only on symbols whose bracket skill interval is above 0 (VALID screen)." },
  { k: "drift_action", label: "On drift", kind: "choice", choices: ["base", "pause", "alert"], help: "base: drop the model's weight to 0 for that question. pause: stop trading. alert: log only." },
  { k: "dynamic_sizing", label: "Dynamic sizing", kind: "bool", help: "Scale risk by edge, volatility regime, run drawdown and correlation with open same-side positions." },
  { k: "be_trigger_r", label: "Break-even at (R)", kind: "num", step: 0.25, help: "Move the stop to break-even once the trade is this many R in profit on mainnet. 0 = off." },
  { k: "max_spread_bp", label: "Max mainnet spread (bp)", kind: "num", step: 1, help: "Liquidity filter." },
  { k: "min_depth_mult", label: "Min depth × our size", kind: "num", step: 1, help: "Mainnet depth within 10bp must be at least this multiple of our notional." },
  { k: "allowed_regimes", label: "Allowed regimes", kind: "list", help: "Comma list, e.g. trend_up/normal_vol,range/low_vol. Empty = all." },
  { k: "ensemble", label: "Extra models", kind: "list", help: "Comma list of other models asked on the real state and blended, e.g. kairos. Weights are fitted per source." },
];

function StrategyModal() {
  const symbol = useStore(s => s.symbol);
  const [cur, setCur] = useState<any>(null);
  const [draft, setDraft] = useState<any>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [costs, setCosts] = useState<any>(null);
  useEffect(() => {
    api(`/api/strategy?symbol=${encodeURIComponent(symbol || "")}`).then((r: any) => { setCur(r.strategy); setDraft(r.strategy); setCosts(r.costs); }).catch((e) => setMsg(e.message));
  }, []);
  if (!cur) return <Dialog className="keys"><div className="ph"><span className="code">STRAT</span>Strategy</div><p>{msg || "Loading…"}</p></Dialog>;
  const changed = FIELDS.filter((f) => JSON.stringify(draft[f.k]) !== JSON.stringify(cur[f.k]));
  const apply = async () => {
    const body: any = {};
    changed.forEach((f) => (body[f.k] = draft[f.k]));
    try {
      const r: any = await post("/api/strategy", body);
      setMsg(r.unchanged ? "Nothing changed." : `Saved. Started ${r.run?.name}.`);
      setCur(r.strategy); setDraft(r.strategy);
      loadOverview();
    } catch (e: any) {
      setMsg(e.message);
    }
  };
  const input = (f: Field) => {
    const v = draft[f.k];
    if (f.kind === "bool") return <button className={`kbtn ${v ? "amber" : ""}`} onClick={() => setDraft({ ...draft, [f.k]: !v })}>{v ? "ON" : "OFF"}</button>;
    if (f.kind === "choice") return <span className="seg">{f.choices!.map((c) => <button key={c} className={v === c ? "on" : ""} onClick={() => setDraft({ ...draft, [f.k]: c })}>{c.toUpperCase()}</button>)}</span>;
    if (f.kind === "list") return <input value={(v || []).join(",")} onChange={(e) => setDraft({ ...draft, [f.k]: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} />;
    return <input type="number" step={f.step} value={v} onChange={(e) => setDraft({ ...draft, [f.k]: Number(e.target.value) })} />;
  };
  return (
    <Dialog className="keys strat">
      <div className="ph"><span className="code">STRAT</span>Strategy · FLAT REQUIRED · NEW RUN</div>
      <table className="t" style={{ marginTop: 6 }}>
        <tbody>{FIELDS.map((f) => (
          <tr key={f.k} className={changed.includes(f) ? "sel" : ""}><td className="amb" title={f.help}>{f.label}</td><td>{input(f)}</td></tr>
        ))}</tbody>
      </table>
      {costs && <p className="muted" style={{ margin: "8px 10px 0" }}>COST BP · {symbol} · TARGET {fx(costs.target_bp, 1)} · STOP {fx(costs.stop_bp, 1)} · TIME {fx(costs.time_bp, 1)}</p>}
      {msg && <div className="warnbox">{msg}</div>}
      <div className="keyactions" style={{ margin: "8px 10px" }}>
        <button className="kbtn amber" disabled={!changed.length} onClick={apply}>APPLY {changed.length ? `(${changed.length})` : ""}</button>
        <button className="kbtn" onClick={() => setDraft(cur)}>RESET</button>
        <button className="kbtn" onClick={() => setState({ modal: null })}>CLOSE</button>
      </div>
    </Dialog>
  );
}

function AlpacaUniverseModal() {
  const symbols = useStore(s => s.ov?.settings?.symbols || []);
  const [text, setText] = useState(symbols.join(", "));
  const [u, setU] = useState<any>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api("/api/universe").then(setU).catch(e => setError(e.message)); }, []);
  const apply = async () => {
    setBusy(true); setError("");
    try {
      await post("/api/symbols", { symbols: text.split(/[,\s]+/).filter(Boolean).map((s: string) => s.toUpperCase()) });
      setTimeout(() => location.reload(), 4000);
    } catch (e: any) { setError(e.message); setBusy(false); }
  };
  return <Dialog className="keys"><div className="ph"><span className="code">UNIV</span>Alpaca stock and crypto universe</div>

    <div className="keybox"><label>SYMBOLS<input aria-label="Alpaca monitored symbols" value={text} onChange={e => setText(e.target.value.toUpperCase())} /></label>
      <p>{u?.candidates.map((c: any) => <button key={c.symbol} className="mini" style={{ margin: 3 }} onClick={() => {
        const list = text.split(/[,\s]+/).filter(Boolean); if (!list.includes(c.symbol)) setText([...list, c.symbol].join(", "));
      }}>{c.symbol}</button>)}</p><p className="muted">REQUEST CAP {u?.budget_per_hour ?? "—"}/H</p>
      {error && <p className="dn">{error}</p>}<button className="kbtn amber" disabled={busy} onClick={() => void apply()}>{busy ? "APPLYING…" : "VERIFY & APPLY"}</button></div>
  </Dialog>;
}
