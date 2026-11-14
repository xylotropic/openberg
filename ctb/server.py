"""Dashboard API + SSE + static UI. Binds to 127.0.0.1 by default; no secrets are ever returned."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .broker.bybit_rest import BybitError
from .config import ROOT, TF_PROFILES, key_hint, load_settings, save_env
from .engine import Engine
from .evaluation import metrics as metrics_mod
from .evaluation import report as report_mod
from .evaluation import validation as validation_mod
from .market.store import TIMEFRAMES
from . import models as models_mod
from . import runs as runs_mod
from . import universe as universe_mod

log = logging.getLogger("ctb.server")
NEXT_OUT = ROOT / "web-next" / "out"
UI_NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>Calibrain Harness</title>
<body style="background:#000;color:#ddd;font:15px/1.6 ui-monospace,Menlo,monospace;padding:48px;max-width:720px">
<h2 style="color:#ff9a2e;font-weight:600">The Calibrain Harness UI is not built yet</h2>
<p>The service is running (API on this address, live socket at <code>/ws</code>). Build the terminal once:</p>
<pre style="background:#111;padding:14px;border:1px solid #333">cd web-next &amp;&amp; npm ci &amp;&amp; npm run build</pre>
<p>then reload this page. No restart is needed.</p></body>"""
PUSH_MS = 100
DEV_ORIGINS = {"localhost:3000", "127.0.0.1:3000"}  # `next dev`


def _allowed_origin(origin: str, host: str) -> bool:
    o = origin.split("://", 1)[-1]
    return o == host or o in DEV_ORIGINS


def j(data: Any, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data, default=str), status=status, content_type="application/json")


@web.middleware
async def dev_cors(request: web.Request, handler):
    """Let `next dev` on :3000 call this API while developing the UI. Only those two origins."""
    origin = request.headers.get("Origin", "")
    dev = origin.split("://", 1)[-1] in DEV_ORIGINS
    if dev and request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    if dev:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
    return resp


