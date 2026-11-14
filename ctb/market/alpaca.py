"""Alpaca IEX stocks, spot crypto and indicative options. No invented flow or derivatives.

One shared upstream socket per feed serves every browser tab. REST backfills keep
historical bars and account-clock context available through reconnects.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..broker.alpaca import AlpacaPaper, asset_class, timestamp
from .store import Bar, MarketStore, TIMEFRAMES
from .stream import AlpacaStream


class AlpacaFeed:
    def __init__(self, store: MarketStore, client: AlpacaPaper, model_symbols=None):
        self.store, self.client = store, client
        self.on_bar_close = []
        self._stop = asyncio.Event()
        self._sem = asyncio.Semaphore(4)
        self.clock: dict[str, Any] = {"is_open": False}
        self._last_minute = int(time.time() // 60)
        self._books = {}
        self._depth_ts = {}
        self._trade_ts = {}
        self.streams = []
        self._streams_by_kind = {}
        self._callbacks = set()
        self.model_symbols = list(model_symbols if model_symbols is not None else store.symbols)
        self._watched = {}
        self._history_locks = {}

    def watch_symbol(self, symbol, tf='5'):
        self._watched[(symbol, tf)] = time.time()

    def subscription(self, kind):
        recent = [key[0] for key, _ in sorted(self._watched.items(), key=lambda x: x[1], reverse=True)]
        limit = getattr(self._streams_by_kind.get(kind), 'symbol_limit', None) or 30
        symbols = list(dict.fromkeys(s for s in [*recent, *self.model_symbols, *self.store.symbols] if asset_class(s) == kind))[:limit]
        channels = {'quotes': symbols, 'trades': symbols, 'bars': symbols}
        if kind == 'crypto':
            channels['orderbooks'] = symbols
        return channels

    async def ensure_history(self, symbol, tf):
        self.watch_symbol(symbol, tf)
        lock = self._history_locks.setdefault((symbol, tf), asyncio.Lock())
        async with lock:
            if not self.store[symbol].bars[tf]:
                await self.history(symbol, tf)

    def stop(self):
        self._stop.set()
        for stream in self.streams:
            stream.stop()

    def stream_status(self, kind, state, error):
        statuses = self.store.context.setdefault('market_streams', {})
        statuses[kind] = {'state': state, 'error': error, 'changed_at': time.time()}
        self.store.ws_connected = any(s['state'] == 'live' for s in statuses.values())
        if state in ('connecting', 'reconnecting', 'stopped'):
            for sym in self.store.symbols:
                if asset_class(sym) == kind:
                    if self._books.pop(sym, None):
                        self.store[sym].bids, self.store[sym].asks = [], []
                        self.store[sym].book_ts = 0
                    self._depth_ts.pop(sym, None)
        if error:
            self.store.record_error('alpaca:stream:' + kind, error)

    def book_info(self, symbol):
        kind = asset_class(symbol)
        status = self.store.context.get('market_streams', {}).get(kind, {})
        depth = symbol in self._books
        return {'source': 'Alpaca US' if kind == 'crypto' else 'IEX' if kind == 'us_equity' else 'INDICATIVE',
                'depth': depth, 'state': status.get('state', 'polling') if symbol in self.subscription(kind)['quotes'] else 'polling'}

    async def stream_message(self, message):
        sym, kind = message.get('S'), message.get('T')
        if sym not in self.store.state:
            return
        st = self.store[sym]
        ts = timestamp(message.get('t'))
        if kind == 'o':
            if message.get('r'):
                self._books[sym] = ({}, {})
            if sym not in self._books or (not message.get('r') and ts < self._depth_ts.get(sym, 0)):
                return
            bids, asks = self._books[sym]
            for levels, field in ((bids, 'b'), (asks, 'a')):
                for row in message.get(field, []):
                    if row['s'] == 0:
                        levels.pop(row['p'], None)
                    else:
                        levels[row['p']] = row['s']
            st.bids, st.asks = sorted(bids.items(), reverse=True), sorted(asks.items())
            st.book_ts = ts
            self._depth_ts[sym] = ts
            if st.bids and st.asks:
                st.ticker.update(bid1Price=st.bids[0][0], ask1Price=st.asks[0][0])
        elif kind == 'q':
            if ts < st.book_ts:
                return
            st.ticker.update(bid1Price=message.get('bp'), ask1Price=message.get('ap'))
            if sym not in self._books:
                scale = 100 if asset_class(sym) == 'us_equity' else 1
                st.bids = [(message['bp'], message.get('bs', 0) * scale)] if message.get('bp') else []
                st.asks = [(message['ap'], message.get('as', 0) * scale)] if message.get('ap') else []
                st.book_ts = ts
        elif kind == 't' and ts >= self._trade_ts.get(sym, 0):
            st.ticker['lastPrice'] = message['p']
            self._trade_ts[sym] = ts
        elif kind in ('b', 'u'):
            # Official minute aggregates; higher timeframes retain the REST backfill.
            bar = Bar(int(ts * 1000), message['o'], message['h'], message['l'], message['c'],
                      message['v'], message['v'] * message.get('vw', message['c']), closed=True)
            closed = st.upsert_bar('1', bar)
            if closed:
                for fn in self.on_bar_close:
                    task = asyncio.create_task(fn(sym, '1'))
                    self._callbacks.add(task)
                    def finished(task):
                        self._callbacks.discard(task)
                        if not task.cancelled() and task.exception():
                            self.store.record_error('alpaca:bar_callback', type(task.exception()).__name__)
                    task.add_done_callback(finished)
        st.last_update = max(st.last_update, ts)
        st.testnet_ticker = dict(st.ticker)
        self.store.ws_messages += 1
        self.store.ws_last_msg = time.time()

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
        await asyncio.gather(*(one(sym, tf) for sym in self.model_symbols for tf in TIMEFRAMES))
        await self.refresh_quotes()

    async def refresh_quotes(self):
        self.clock = await self.client.clock()
        self.store.context["market_clock"] = self.clock
        universe = []
        for kind in ("us_equity", "crypto", "option"):
            syms = [s for s in self.store.symbols if asset_class(s) == kind]
            if not syms:
                continue
            snapshots = {}
            for offset in range(0, len(syms), 100):
                snapshots.update(await self.client.snapshots(syms[offset:offset + 100], kind))
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
                if timestamp(trade.get('t')) < self._trade_ts.get(sym, 0):
                    price = st.ticker.get('lastPrice') or price
                else:
                    self._trade_ts[sym] = timestamp(trade.get('t'))
                st.ticker = {**st.ticker, "lastPrice": price,
                             "price24hPcnt": change, "turnover24h": day.get("v", 0) * day.get("vw", price),
                             "dataFeed": "indicative" if kind == "option" else "iex" if kind == "us_equity" else "alpaca-us"}
                if timestamp(q.get('t')) >= st.book_ts and sym not in self._books:
                    st.ticker.update(bid1Price=q.get('bp'), ask1Price=q.get('ap'))
                    st.book_ts = timestamp(q.get('t'))
                    scale = 100 if kind == 'us_equity' else 1
                    st.bids = [(q['bp'], q.get('bs', 0) * scale)] if q.get('bp') else []
                    st.asks = [(q['ap'], q.get('as', 0) * scale)] if q.get('ap') else []
                # Same quote source powers paper execution; no second synthetic venue.
                st.testnet_ticker = dict(st.ticker)
                st.last_update = max(st.last_update, timestamp(trade.get("t")), timestamp(q.get("t")))
                universe.append({"symbol": sym, "last": price, "pct24h": change * 100 if change is not None else None,
                                 "turnover24h": st.ticker["turnover24h"], "oi_value": None, "funding": None})
                self.store.ws_messages += 1
        self.store.context.update(universe=universe, universe_all=universe, universe_ts=time.time())

    async def poll_loop(self):
        tasks = [asyncio.create_task(self.backfill_loop())]
        for kind, path in [('crypto', 'v1beta3/crypto/us'), ('us_equity', 'v2/iex')]:
            symbols = [s for s in self.store.symbols if asset_class(s) == kind]
            if not symbols:
                continue
            stream = AlpacaStream(self.client, 'wss://stream.data.alpaca.markets/' + path,
                                  lambda kind=kind: self.subscription(kind), self.stream_message,
                                  lambda state, error, kind=kind: self.stream_status(kind, state, error))
            self.streams.append(stream)
            self._streams_by_kind[kind] = stream
            tasks.append(asyncio.create_task(stream.run()))
        try:
            await asyncio.gather(*tasks)
        finally:
            tasks.extend(self._callbacks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def backfill_loop(self):
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
                        targets = {(sym, tf) for sym in self.model_symbols for tf in TIMEFRAMES}
                        targets.update(key for key, seen in self._watched.items() if time.time() - seen < 600)
                        await asyncio.gather(*(refresh(sym, tf) for sym, tf in targets))
            except Exception as exc:
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
