"""Alpaca IEX stocks, spot crypto and indicative options. No invented flow or derivatives.

REST polling avoids consuming the account's single free market-data WebSocket connection.
Closed-bar decisions and the browser WebSocket still use the shared harness engine.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..broker.alpaca import AlpacaPaper, asset_class, timestamp
from .store import Bar, MarketStore, TIMEFRAMES


class AlpacaFeed:
    def __init__(self, store: MarketStore, client: AlpacaPaper):
        self.store, self.client = store, client
        self.on_bar_close = []
        self._stop = asyncio.Event()
        self._sem = asyncio.Semaphore(4)
        self.clock: dict[str, Any] = {"is_open": False}
        self._last_minute = int(time.time() // 60)

    def stop(self):
        self._stop.set()

    def decision_ready(self, symbol: str) -> tuple[bool, str]:
        st = self.store[symbol]
        if asset_class(symbol) != "crypto" and not self.clock.get("is_open"):
            return False, "US market closed; new stock and option decisions are paused"
        if asset_class(symbol) == "option":
            return False, "The free options feed is indicative; use the options workspace for paper limit orders"
        if not st.last_update or time.time() - st.last_update > 60:
            return False, "Alpaca quotes are stale; no forecast or order will use them"
        if len(st.closed_bars("1")) < 60:
            return False, "Waiting for Alpaca historical bars"
        return True, "ready"

    async def history(self, symbol: str, tf: str, *, recent: bool = False):
        now = time.time()
        span = int(tf) * 60 * (4 if recent else 1050) * (1 if asset_class(symbol) == "crypto" else 5)
        async with self._sem:
            data = await self.client.bars(symbol, tf, now - span, now, limit=8 if recent else 1000)
        rows = data.get("bars", {}).get(symbol, [])
        st = self.store[symbol]
        for row in sorted(rows, key=lambda r: r["t"]):
            t = int(timestamp(row["t"]) * 1000)
            bar = Bar(t, row["o"], row["h"], row["l"], row["c"], row["v"],
                      row["v"] * row.get("vw", row["c"]), closed=t + int(tf) * 60000 <= now * 1000)
            closed = st.upsert_bar(tf, bar)
            if recent and closed:
                for fn in self.on_bar_close:
                    await fn(symbol, tf)

    async def warmup(self):
        if not self.client.configured:
            self.store.record_error("alpaca", "Add Alpaca paper API key and secret under KEYS")
            return
        async def one(sym, tf):
            try:
                await self.history(sym, tf)
            except Exception as exc:
                self.store.record_error(f"bars:{sym}:{tf}", str(exc))
        await asyncio.gather(*(one(sym, tf) for sym in self.store.symbols for tf in TIMEFRAMES))
        await self.refresh_quotes()

    async def refresh_quotes(self):
        self.clock = await self.client.clock()
        self.store.context["market_clock"] = self.clock
        universe = []
        for kind in ("us_equity", "crypto", "option"):
            syms = [s for s in self.store.symbols if asset_class(s) == kind]
            if not syms:
                continue
            snapshots = await self.client.snapshots(syms, kind)
            for sym, snap in snapshots.items():
                if sym not in self.store.state:
                    continue
                st = self.store[sym]
                trade, q = snap.get("latestTrade") or {}, snap.get("latestQuote") or {}
                day, prev = snap.get("dailyBar") or snap.get("latestDailyBar") or {}, snap.get("prevDailyBar") or snap.get("previousDailyBar") or {}
                price = trade.get("p") or ((q.get("bp", 0) + q.get("ap", 0)) / 2) or day.get("c")
                if not price:
                    continue
                change = price / prev["c"] - 1 if prev.get("c") else None
                st.ticker = {"lastPrice": price, "bid1Price": q.get("bp"), "ask1Price": q.get("ap"),
                             "price24hPcnt": change, "turnover24h": day.get("v", 0) * day.get("vw", price),
                             "dataFeed": "indicative" if kind == "option" else "iex" if kind == "us_equity" else "alpaca-us"}
                # Same quote source powers paper execution; no second synthetic venue.
                st.testnet_ticker = dict(st.ticker)
                st.last_update = max(timestamp(trade.get("t")), timestamp(q.get("t")))
                st.book_ts = timestamp(q.get("t"))
                scale = 100 if kind == "us_equity" else 1
                st.bids = [(q["bp"], q.get("bs", 0) * scale)] if q.get("bp") else []
                st.asks = [(q["ap"], q.get("as", 0) * scale)] if q.get("ap") else []
                universe.append({"symbol": sym, "last": price, "pct24h": change * 100 if change is not None else None,
                                 "turnover24h": st.ticker["turnover24h"], "oi_value": None, "funding": None})
                self.store.ws_messages += 1
        self.store.context.update(universe=universe, universe_all=universe, universe_ts=time.time())
        self.store.ws_connected = True
        self.store.ws_last_msg = time.time()

    async def poll_loop(self):
        while not self._stop.is_set():
            try:
                if self.client.configured:
                    await self.refresh_quotes()
                    minute = int(time.time() // 60)
                    if minute != self._last_minute:
                        self._last_minute = minute
                        async def refresh(sym, tf):
                            try:
                                await self.history(sym, tf, recent=True)
                            except Exception as exc:
                                self.store.record_error(f"bars:{sym}:{tf}", str(exc))
                        await asyncio.gather(*(refresh(sym, tf) for sym in self.store.symbols for tf in TIMEFRAMES))
            except Exception as exc:
                self.store.ws_connected = False
                self.store.record_error("alpaca", str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass

    async def fetch_1m(self, session, symbol: str, start_ms: int, end_ms: int):
        out, page = {}, None
        while True:
            data = await self.client.bars(symbol, "1", start_ms / 1000, end_ms / 1000, limit=10000,
                                          sort="asc", page_token=page)
            for b in data.get("bars", {}).get(symbol, []):
                t = int(timestamp(b["t"]) * 1000)
                out[t] = (t, b["o"], b["h"], b["l"], b["c"])
            page = data.get("next_page_token")
            if not page:
                break
        return [out[k] for k in sorted(out)]