@web.middleware
async def same_origin_only(request: web.Request, handler):
    """Block cross-site requests: any web page you visit could otherwise POST to this local
    server (toggle trading, swap keys). Require JSON (forces a CORS preflight we never grant)
    and, when the browser sends an Origin, that it is this server."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and not _allowed_origin(origin, request.host):
            return j({"error": "cross-origin request refused"}, 403)
        if not request.content_type.startswith("application/json"):
            return j({"error": "Content-Type must be application/json"}, 415)
    return await handler(request)


def make_app(engine: Engine) -> web.Application:
    s = engine.s
    app = web.Application(middlewares=[dev_cors, same_origin_only])
    routes = web.RouteTableDef()
    research = None
    manual = None
    equity_cache = {}

    def manual_trading():
        nonlocal manual
        if s.venue != 'alpaca' or not engine.executor:
            raise ValueError('Connect the Alpaca paper account first')
        if manual is None or manual.ex is not engine.executor:
            manual = engine.executor.manual
        return manual

    def research_plugins():
        nonlocal research
        if s.venue != "alpaca":
            raise ValueError("Research workspaces require the Alpaca venue")
        if research is None:
            from .research import Research
            research = Research(engine.session, engine.alpaca)
        return research

    async def research_response(fn):
        from .broker.alpaca import AlpacaError
        try:
            return j(await fn())
        except (ValueError, AlpacaError) as exc:
            return j({"error": str(exc)}, 400)
        except Exception as exc:
            return j({"error": str(exc)}, 502)

    def alpha_service():
        if not getattr(engine,'alpha',None):
            raise ValueError('The economic-link strategy requires a connected pinned paper coordinator')
        return engine.alpha

    @routes.get('/api/scalper')
    async def scalper_status(_):
        strategy=getattr(engine,'scalper',None)
        return j(strategy.status() if strategy else {'enabled':False,'error':'Session strategy is not enabled'})

    @routes.get('/api/alpha')
    async def alpha_status(_):
        try: return j(alpha_service().status())
        except ValueError as exc: return j({'error':str(exc)},503)

    @routes.post('/api/alpha/control')
    async def alpha_control(req):
        values=await req.json()
        return await research_response(lambda: alpha_service().control(values.get('action')))

    @routes.get('/api/alpha/evidence/{id}')
    async def alpha_evidence(req):
        service=alpha_service();identity=req.match_info['id']
        source=service.j.one('SELECT id,provider,entity,url,published_at,original_start,observed_at,ingest_kind,content_hash,text,metadata_json,previous_id FROM alpha_sources WHERE id=?',[identity])
        if not source: return j({'error':'Evidence was not found'},404)
        # Original raw bytes remain in the local journal; rendering uses plain escaped text.
        if source.get('metadata_json'): source['metadata_json']={k:v for k,v in source['metadata_json'].items() if k!='documents'}
        return j({'source':source,'passages':service.j.query('SELECT * FROM alpha_passages WHERE source_id=?',[identity])})

    @routes.get('/api/alpha/classification/{id}')
    async def alpha_classification(req):
        row=alpha_service().j.one('SELECT * FROM alpha_classifications WHERE id=?',[req.match_info['id']])
        return j(row if row else {'error':'Classification was not found'},200 if row else 404)

    @routes.post('/api/manual/preview')
    async def manual_preview(req):
        values = await req.json()
        return await research_response(lambda: manual_trading().preview(values))

    @routes.get('/api/manual/orders')
    async def manual_orders(_):
        return await research_response(lambda: manual_trading().orders())

    @routes.post('/api/manual/orders')
    async def manual_send(req):
        values = await req.json()
        return await research_response(lambda: manual_trading().send(str(values.get('id', ''))))

    @routes.post('/api/manual/orders/{id}/cancel')
    async def manual_cancel(req):
        return await research_response(lambda: manual_trading().cancel(req.match_info['id']))

    @routes.get("/api/research/plugins")
    async def plugins(_):
        return j({"plugins": [
            {"id": "alpaca-news", "name": "Alpaca Newsroom", "source": "https://data.alpaca.markets/v1beta1/news",
             "pricing": "Uses the current Alpaca entitlement; no paid upgrade is enabled"},
            {"id": "sec-edgar", "name": "SEC EDGAR · all forms", "source": "https://data.sec.gov/submissions/",
             "pricing": "Free public SEC API; no API key"}]})

    @routes.get("/api/research/news")
    async def research_news(req):
        async def get():
            result = await research_plugins().newsroom(req.query.get("symbol", ""), req.query.get("page_token"))
            if not req.query.get("page_token"):
                items = {str(n['id']): n for n in result['items']}
                wanted = {s.strip().replace('/', '').upper() for s in req.query.get('symbol', '').split(',') if s.strip()}
                for n in reversed(engine.store.context.get('news_articles', [])):
                    if not wanted or wanted.intersection(s.replace('/', '').upper() for s in n.get('symbols', [])):
                        if str(n['id']) not in items or n.get('updated_at', '') >= items[str(n['id'])].get('updated_at', ''):
                            items[str(n['id'])] = n
                result['items'] = sorted(items.values(), key=lambda n: n.get('created_at', ''), reverse=True)[:50]
            result["status"] = engine.store.context.get("news_status", {})
            return result
        return await research_response(get)

    @routes.get("/api/research/filings")
    async def research_filings(req):
        async def get():
            return await research_plugins().filings(req.query.get("query", "AAPL"), archive=req.query.get("archive"),
                offset=max(0, int(req.query.get("offset", 0))), limit=min(200, max(1, int(req.query.get("limit", 100)))),
                form=req.query.get("form", ""))
        return await research_response(get)

    @routes.get("/api/options")
    async def options(req):
        return await research_response(lambda: research_plugins().options(req.query.get("underlying", "SPY").upper(),
            page_token=req.query.get("page_token"), expiry=req.query.get("expiry"), kind=req.query.get("type")))

    @routes.post("/api/options/order")
    async def options_order(req):
        async def submit():
            if s.venue != "alpaca" or not engine.executor:
                raise ValueError("Connect the Alpaca paper account first")
            body = await req.json()
            qty = body.get("qty")
            if isinstance(qty, bool) or not isinstance(qty, int):
                raise ValueError("Quantity must be a whole number")
            return await engine.executor.option_entry(str(body.get("symbol", "")).upper(), qty, float(body.get("limit_price", 0)))
        return await research_response(submit)

    @routes.get('/api/options/surface')
    async def option_surface(req):
        return await research_response(lambda: research_plugins().option_surface(
            req.query.get('underlying', 'SPY').upper(), kind=req.query.get('type', 'call'),
            days=int(req.query.get('days', '90')), band=float(req.query.get('band', '.25'))))

    @routes.get("/")
    async def index(_: web.Request) -> web.StreamResponse:
        if (NEXT_OUT / "index.html").exists():
            return web.FileResponse(NEXT_OUT / "index.html")
        return web.Response(status=503, content_type="text/html", text=UI_NOT_BUILT)

    @routes.get("/api/overview")
    async def overview(_: web.Request) -> web.Response:
        ex = engine.executor
        st = engine.store
        return j({
            "settings": s.public_view(),
            "run": getattr(engine, "run", None),
            "uptime_s": time.time() - engine.started_at,
            "data": {
                "ws_connected": st.ws_connected, "ws_messages": st.ws_messages, "ws_last_msg_age_s": time.time() - st.ws_last_msg if st.ws_last_msg else None,
                "errors": list(st.data_errors)[-15:], "fear_greed": st.context.get("fear_greed"), "macro": st.context.get("macro"),
            },
            # "kairos" = the ACTIVE classification model's status (key name kept for older clients)
            "kairos": {"provider": engine.kairos.provider, "label": engine.kairos.spec.label, "model_id": engine.kairos.model,
                       "configured": engine.kairos.configured, "requests_last_hour": engine.budget.used(),
                       "budget_per_hour": engine.budget.per_hour, "news_requests_last_hour": engine.news_budget.used(),
                       "last_error": engine.last_decision_error, "probe_rate": engine.probe_rate, "inflight": sorted(engine.inflight)},
            "account": ex.account_view() if ex else None,
            "executor_error": (ex.sync_error if ex else "no Alpaca paper keys configured" if s.venue == "alpaca" else "no Bybit testnet keys configured"),
            "labelled_total": (engine.db.one("SELECT COUNT(*) n FROM decisions WHERE label_json IS NOT NULL") or {}).get("n", 0),
        })

    @routes.get("/api/market/{symbol:.+}")
    async def market(req: web.Request) -> web.Response:
        sym = req.match_info["symbol"].upper()
        if sym not in engine.store.state:
            return j({"error": "unknown symbol"}, 404)
        tf = req.query.get("tf", s.decision_tf)
        if tf not in TIMEFRAMES:
            tf = s.decision_tf
        if s.venue == 'alpaca' and hasattr(engine, 'feed'):
            try:
                await engine.feed.ensure_history(sym, tf)
            except Exception as exc:
                return j({'error': f'Chart history unavailable: {exc}'}, 502)
        st = engine.store[sym]
        bars = [b.as_dict() for b in st.bars[tf][-300:]]
        preview = engine.latest_state.get(sym)
        return j({
            "symbol": sym, "tf": tf, "bars": bars, "ticker": st.ticker, "testnet_ticker": st.testnet_ticker,
            "book": {"bids": st.bids[:15], "asks": st.asks[:15], "source_ts": st.book_ts,
                     "age_s": time.time() - st.book_ts if st.book_ts else None,
                     **(engine.feed.book_info(sym) if s.venue == 'alpaca' and hasattr(engine, 'feed') else {})},
            "flow": {"5m": st.flow_window(5), "15m": st.flow_window(15), "60m": st.flow_window(60)},
            "liquidations": {"15m": st.liq_window(15), "60m": st.liq_window(60), "recent": list(st.liquidations)[-20:]},
            "oi_history": st.oi_history[-96:], "ls_ratio": st.ls_ratio[-48:], "funding_history": st.funding_history[-21:],
            "instrument": st.instrument, "state_preview": preview,
            "news": [n for n in engine.store.context.get("news", []) if sym in n["symbols"] or n["market_wide"]][:20],
        })

    @routes.get("/api/decisions")
    async def decisions(req: web.Request) -> web.Response:
        sym = req.query.get("symbol")
        limit = min(int(req.query.get("limit", 100)), 1000)
        where = "probe IS NULL" + (" AND symbol=?" if sym else "")
        rows = engine.db.query(f"SELECT id, ts, symbol, action, decision_json, latency_ms, error_kind, error_msg, issues_json, parsed_json, "
                               f"price, sl_bp, tp_bp, horizon_min, label_json, probe, qset_version FROM decisions WHERE {where} ORDER BY id DESC LIMIT ?",
                               ([sym] if sym else []) + [limit])
        return j([engine._decision_view(r) for r in rows])

    @routes.get("/api/decision/{id}")
    async def decision(req: web.Request) -> web.Response:
        r = engine.db.one("SELECT * FROM decisions WHERE id=?", (int(req.match_info["id"]),))
        if not r:
            return j({"error": "not found"}, 404)
        probes = engine.db.query("SELECT id, probe, parsed_json, error_kind FROM decisions WHERE probe_of=?", (r["id"],))
        r["probes"] = probes
        return j(r)

    @routes.get("/api/trades")
    async def trades(req: web.Request) -> web.Response:
        run = req.query.get("run")
        if run:
            return j(engine.db.query("SELECT * FROM trades WHERE run_id=? ORDER BY id DESC LIMIT 1000", (int(run),)))
        return j(engine.db.query("SELECT * FROM trades ORDER BY id DESC LIMIT 1000"))

    def run_summary(m: dict[str, Any]) -> dict[str, Any]:
        t, e, mq, ops = m["trading"], m.get("edge") or {}, m.get("model_quality") or {}, m["ops"]
        return {
            "decisions": ops["requests"], "errors": ops["requests"] - ops["ok"], "labelled": m.get("labelled"),
            "latency_p50_ms": ops.get("latency_ms_p50"),
            "closed": t.get("closed", 0), "net_pnl": t.get("net_pnl_usdt"), "fees": t.get("fees_usdt"), "win_rate": t.get("win_rate"),
            "avg_r": t.get("avg_r"), "profit_factor": t.get("profit_factor"), "max_dd": t.get("max_drawdown_usdt"),
            "by_side": t.get("by_side"), "attempts": t.get("attempts_by_status"),
            "entry_slip_bp": ((t.get("execution") or {}).get("entry_slip_bp") or {}).get("mean"),
            "stop_slip_bp": ((t.get("execution") or {}).get("stop_exit_slip_bp") or {}).get("mean"),
            "strict_n": e.get("strict_signals_n"), "strict_r": e.get("strict_signals_mean_r"),
            "explore_n": e.get("exploration_signals_n"), "explore_r": e.get("exploration_signals_mean_r"),
            "skill_long": (mq.get("long_bracket") or {}).get("skill_vs_climatology"),
            "skill_short": (mq.get("short_bracket") or {}).get("skill_vs_climatology"),
            "vs_base_long": ((m.get("vs_base_rates") or {}).get("long_bracket") or {}).get("kairos_skill_vs_base"),
        }

    @routes.get("/api/runs")
    async def runs(_: web.Request) -> web.Response:
        out = []
        for r in engine.db.query("SELECT * FROM runs ORDER BY id"):
            m = await asyncio.to_thread(metrics_mod.compute, engine.db, run_id=r["id"])
            open_n = (engine.db.one("SELECT COUNT(*) n FROM trades WHERE run_id=? AND status IN ('open','opening')", (r["id"],)) or {}).get("n", 0)
            out.append({**r, "current": r["ended_ts"] is None, "open_trades": open_n, **run_summary(m)})
        m = await asyncio.to_thread(metrics_mod.compute, engine.db)
        overall = {"id": None, "name": "Overall", "started_ts": out[0]["started_ts"] if out else None, "ended_ts": None,
                   "open_trades": (engine.db.one("SELECT COUNT(*) n FROM trades WHERE status IN ('open','opening')") or {}).get("n", 0),
                   **run_summary(m)}
        return j({"runs": out, "overall": overall})

    @routes.post("/api/runs/start")
    async def run_start(req: web.Request) -> web.Response:
        body = await req.json()
        if engine.db.one("SELECT id FROM trades WHERE status IN ('open','opening') LIMIT 1"):
            return j({"error": "close the open bot trades first (KILL, or wait for their exits) so each run's P&L stays its own"}, 409)
        engine.run = runs_mod.start(engine.db, s, name=(body.get("name") or None), notes=body.get("notes"))
        engine.emit("event", engine.db.event("warn", "run", f"started {engine.run['name']}"))
        engine.emit("settings", {})
        return j(engine.run)

    @routes.get("/api/news")
    async def news(_: web.Request) -> web.Response:
        forecasts = engine.db.query("SELECT * FROM news ORDER BY ts DESC LIMIT 200")
        return j({"headlines": engine.store.context.get("news", [])[:60], "forecasts": forecasts,
                  "items": engine.store.context.get("news_articles", [])[:100],
                  "status": engine.store.context.get("news_status", {})})

    @routes.get("/api/events")
    async def events(_: web.Request) -> web.Response:
        return j(engine.db.query("SELECT * FROM events ORDER BY id DESC LIMIT 200"))

    @routes.get("/api/metrics")
    async def metrics(req: web.Request) -> web.Response:
        sym = req.query.get("symbol") or None
        hours = req.query.get("hours")
        tf = req.query.get("tf") or None
        qset = req.query.get("qset") or None
        run = int(req.query["run"]) if req.query.get("run") else None
        model = req.query.get("model") or None
        since = time.time() - float(hours) * 3600 if hours else None
        m = await asyncio.to_thread(metrics_mod.compute, engine.db, symbol=sym, since_ts=since, decision_tf=tf, qset=qset, run_id=run, model=model)
        m["calibration_weights"] = {"weights": engine.calibrator.weights, "fitted_at": engine.calibrator.fitted_at,
                                    "report": engine.calibrator.report}
        m["observations"] = report_mod.observations(m)
        return j(m)

    @routes.get("/api/validation")
    async def validation(req: web.Request) -> web.Response:
        """Per-symbol skill with intervals, time-of-day / session / regime / event splits, drift,
        walk-forward, edge vs cost and execution quality (independent windows only)."""
        run = int(req.query["run"]) if req.query.get("run") else None
        model = req.query.get("model") or (None if run else s.model_provider)
        tf = req.query.get("tf") or (None if run else s.decision_tf)
        v = await asyncio.to_thread(validation_mod.compute, engine.db, run_id=run, model=model, decision_tf=tf)
        v["filters"] = {"run": run, "model": model, "tf": tf}
        cost_symbol = req.query.get("symbol") if req.query.get("symbol") in s.symbols else s.symbols[0]
        v["live_cost_model"] = engine.costs.outcome_costs(symbol=cost_symbol)
        v["cost_symbol"] = cost_symbol
        v["calibration"] = {"weights": engine.calibrator.weights_view(), "overrides": engine.calibrator.overrides,
                            "report": {g: engine.calibrator.report.get(g) for g in ("bracket", "return_distribution", "direction", "big_move")}}
        return j(v)

    @routes.get("/api/strategy")
    async def strategy_get(req: web.Request) -> web.Response:
        symbol = req.query.get("symbol") if req.query.get("symbol") in s.symbols else s.symbols[0]
        return j({"strategy": s.strategy_view(), "choices": s.STRATEGY_CHOICES,
                  "costs": engine.costs.outcome_costs(symbol=symbol), "cost_symbol": symbol})

    @routes.post("/api/strategy")
    async def strategy_set(req: web.Request) -> web.Response:
        """Change strategy settings (only while flat; starts a new run)."""
        body = await req.json()
        try:
            return j(await engine.set_strategy(body if isinstance(body, dict) else {}))
        except ValueError as exc:
            return j({"error": str(exc)}, 409)

    @routes.get("/api/report")
    async def report(req: web.Request) -> web.Response:
        run = int(req.query["run"]) if req.query.get("run") else None
        m = await asyncio.to_thread(metrics_mod.compute, engine.db, model=req.query.get("model") or None, run_id=run)
        m["validation"] = await asyncio.to_thread(validation_mod.compute, engine.db, run_id=run, model=req.query.get("model") or None)
        md = report_mod.render(m, s.public_view())
        out = ROOT / "data" / "kairos_report.md"
        out.write_text(md, encoding="utf-8")
        return web.Response(text=md, content_type="text/markdown")

    # ---------------------------------------------------------------- controls
    @routes.post("/api/trading")
    async def trading(req: web.Request) -> web.Response:
        body = await req.json()
        enabled = bool(body.get("enabled"))
        if enabled and not engine.executor:
            return j({"error": "no Bybit testnet keys configured"}, 400)
        if enabled and engine.executor and engine.executor.halted_reason:
            if not body.get("clear_halt"):
                return j({"error": f"halted: {engine.executor.halted_reason}. Send clear_halt=true to resume."}, 409)
            engine.executor.halted_reason = None
        s.trading_enabled = enabled
        if s.scalper_enabled: save_env({'CTB_TRADING_ENABLED':'1' if enabled else '0'})
        engine.emit("event", engine.db.event("warn", "trading_toggled", f"testnet trading {'ENABLED' if enabled else 'disabled'} from dashboard"))
        return j({"trading_enabled": s.trading_enabled})

    @routes.post("/api/close")
    async def close(req: web.Request) -> web.Response:
        body = await req.json()
        if not engine.executor:
            return j({"error": "no executor"}, 400)
        ok = await engine.executor.close(str(body.get("symbol", "")).upper(), "manual")
        return j({"closed": ok})

    @routes.post("/api/kill")
    async def kill(_: web.Request) -> web.Response:
        if getattr(engine,'alpha',None):
            await engine.alpha.control('close_owned')
        if engine.executor:
            await engine.executor.kill("dashboard kill switch")
        s.trading_enabled = False
        if s.scalper_enabled: save_env({'CTB_TRADING_ENABLED':'0'})
        return j({"killed": True})

    @routes.post("/api/decide")
    async def decide_now(req: web.Request) -> web.Response:
        body = await req.json()
        sym = str(body.get("symbol", "")).upper()
        if sym not in engine.store.state:
            return j({"error": "unknown symbol"}, 404)
        if not engine.kairos.configured:
            return j({"error": f"{engine.kairos.spec.key_env} is not set"}, 400)
        if s.venue == "alpaca":
            ready, why = engine.feed.decision_ready(sym)
            if not ready:
                return j({"error": why}, 409)
        row = await engine.decide(sym, force_probe=bool(body.get("probe")))
        return j(engine._decision_view(row) if row else {"skipped": True})

    @routes.post("/api/decision-tf")
    async def decision_tf(req: web.Request) -> web.Response:
        if s.scalper_enabled:
            return j({'error':'The session scan runs every 30 seconds; chart timeframes do not change it'},409)
        """Switch the trading timeframe live: cadence, horizon and stop sizing change together."""
        body = await req.json()
        tf = str(body.get("tf", ""))
        if tf not in TF_PROFILES:
            return j({"error": f"timeframe must be one of {sorted(TF_PROFILES, key=int)} (minutes)"}, 400)
        old = s.decision_tf
        updates = s.apply_tf_profile(tf)
        need = s.required_rph(tf, engine.probe_rate)
        if body.get("raise_budget") and need > s.kairos_max_requests_per_hour:
            s.kairos_max_requests_per_hour = need
            engine.budget.per_hour = need
            updates["CTB_KAIROS_MAX_RPH"] = str(need)
        save_env(updates)
        p = TF_PROFILES[tf]
        msg = (f"trading timeframe {TF_PROFILES[old]['label'] if old in TF_PROFILES else old} -> {p['label']}: "
               f"horizon {p['horizon_bars'] * int(tf)} min, stop from {p['atr_tf']}m ATR, needs ~{need} req/h "
               f"(budget {s.kairos_max_requests_per_hour}). Open positions keep their original time-stop.")
        engine.emit("event", engine.db.event("warn", "decision_tf", msg))
        engine.emit("settings", {"decision_tf": tf})
        return j(s.public_view())

    @routes.post("/api/settings")
    async def settings(req: web.Request) -> web.Response:
        body = await req.json()
        allowed = {"min_edge_r": float, "min_setup_quality": float, "risk_per_trade_pct": float, "allow_short": bool, "max_positions": int,
                   "exploration_mode": bool, "explore_min_lean_r": float, "explore_risk_pct": float}
        if "max_positions" in body:
            try:
                n = int(body["max_positions"])
            except (TypeError, ValueError):
                return j({"error": "max_positions must be a whole number"}, 400)
            if not 1 <= n <= 50:
                return j({"error": "max_positions must be between 1 and 50"}, 400)
            body["max_positions"] = n
        for k, typ in allowed.items():
            if k in body:
                setattr(s, k, typ(body[k]))
        if "max_positions" in body:
            save_env({"CTB_MAX_POSITIONS": str(s.max_positions)})  # survives restarts; open positions untouched
        if "exploration_mode" in body:
            save_env({"CTB_EXPLORATION": "1" if s.exploration_mode else "0"})  # survives restarts
        if "probe_rate" in body:
            engine.probe_rate = max(0.0, min(float(body["probe_rate"]), 0.5))
        engine.emit("event", engine.db.event("info", "settings", f"settings updated: { {k: body[k] for k in body if k in allowed or k == 'probe_rate'} }"))
        return j(s.public_view())

    # ---------------------------------------------------------------- terminal panels
    @routes.get("/api/monitor")
    async def monitor(_: web.Request) -> web.Response:
        """Everything the always-on panels need, in one poll."""
        rows, tape = [], []
        latest = {r["symbol"]: r for r in engine.db.query(
            "SELECT d.symbol, d.ts, d.action, d.error_kind, d.decision_json FROM decisions d "
            "JOIN (SELECT symbol, MAX(id) mid FROM decisions WHERE probe IS NULL GROUP BY symbol) m ON d.id = m.mid")}
        for sym in engine.store.symbols:
            st = engine.store[sym]
            t = st.ticker
            c15 = st.closes("15")[-96:]  # 24h of 15m closes
            last = t.get("lastPrice")
            tn = st.testnet_ticker.get("lastPrice")
            d = latest.get(sym)
            rows.append({
                "symbol": sym, "last": last, "mark": t.get("markPrice"), "index": t.get("indexPrice"),
                "bid": t.get("bid1Price"), "ask": t.get("ask1Price"),
                "pct24h": 100 * float(t["price24hPcnt"]) if t.get("price24hPcnt") else None,
                "high24h": t.get("highPrice24h"), "low24h": t.get("lowPrice24h"),
                "turnover24h": float(t["turnover24h"]) if t.get("turnover24h") else None,
                "oi_value": float(t["openInterestValue"]) if t.get("openInterestValue") else None,
                "funding": float(t["fundingRate"]) if t.get("fundingRate") else None,
                "next_funding_ms": int(t["nextFundingTime"]) if t.get("nextFundingTime") else None,
                "testnet_last": tn,
                "divergence_bp": round(10000 * (float(tn) / float(last) - 1), 1) if tn and last else None,
                "spark": c15, "age_s": round(time.time() - st.last_update, 1) if st.last_update else None,
                "decision": {"ts": d["ts"], "action": d["action"], "error": d["error_kind"],
                             "ev_long": (d["decision_json"] or {}).get("ev_long_r"), "ev_short": (d["decision_json"] or {}).get("ev_short_r")} if d else None,
            })
            for ts, side, notional in list(st.liquidations)[-40:]:
                tape.append({"ts": ts / 1000, "symbol": sym, "kind": "LIQ", "side": "LONG" if side == "Buy" else "SHORT", "notional": notional})
            for ts, side, qty, px, notional in list(st.large_trades)[-40:]:
                tape.append({"ts": ts / 1000, "symbol": sym, "kind": "BLOCK", "side": "BUY" if side == "Buy" else "SELL", "notional": notional, "price": px, "qty": qty})
        tape.sort(key=lambda x: x["ts"], reverse=True)
        ctx = engine.store.context
        return j({"rows": rows, "tape": tape[:80], "universe": ctx.get("universe", []), "universe_ts": ctx.get("universe_ts"),
                  "news": ctx.get("news", [])[:40], "macro": ctx.get("macro", {}), "fear_greed": ctx.get("fear_greed"), "server_ts": time.time()})

    @routes.post('/api/watchlist')
    async def add_watch_symbol(req):
        async def add():
            if s.venue != 'alpaca':
                raise ValueError('The quote watchlist requires Alpaca')
            body = await req.json()
            symbol = str(body.get('symbol', '')).strip().upper()
            if symbol in engine.store.state:
                return {'symbol': symbol, 'watchlist_symbols': s.watchlist_symbols}
            if len(engine.store.symbols) >= 500:
                raise ValueError('The watchlist supports up to 500 symbols')
            asset = await engine.alpaca.asset(symbol)
            if not asset.get('tradable'):
                raise ValueError('This symbol is not tradable on Alpaca')
            symbol = asset.get('symbol') or symbol
            from .market.store import SymbolState
            if symbol not in engine.store.state:
                engine.store.state[symbol] = SymbolState(symbol)
                engine.store.symbols.append(symbol)
                s.watchlist_symbols = list(engine.store.symbols)
                save_env({'CTB_WATCHLIST': ','.join(s.watchlist_symbols)})
            engine.feed.watch_symbol(symbol)
            await engine.feed.refresh_quotes()
            engine.emit('settings', {})
            return {'symbol': symbol, 'watchlist_symbols': s.watchlist_symbols}
        return await research_response(add)

    @routes.get("/api/perf")
    async def perf(req: web.Request) -> web.Response:
        """24h relative performance: % change of each symbol's 5m closes from the first common bar."""
        out = {}
        for sym in s.symbols:
            bars = engine.store[sym].closed_bars("5")[-288:]
            if bars:
                out[sym] = [(b.t // 1000, b.c) for b in bars]
        if not out:
            return j({})
        start = max(v[0][0] for v in out.values())
        res = {}
        for sym, pts in out.items():
            pts = [p for p in pts if p[0] >= start]
            base = pts[0][1]
            res[sym] = [[t, round(100 * (c / base - 1), 4)] for t, c in pts]
        return j(res)

    @routes.get('/api/equity-curve')
    async def equity_curve(req):
        if s.venue!='alpaca' or not engine.executor:
            return j({'error':'Connect the Alpaca paper account first'},400)
        period=req.query.get('period','1W')
        if period not in ('1D','1W','1M'): return j({'error':'Choose 1D, 1W or 1M'},400)
        key=(engine.executor.account_id,period)
        if key not in equity_cache or time.time()-equity_cache[key]['at']>30:
            try:
                raw=await engine.executor.client.request('GET','/v2/account/portfolio/history',params={
                    'period':period,'timeframe':'1D' if period=='1M' else '1Min' if period=='1D' else '15Min',
                    'intraday_reporting':'continuous','pnl_reset':'no_reset'})
                import math
                points={int(t):float(v) for t,v in zip(raw.get('timestamp',[]),raw.get('equity',[]))
                    if type(t) in (int,float) and type(v) in (int,float) and math.isfinite(t) and math.isfinite(v) and t>0 and v>=0}
                equity_cache[key]={'at':time.time(),'points':sorted(points.items())}
            except Exception as exc: return j({'error':str(exc)},502)
        points=list(equity_cache[key]['points'])
        ex=engine.executor
        if not ex.sync_error and time.time()-ex.last_sync<60 and ex.wallet.get('equity') is not None:
            current=(int(ex.last_sync),float(ex.wallet['equity']))
            if not points or current[0]>points[-1][0]: points.append(current)
        return j({'points':points,'currency':'USD','period':period,'source':'Alpaca paper account','as_of':points[-1][0] if points else None})

    # ---------------------------------------------------------------- keys
    @routes.get("/api/keys")
    async def keys_status(_: web.Request) -> web.Response:
        return j({
            "kairos": {**key_hint(s.kairos_api_key), "base_url": s.kairos_base_url},
            **{provider: {**key_hint(spec.api_key), "base_url": spec.base_url, "model_id": spec.model}
               for provider, spec in models_mod.MODELS.items() if provider != "kairos"},
            "active_model": s.model_provider,
            "alpaca": {"api_key": key_hint(s.alpaca_api_key), "api_secret": key_hint(s.alpaca_api_secret),
                       "connected": bool(s.venue == "alpaca" and engine.executor and not engine.executor.sync_error),
                       "paper": True, "account_id": s.alpaca_account_id},
            "bybit": {"api_key": key_hint(s.bybit_api_key), "api_secret": key_hint(s.bybit_api_secret),
                      "connected": bool(engine.executor and not engine.executor.sync_error),
                      "error": engine.executor.sync_error if engine.executor else None},
        })

    @routes.post("/api/keys/alpaca")
    async def keys_alpaca(req):
        async def save():
            body = await req.json()
            key, secret = str(body.get("api_key", "")).strip(), str(body.get("api_secret", "")).strip()
            if not key or not secret:
                raise ValueError("Alpaca paper API key and secret are both required")
            return await engine.set_alpaca_keys(key, secret)
        return await research_response(save)

    @routes.post("/api/keys/bybit")
    async def keys_bybit(req: web.Request) -> web.Response:
        body = await req.json()
        key, secret = str(body.get("api_key", "")).strip(), str(body.get("api_secret", "")).strip()
        if not key or not secret:
            return j({"error": "both API key and secret are required"}, 400)
        try:
            return j(await engine.set_bybit_keys(key, secret))
        except BybitError as exc:
            hint = " (10003 = key not valid on TESTNET; create it at testnet.bybit.com)" if exc.ret_code in (10003, 10004, 33004) else ""
            return j({"error": f"Bybit rejected the keys: {exc.ret_code} {exc.ret_msg}{hint}"}, 400)
        except ValueError as exc:
            return j({"error": str(exc)}, 409)

    @routes.post("/api/keys/kairos")
    async def keys_kairos(req: web.Request) -> web.Response:
        body = await req.json()
        key = str(body.get("api_key", "")).strip()
        if not key:
            return j({"error": "API key is required"}, 400)
        try:
            return j(await engine.set_kairos_key(key, body.get("base_url") or None))
        except ValueError as exc:
            return j({"error": str(exc)}, 400)

    @routes.post("/api/keys/kairos/test")
    async def keys_kairos_test(_: web.Request) -> web.Response:
        return j(await engine.test_model("kairos"))

    @routes.post("/api/keys/{provider}")
    async def keys_model(req: web.Request) -> web.Response:
        provider = req.match_info["provider"]
        if provider not in models_mod.MODELS:
            return j({"error": "unknown model provider"}, 404)
        body = await req.json()
        key = str(body.get("api_key", "")).strip()
        if not key:
            return j({"error": "API key is required"}, 400)
        return j(await engine.set_model_key(provider, key))

    @routes.post("/api/keys/{provider}/test")
    async def keys_model_test(req: web.Request) -> web.Response:
        provider = req.match_info["provider"]
        if provider not in models_mod.MODELS:
            return j({"error": "unknown model provider"}, 404)
        return j(await engine.test_model(provider))

    @routes.post("/api/model")
    async def set_model(req: web.Request) -> web.Response:
        """Switch the classification model (only while flat; starts a new run)."""
        body = await req.json()
        try:
            return j(await engine.set_model(str(body.get("provider", "")).strip().lower()))
        except ValueError as exc:
            return j({"error": str(exc)}, 409)

    @routes.get("/stream")
    async def stream(req: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await resp.prepare(req)
        q: asyncio.Queue = asyncio.Queue()
        engine.subscribers.add(q)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    await resp.write(f"data: {msg}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            engine.subscribers.discard(q)
        return resp

    # ---------------------------------------------------------------- symbol universe
    @routes.get("/api/universe")
    async def universe(_: web.Request) -> web.Response:
        if s.venue == "alpaca":
            from .broker.alpaca import asset_class
            known = list(dict.fromkeys([*s.symbols, "SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN", "META", "GOOGL", "BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "LTC/USD"]))
            cands = [{"symbol": sym, "selected": sym in s.symbols, "asset_type": asset_class(sym)} for sym in known]
        else:
            cands = await universe_mod.candidates(engine.session, engine.store.context.get("universe_all", []), s.symbols, s.max_notional_usdt)
        per_sym = s.required_rph(probe_rate=engine.probe_rate) / max(1, len(s.symbols))
        return j({"selected": s.symbols, "candidates": cands, "max_symbols": universe_mod.MAX_SYMBOLS,
                  "budget_per_hour": s.kairos_max_requests_per_hour, "rph_per_symbol": round(per_sym, 2),
                  "decision_tf": s.decision_tf, "thin_depth_usd": s.max_notional_usdt,
                  "max_entry_slip_bp": s.max_entry_slip_bp})

    @routes.post("/api/symbols")
    async def set_symbols(req: web.Request) -> web.Response:
        """Change the traded universe. Saved to .env, then the service restarts itself (~15 s) so
        every feed, stream, store and instrument filter is rebuilt for the new list."""
        body = await req.json()
        syms = [str(x).strip().upper() for x in body.get("symbols") or []]
        if s.scalper_enabled and set(syms)!= {'SPY','TSLA','AAPL','GOOGL','MSFT','NVDA','AMD','INTC','BTC/USD','ETH/USD','SOL/USD'}:
            return j({'error':'The active session strategy uses SPY, seven equities, BTC/USD, ETH/USD and SOL/USD. Add other symbols to the watchlist.'},409)
        if s.venue == "alpaca":
            errs = []
            if not syms or len(syms) > 30 or len(set(syms)) != len(syms):
                errs.append("Choose 1–30 distinct Alpaca symbols")
            if not errs:
                for sym in syms:
                    try:
                        asset = await engine.alpaca.asset(sym)
                        if not asset.get("tradable"):
                            errs.append(f"{sym} is not tradable on Alpaca")
                    except Exception as exc:
                        errs.append(f"{sym}: {exc}")
        else:
            errs = await universe_mod.validate(engine.session, syms)
        if errs:
            return j({"error": "; ".join(errs)}, 400)
        open_syms = {r["symbol"] for r in engine.db.query("SELECT symbol FROM trades WHERE status IN ('open','opening')")}
        if dropped := sorted(open_syms - set(syms)):
            return j({"error": f"{', '.join(dropped)} still has an open bot trade; close it (or wait for its exit) before removing it"}, 409)
        minutes = s.decision_tf_minutes
        need = int(round(len(syms) * (60 / minutes) * ((2 if s.symmetrize else 1) + 3 * engine.probe_rate)))
        if s.scalper_enabled: need=s.required_rph()
        updates = {"CTB_SYMBOLS": ",".join(syms)}
        if need > s.kairos_max_requests_per_hour:
            if not body.get("raise_budget"):
                return j({"error": f"{len(syms)} symbols on {minutes}m bars need ~{need} model requests/hour; the cap is "
                                   f"{s.kairos_max_requests_per_hour}. Remove symbols, use a slower trading timeframe, or raise the cap.",
                          "need": need}, 409)
            updates["CTB_KAIROS_MAX_RPH"] = str(need)
        save_env(updates)
        resume = bool(s.trading_enabled)
        engine.emit("event", engine.db.event("warn", "symbols", f"universe set to {len(syms)} symbols ({', '.join(syms)}); "
                                             f"~{need} Kairos req/h; restarting{' (auto-trade resumes)' if resume else ''}"))

        def reexec() -> None:
            env = dict(os.environ)
            if resume:
                env["CTB_RESUME_TRADING"] = "1"  # a restart we asked for must not silently stop the test
            os.execve(sys.executable, [sys.executable, "-m", "ctb.server"], env)

        asyncio.get_running_loop().call_later(1.0, reexec)
        return j({"ok": True, "symbols": syms, "need_rph": need, "restarting": True})

    @routes.get("/ws")
    async def ws_endpoint(req: web.Request) -> web.WebSocketResponse:
        """One socket per tab. Pushes engine events as they happen, plus market ticks, the order
        book and the forming candle for the symbol the client is watching, every PUSH_MS (only
        what changed). Replaces polling, so the UI updates within ~100 ms of Bybit."""
        origin = req.headers.get("Origin")
        if origin and not _allowed_origin(origin, req.host):
            raise web.HTTPForbidden(text="cross-origin websocket refused")
        ws = web.WebSocketResponse(heartbeat=20, compress=True)
        await ws.prepare(req)
        q: asyncio.Queue = asyncio.Queue()
        engine.subscribers.add(q)
        watch = {"symbol": s.symbols[0], "tf": "5"}

        async def reader() -> None:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        m = json.loads(msg.data)
                    except ValueError:
                        continue
                    if m.get("op") == "watch":
                        if m.get("symbol") in engine.store.state:
                            watch["symbol"] = m["symbol"]
                        if m.get("tf") in TIMEFRAMES:
                            watch["tf"] = m["tf"]
                        if s.venue == 'alpaca':
                            engine.feed.watch_symbol(watch['symbol'], watch['tf'])

        async def forward_events() -> None:
            while True:
                await ws.send_str(await q.get())

        async def market() -> None:
            last_sent: dict[str, Any] = {}
            while True:
                await asyncio.sleep(PUSH_MS / 1000)
                ticks = {}
                for sym in engine.store.symbols:
                    st = engine.store[sym]
                    t, tn = st.ticker, st.testnet_ticker
                    row = (t.get("lastPrice"), t.get("markPrice"), t.get("bid1Price"), t.get("ask1Price"), tn.get("lastPrice"))
                    if last_sent.get(sym) != row:
                        last_sent[sym] = row
                        ticks[sym] = {"last": row[0], "mark": row[1], "bid": row[2], "ask": row[3], "testnet_last": row[4],
                                      "pct24h": 100 * float(t["price24hPcnt"]) if t.get("price24hPcnt") else None}
                st = engine.store[watch["symbol"]]
                info = engine.feed.book_info(watch['symbol']) if s.venue == 'alpaca' else {}
                book = (st.bids[:15], st.asks[:15], st.book_ts, info)
                bars = st.bars.get(watch["tf"]) or []
                bar = bars[-1].as_dict() if bars else None
                out: dict[str, Any] = {}
                if ticks:
                    out["ticks"] = ticks
                if last_sent.get("_book") != (watch["symbol"], book):
                    last_sent["_book"] = (watch["symbol"], book)
                    out["book"] = {"symbol": watch["symbol"], "bids": book[0], "asks": book[1],
                                   "source_ts": book[2], **info}
                if bar and last_sent.get("_bar") != (watch["symbol"], watch["tf"], bar):
                    last_sent["_bar"] = (watch["symbol"], watch["tf"], bar)
                    out["bar"] = {"symbol": watch["symbol"], "tf": watch["tf"], **bar}
                if out:
                    out["ts"] = time.time()
                    await ws.send_str(json.dumps({"kind": "market", "data": out}, default=str))

        tasks = [asyncio.create_task(forward_events()), asyncio.create_task(market())]
        try:
            await reader()
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            for t in tasks:
                t.cancel()
            engine.subscribers.discard(q)
        return ws

    # The UI is the Next.js static export (web-next/out), served on the same origin as the API.
    # Files are resolved per request, so a fresh `npm run build` is picked up without a restart.
    def export_file(rel: str) -> Path:
        f = (NEXT_OUT / rel).resolve()
        if NEXT_OUT.resolve() not in f.parents or not f.is_file():
            raise web.HTTPNotFound()
        return f

    async def next_asset(req: web.Request) -> web.FileResponse:
        return web.FileResponse(export_file("_next/" + req.match_info["tail"]),
                                headers={"Cache-Control": "public, max-age=31536000, immutable"})  # hashed names

    async def root_asset(req: web.Request) -> web.FileResponse:
        """Icons and small files the export writes at its root (favicon.ico, icon.svg, ...)."""
        return web.FileResponse(export_file(req.match_info["name"]))

    app.add_routes(routes)
    app.router.add_get("/_next/{tail:.+}", next_asset)
    app.router.add_get(r"/{name:[A-Za-z0-9_.-]+\.(?:ico|svg|png|webmanifest|txt)}", root_asset)

    async def on_startup(_: web.Application) -> None:
        await engine.start()

    async def on_cleanup(_: web.Application) -> None:
        await engine.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = load_settings()
    if os.environ.pop("CTB_RESUME_TRADING", None) == "1":
        s.trading_enabled = True  # restart requested by the symbol picker while auto-trade was on
    engine = Engine(s)
    print(f"Calibrain Harness: http://{s.host}:{s.port}  (venue: {s.public_view()['venue_label']}, "
          f"model {s.model_spec.label} ({s.model_spec.model}) key {'set' if s.model_spec.api_key else 'NOT set'}, "
          f"trading {'ON' if s.trading_enabled else 'observe-only'})")
    web.run_app(make_app(engine), host=s.host, port=s.port, print=None)


if __name__ == "__main__":
    main()
