"""Paper execution with durable order ids and strict ownership of bot positions.

No cancel-all or close-all endpoint is used. A foreign position/order on a symbol blocks entry.
Stop-limit backstops remain on Alpaca; managed targets/time exits run in this service.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Any

import aiohttp

from .alpaca import AlpacaError, AlpacaPaper, TERMINAL, asset_class, timestamp, iso
from .executor import Executor, adverse_bp
from ..policy import risk_pct_for


class AlpacaExecutor(Executor):
    def __init__(self, s, db, client: AlpacaPaper, emit, feed):
        super().__init__(s, db, client, emit)
        self.feed = feed
        self.account_id = ""
        self.account: dict = {}
        self.raw_positions = []
        self.clock: dict = {}
        self.kill_requested = False
        from .manual import ManualTrading
        self.manual = ManualTrading(self)

    def owned(self, symbol: str | None = None):
        return self.db.query("SELECT * FROM trades WHERE venue='alpaca' AND account_id=? "
                             "AND status IN ('opening','open','closing')" + (" AND symbol=?" if symbol else ""),
                             [self.account_id] + ([symbol] if symbol else []))

    async def start(self):
        await self.sync()
        if self.sync_error:
            raise RuntimeError(self.sync_error)
        self.instruments = {sym: await self.client.asset(sym) for sym in self.s.symbols}

    async def sync(self):
        async with self._sync_lock:
            try:
                a, positions, orders, clock = await asyncio.gather(self.client.account(), self.client.positions(),
                                                                  self.client.orders(), self.client.clock())
                expected = self.s.alpaca_account_id
                if expected and a['id'] != expected:
                    raise ValueError('Alpaca account does not match the configured harness paper account')
                self.account_id, self.account, self.clock = a['id'], a, clock
                self.raw_positions = positions
                self.wallet = {"equity": float(a['equity']), "available": max(0.0, float(a['buying_power'])),
                               "cash": float(a['cash']), "unrealized_pnl": sum(float(p['unrealized_pl']) for p in positions),
                               "currency": "USD"}
                self.positions = [{"symbol": p['symbol'], "side": "Buy" if p['side'] == 'long' else "Sell",
                                   "size": abs(float(p['qty'])), "avgPrice": float(p['avg_entry_price']),
                                   "markPrice": float(p['current_price']), "unrealisedPnl": float(p['unrealized_pl']),
                                   "positionValue": abs(float(p['market_value']))} for p in positions]
                # Alpaca position symbols may omit crypto's slash.
                for p in self.positions:
                    p['symbol'] = next((s for s in self.s.symbols if s.replace('/', '') == p['symbol'].replace('/', '')), p['symbol'])
                self.open_orders, self.last_sync, self.sync_error = orders, time.time(), None
            except Exception as exc:
                self.sync_error = str(exc)

    def _order_symbol(self, order):
        return str(order.get('symbol', '')).replace('/', '')

    def manual_pending(self, symbol):
        if self.db.one('SELECT resource FROM manual_ownership WHERE account_id=? AND resource=? LIMIT 1',
                       [self.account_id, symbol.replace('/', '')]):
            return True
        return any(self._order_symbol(r) == symbol.replace('/', '') for r in self.db.query(
            "SELECT symbol FROM manual_orders WHERE account_id=? AND attempted_ts IS NOT NULL "
            "AND status NOT IN ('filled','canceled','expired','rejected','replaced')", [self.account_id]))

    def ownership_ok(self, t, position) -> bool:
        if not position:
            return False
        return (position['side'] == ('Buy' if t['side'] == 'long' else 'Sell')
                and math.isclose(float(position['size']), float(t['qty']), rel_tol=1e-6, abs_tol=1e-8))

    async def open(self, *, symbol, side, sl_bp, tp_bp, decision_id, ev_r, exploration=False,
                   decision_price=None, horizon_min=None, vol_state=None, regime=None, events=None):
        async with self._entry_lock:
            await self.sync()
            reason = self.check_guards(symbol)
            if self.manual_pending(symbol):
                return {"ok": False, "reason": "A manual paper order on this symbol is pending reconciliation"}
            kind = asset_class(symbol)
            if reason:
                return {"ok": False, "reason": reason}
            if self.account.get('trading_blocked') or self.account.get('account_blocked'):
                return {"ok": False, "reason": "Alpaca account is blocked"}
            if kind != 'crypto' and not self.clock.get('is_open'):
                return {"ok": False, "reason": "US market closed"}
            if kind == 'crypto' and side == 'short':
                return {"ok": False, "reason": "Alpaca crypto is spot-only; short opening is unavailable"}
            if kind == 'option':
                return {"ok": False, "reason": "Options use explicit paper limit orders in the options workspace; indicative quotes do not drive auto-trading"}
            asset = self.instruments.get(symbol) or await self.client.asset(symbol)
            if not asset.get('tradable') or (side == 'short' and not asset.get('shortable')):
                return {"ok": False, "reason": "Asset not tradable/shortable on this Alpaca account"}
            if any(self._order_symbol(o) == symbol.replace('/', '') for o in self.open_orders):
                return {"ok": False, "reason": "Existing order on symbol; ownership cannot be shared"}
            st = self.feed.store[symbol]
            if not st.last_update or time.time() - st.last_update > 30:
                return {"ok": False, "reason": "Quote is stale"}
            buy = side == 'long'
            q = st.ticker
            ref = float(q.get('ask1Price' if buy else 'bid1Price') or 0)
            if not ref or sl_bp <= 0:
                return {"ok": False, "reason": "Executable quote or stop distance unavailable"}
            eq = self.wallet['equity']
            same_side = [t['symbol'] for t in self.owned() if t['side'] == side]
            correlations = list(self.corr_fn(symbol, same_side).values()) if self.corr_fn and same_side else []
            risk_pct, sizing = risk_pct_for(self.s, exploration=exploration, ev_r=ev_r, vol_state=vol_state,
                                           run_drawdown_pct=self.run_drawdown_pct(), same_side_corr=correlations)
            risk = eq * risk_pct / 100
            open_risk = sum(float(t.get('risk_usd') or 0) for t in self.owned())
            risk = min(risk, max(0, eq * self.s.max_open_risk_pct / 100 - open_risk))
            if sum(t['side'] == side for t in self.owned()) >= self.s.max_same_side:
                return {"ok": False, "reason": "Same-side position cap"}
            available = max(0.0, float(self.account['cash'])) if kind == 'crypto' else self.wallet['available']
            notional = min(risk / (sl_bp / 10000), self.s.max_notional_usdt, available * .9, eq * self.s.max_leverage * .9)
            step = float(asset.get('min_trade_increment') or 1e-8) if kind == 'crypto' else 1.0
            dec_step = Decimal(str(step))
            qty = float((Decimal(str(notional)) / Decimal(str(ref)) / dec_step).to_integral_value(rounding=ROUND_DOWN) * dec_step)
            if qty <= 0 or qty < float(asset.get('min_order_size') or step):
                return {"ok": False, "reason": "Risk budget below minimum order size"}
            maker = self.s.entry_mode == 'maker'
            price = float(q.get('bid1Price' if buy else 'ask1Price') or ref) if maker else ref * (1 + (1 if buy else -1) * self.s.max_entry_slip_bp / 10000)
            price = round(price, 2 if price >= 1 else 4)
            link = 'ctba-' + uuid.uuid4().hex[:28]
            t_id = self.db.insert('trades', {"venue": "alpaca", "account_id": self.account_id, "multiplier": 1,
                "symbol": symbol, "side": side, "decision_id": decision_id, "entry_link_id": link, "status": "opening",
                "opened_ts": time.time(), "deadline_ts": time.time() + (horizon_min or (self.s.horizon_bars * self.s.decision_tf_minutes)) * 60,
                "requested_qty": qty, "ref_price": ref, "cap_price": price, "decision_price": decision_price,
                "planned_sl_bp": sl_bp, "planned_tp_bp": tp_bp, "ev_r": ev_r,
                "risk_usd": qty * ref * sl_bp / 10000, "sizing_json": sizing,
                "run_id": self.run_id_fn(), "mode": "exploration" if exploration else "strict",
                "entry_mode": "maker" if maker else "taker", "exit_mode": "managed", "horizon_min": horizon_min,
                "regime": regime, "events_json": events or []})
            body = {"symbol": symbol, "qty": format(Decimal(str(qty)), 'f'), "side": "buy" if buy else "sell", "type": "limit",
                    "limit_price": str(price), "time_in_force": "gtc" if kind == 'crypto' else "day", "client_order_id": link}
            try:
                order = await self.client.submit(body)
                until = time.time() + (self.s.maker_wait_s if maker else 5)
                while order['status'] not in TERMINAL and time.time() < until:
                    await asyncio.sleep(.5)
                    order = await self.client.order(link)
                if order['status'] not in TERMINAL:
                    await self.client.cancel(order)
                    # Cancellation is asynchronous. Never assume unfilled shares cannot still fill.
                    for _ in range(20):
                        order = await self.client.order(link)
                        if order['status'] in TERMINAL:
                            break
                        await asyncio.sleep(.25)
                if order['status'] not in TERMINAL:
                    return {"ok": False, "reason": "Entry cancellation pending; reconciliation will resolve it"}
                await self.resolve_entry(self.db.one('SELECT * FROM trades WHERE id=?', [t_id]), order)
                await self.sync()
                return {"ok": float(order.get('filled_qty') or 0) > 0, "trade_id": t_id, "status": order['status']}
            except AlpacaError as exc:
                if exc.status >= 500:
                    return {"ok": False, "reason": "Ambiguous broker error; entry retained for reconciliation"}
                self.db.update('trades', t_id, {"status": "error", "notes": str(exc)})
                return {"ok": False, "reason": str(exc)}
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return {"ok": False, "reason": "Ambiguous entry response; retained durable id for reconciliation"}

    async def resolve_entry(self, t, order):
        if order['status'] not in TERMINAL:
            return
        qty = float(order.get('filled_qty') or 0)
        if qty <= 0:
            self.db.update('trades', t['id'], {"status": "unfilled", "entry_order_id": order['id']})
            return
        entry = float(order['filled_avg_price'])
        gross_qty, entry_fee = qty, None
        if asset_class(t['symbol']) == 'crypto' and t['side'] == 'long':
            await self.sync()
            position = self.position(t['symbol'])
            if not position or position['side'] != 'Buy':
                raise RuntimeError('Filled crypto entry has no credited long position yet')
            credited = float(position['size'])
            asset = self.instruments.get(t['symbol']) or await self.client.asset(t['symbol'])
            step = float(asset.get('min_trade_increment') or 1e-9)
            if not (0 <= gross_qty - credited <= gross_qty * .0025 + 2 * step):
                raise RuntimeError('Credited crypto quantity differs by more than the tier-1 fee and rounding')
            activities = await self.client.request('GET', '/v2/account/activities',
                params={'after': iso(t['opened_ts']),
                        'activity_types': 'FILL', 'direction': 'asc', 'page_size': 100})
            if len(activities) >= 100:
                raise RuntimeError('Too many account fills to prove exclusive entry ownership')
            if any(a.get('symbol', '').replace('/', '') == t['symbol'].replace('/', '')
                   and a.get('order_id') != order['id'] for a in activities):
                raise RuntimeError('Another order changed this crypto symbol during entry')
            entry_fee = (gross_qty - credited) * entry
            qty = credited
        sign = 1 if t['side'] == 'long' else -1
        sl = entry * (1 - sign * t['planned_sl_bp'] / 10000)
        tp = entry * (1 + sign * t['planned_tp_bp'] / 10000)
        self.db.update('trades', t['id'], {"status": "open", "qty": qty, "gross_qty": gross_qty, "entry_fee": entry_fee,
            "entry_price": entry, "entry_order_id": order['id'],
            "notional": qty * entry, "sl_price": sl, "tp_price": tp, "risk_usd": qty * abs(entry - sl),
            "entry_slip_bp": adverse_bp(t['side'], entry, t['ref_price'], entry=True),
            "protection": "managed stop/target; Alpaca stop-limit backstop pending"})
        t = self.db.one('SELECT * FROM trades WHERE id=?', [t['id']])
        if asset_class(t['symbol']) == 'option':
            self.db.update('trades', t['id'], {'risk_usd': qty * entry * (t.get('multiplier') or 100),
                'notional': qty * entry * (t.get('multiplier') or 100),
                'protection': 'Long option: full premium at risk; scheduled exit before market close'})
        else:
            await self.arm_backstop(t)

    async def arm_backstop(self, t):
        if t.get('stop_order_id'):
            return
        buy = t['side'] == 'short'
        stop = t['entry_price'] + (t['sl_price'] - t['entry_price']) * self.s.backstop_mult
        limit = stop * (1.03 if buy else .97)
        link = f"{t['entry_link_id']}-stop"
        try:
            try:
                order = await self.client.order(link)
            except AlpacaError as exc:
                if exc.status != 404:
                    raise
                order = await self.client.submit({"symbol": t['symbol'], "qty": format(Decimal(str(t['qty'])), 'f'),
                    "side": "buy" if buy else "sell", "type": "stop_limit", "stop_price": str(round(stop, 2)),
                    "limit_price": str(round(limit, 2)), "time_in_force": "gtc", "client_order_id": link})
            self.db.update('trades', t['id'], {"stop_order_id": order['id'], "backstop_price": stop,
                "protection": "Alpaca stop-limit backstop; managed stop/target/time exit"})
        except Exception as exc:
            self.halted_reason = f"Backstop failed for {t['symbol']}: {exc}"
            self.db.event('error', 'protection', self.halted_reason, t['symbol'])
            await self.sync()
            await self.close(t['symbol'], 'protection_failed')

    async def exit_orders(self, t):
        links = t.get('exit_links_json') or ([t['exit_link_id']] if t.get('exit_link_id') else [])
        links = list(dict.fromkeys([*links, f"{t['entry_link_id']}-stop"]))
        orders = []
        for link in links:
            try:
                orders.append(await self.client.order(link))
            except AlpacaError as exc:
                if exc.status != 404:
                    raise
        return orders

    async def _submit_exit(self, t, qty, *, new=False):
        links = list(t.get('exit_links_json') or ([t['exit_link_id']] if t.get('exit_link_id') else []))
        if new or not links:
            links.append(f"{t['entry_link_id']}-x{len(links) + 1}")
        link = links[-1]
        self.db.update('trades', t['id'], {'status': 'closing', 'exit_link_id': link,
            'exit_links_json': links, 'exit_attempt_ts': time.time()})
        try:
            await self.client.submit({'symbol': t['symbol'], 'qty': format(Decimal(str(qty)), 'f'),
                'side': 'sell' if t['side'] == 'long' else 'buy', 'type': 'market',
                'time_in_force': 'gtc' if asset_class(t['symbol']) == 'crypto' else 'day', 'client_order_id': link,
                **({'position_intent': 'sell_to_close'} if asset_class(t['symbol']) == 'option' else {})})
        except Exception as exc:
            # Retain the exact client id even when the HTTP result is ambiguous.
            self.db.event('warn', 'exit_pending', str(exc)[:300], t['symbol'])

    async def continue_close(self, t):
        orders = await self.exit_orders(t)
        filled = sum(float(o.get('filled_qty') or 0) for o in orders)
        remaining = max(0.0, float(t['qty']) - filled)
        if remaining <= float(t['qty']) * 1e-6:
            return await self.record_close(t, orders=orders)
        if any(o['status'] not in TERMINAL for o in orders):
            return False
        await self.sync()
        position = self.position(t['symbol'])
        expected = {**t, 'qty': remaining}
        if not self.ownership_ok(expected, position):
            self.halted_reason = f"External change to {t['symbol']}; closing paused to preserve other positions"
            return False
        if asset_class(t['symbol']) != 'crypto' and not self.clock.get('is_open'):
            return False
        if time.time() - (t.get('exit_attempt_ts') or 0) < 15:
            return False
        links = t.get('exit_links_json') or []
        # A terminal partial fill gets a new id for the remaining quantity. A missing response
        # is retried with the SAME id, which Alpaca rejects if it already accepted the order.
        last_found = any(o.get('client_order_id') == t.get('exit_link_id') for o in orders)
        await self._submit_exit(t, remaining, new=bool(links and last_found))
        return False

    async def close(self, symbol, reason, *, locked=False):
        async with self.lock(symbol):
            trades = self.owned(symbol)
            if len(trades) != 1:
                return False
            t = trades[0]
            if t['status'] == 'opening':
                try:
                    order = await self.client.order(t['entry_link_id'])
                    if order['status'] not in TERMINAL:
                        await self.client.cancel(order)
                except AlpacaError as exc:
                    if exc.status != 404:
                        raise
                return False
            if t['status'] == 'closing':
                return await self.continue_close(t)
            await self.sync()
            if asset_class(symbol) != 'crypto' and not self.clock.get('is_open'):
                return False  # keep protection; do not replace it with an order queued overnight
            exits = await self.exit_orders(t)
            already_filled = sum(float(o.get('filled_qty') or 0) for o in exits)
            position = self.position(symbol)
            if not position:
                return await self.record_close(t, orders=exits)
            if not self.ownership_ok({**t, 'qty': t['qty'] - already_filled}, position):
                self.halted_reason = f"External change to {symbol}; automatic closing disabled to preserve other positions"
                return False
            stop = next((o for o in exits if o.get('client_order_id') == f"{t['entry_link_id']}-stop"), None)
            if stop and stop['status'] not in TERMINAL:
                await self.client.cancel(stop)
                for _ in range(20):
                    stop = await self.client.order(f"{t['entry_link_id']}-stop")
                    if stop['status'] in TERMINAL:
                        break
                    await asyncio.sleep(.25)
                if stop['status'] not in TERMINAL:
                    return False
            self.db.update('trades', t['id'], {'status': 'closing', 'close_reason': reason})
            t = self.db.one('SELECT * FROM trades WHERE id=?', [t['id']])
            return await self.continue_close(t)

    async def record_close(self, t, *, orders=None, **kwargs):
        exits = orders if orders is not None else await self.exit_orders(t)
        exits = [o for o in exits if float(o.get('filled_qty') or 0)]
        qty = sum(float(o['filled_qty']) for o in exits)
        if qty < float(t.get('qty') or 0) * (1 - 1e-6) or not qty:
            return False
        if qty > float(t['qty']) * (1 + 1e-6):
            self.halted_reason = f"Exit quantity mismatch on {t['symbol']}"
            return False
        px = sum(float(o['filled_qty']) * float(o['filled_avg_price']) for o in exits) / qty
        gross = (px - t['entry_price']) * qty * (1 if t['side'] == 'long' else -1) * (t.get('multiplier') or 1)
        self.db.update('trades', t['id'], {'status': 'closed', 'closed_ts': max(timestamp(o.get('filled_at')) for o in exits),
            'exit_price': px, 'realized_pnl': gross, 'fees': None, 'r_mult': gross / t['risk_usd'] if t.get('risk_usd') else None,
            'close_reason': t.get('close_reason') or 'backstop', 'fills_json': exits,
            'close_notes': 'Fill P&L from Alpaca order records. Per-trade fees not reported; this is gross of any fees.'})
        return True

    async def reconcile_loop(self):
        while True:
            try:
                await self.sync()
                if not self.sync_error:
                    try:
                        await self.manual.refresh()
                    except Exception as exc:
                        self.db.event('warn', 'manual_reconcile', type(exc).__name__)
                if self.kill_requested:
                    await self.kill(self.halted_reason or 'kill requested')
                if self.s.kill_file.exists():
                    await self.kill('kill file')
                for t in self.owned():
                    if t['status'] == 'opening':
                        order = await self.client.order(t['entry_link_id'])
                        if order['status'] not in TERMINAL and (t['deadline_ts'] <= time.time() or
                                (asset_class(t['symbol']) != 'option' and time.time() - t['opened_ts'] > self.s.maker_wait_s + 10)):
                            await self.client.cancel(order)
                        await self.resolve_entry(t, order)
                    elif t['status'] == 'closing':
                        await self.continue_close(t)
                    elif not self.position(t['symbol']):
                        await self.record_close(t)
                    elif t['deadline_ts'] <= time.time():
                        await self.close(t['symbol'], 'time_stop')
                    else:
                        if not t.get('stop_order_id') and asset_class(t['symbol']) != 'option':
                            await self.arm_backstop(t)
                        await self.check_stop(t['symbol'])
                self.emit('account', self.account_view())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.sync_error = str(exc)
                self.db.event('error', 'alpaca_reconcile', str(exc)[:300])
            await asyncio.sleep(5)

    async def check_stop(self, symbol):
        trades = self.owned(symbol)
        if len(trades) != 1 or trades[0]['status'] != 'open':
            return
        if asset_class(symbol) == 'option' or symbol not in self.feed.store.state:
            return
        t, st = trades[0], self.feed.store[symbol]
        if time.time() - st.last_update > 30:
            return
        px = float(st.ticker.get('bid1Price' if t['side'] == 'long' else 'ask1Price') or 0)
        if px:
            sign = 1 if t['side'] == 'long' else -1
            original_distance = t['entry_price'] * t['planned_sl_bp'] / 10000
            progress = sign * (px - t['entry_price']) / original_distance if original_distance > 0 else 0
            if progress > (t.get('mfe_r') or 0):
                self.db.update('trades', t['id'], {'mfe_r': progress})
            if self.s.be_trigger_r > 0 and not t.get('be_moved_ts') and progress >= self.s.be_trigger_r:
                t['sl_price'] = t['entry_price'] * (1 + sign * self.s.be_offset_bp / 10000)
                self.db.update('trades', t['id'], {'sl_price': t['sl_price'], 'be_moved_ts': time.time()})
            if sign * (px - t['sl_price']) <= 0:
                await self.close(symbol, 'stop_loss')
            elif sign * (px - t['tp_price']) >= 0:
                await self.close(symbol, 'take_profit')

    async def kill(self, reason):
        self.s.trading_enabled = False
        self.kill_requested = True
        self.halted_reason = reason
        for t in self.owned():
            await self.close(t['symbol'], 'kill')

    def daily_pnl(self):
        equity = float(self.account.get('equity') or 0)
        previous = float(self.account.get('last_equity') or equity)
        return equity - previous

    def account_view(self):
        view = super().account_view()
        owned = {t['symbol']: t for t in self.owned()}
        for p in view['positions']:
            t = owned.get(p['symbol'])
            p.update(owned=bool(t), sl=t.get('sl_price') if t else None, tp=t.get('tp_price') if t else None)
            raw = next((r for r in self.raw_positions if r['symbol'].replace('/', '') == p['symbol'].replace('/', '')), {})
            p['available_qty'] = str(abs(Decimal(str(raw.get('qty_available', raw.get('qty', p['size']))))))
        return {**view, "venue": "alpaca", "account_id": self.account_id, "currency": "USD", "paper": True,
                "options_trading_level": self.account.get('options_trading_level'), "market_clock": self.clock,
                "pnl_basis": "gross broker fills; fees unavailable"}

    async def option_entry(self, symbol: str, qty: int, limit_price: float):
        if asset_class(symbol) != 'option' or type(qty) is not int or not 1 <= qty <= 100:
            raise ValueError('Choose an option contract and a whole quantity from 1 to 100')
        if not math.isfinite(limit_price) or limit_price <= 0:
            raise ValueError('A positive, finite limit premium is required')
        async with self._entry_lock:
            await self.sync()
            if self.sync_error or not self.clock.get('is_open'):
                raise ValueError('Options orders require a connected account and an open US market')
            if int(self.account.get('options_trading_level') or 0) < 2:
                raise ValueError('Buying calls/puts requires Alpaca options Level 2 or higher')
            if self.halted_reason or self.s.kill_file.exists() or self.account.get('trading_blocked'):
                raise ValueError('The paper executor is halted')
            if self.position(symbol) or self.owned(symbol) or self.manual_pending(symbol) or any(self._order_symbol(o) == symbol for o in self.open_orders):
                raise ValueError('A position or order already exists on this contract')
            contract = await self.client.asset(symbol)
            if not contract.get('tradable'):
                raise ValueError('Contract is not tradable')
            multiplier = float(contract.get('size') or 100)
            premium = qty * multiplier * limit_price
            risk_limit = min(self.s.max_notional_usdt, self.wallet['equity'] * self.s.risk_per_trade_pct / 100)
            if premium > risk_limit or premium > float(self.account.get('options_buying_power') or 0):
                raise ValueError(f'Total option premium exceeds the ${risk_limit:.2f} paper risk cap')
            # Long premium is the full risk budget. Exit before today\'s close to avoid exercise.
            deadline = timestamp(self.clock['next_close']) - 15 * 60
            if deadline <= time.time():
                raise ValueError('No new option positions in the final 15 minutes of the session')
            link = 'ctba-' + uuid.uuid4().hex[:28]
            tid = self.db.insert('trades', {'venue': 'alpaca', 'account_id': self.account_id, 'multiplier': multiplier,
                'symbol': symbol, 'side': 'long', 'status': 'opening', 'entry_link_id': link,
                'opened_ts': time.time(), 'deadline_ts': deadline, 'requested_qty': qty,
                'planned_sl_bp': 10000, 'planned_tp_bp': 10000, 'ref_price': limit_price, 'risk_usd': premium,
                'entry_mode': 'limit', 'mode': 'manual_option', 'run_id': self.run_id_fn(),
                'notes': 'Explicit paper long option; entire debit at risk; free indicative market data'})
            order = await self.client.submit({'symbol': symbol, 'qty': str(qty), 'side': 'buy', 'type': 'limit',
                'limit_price': str(round(limit_price, 2)), 'time_in_force': 'day', 'position_intent': 'buy_to_open',
                'client_order_id': link})
            self.db.update('trades', tid, {'entry_order_id': order['id']})
            return {'ok': True, 'trade_id': tid, 'order_id': order['id'], 'status': order['status'], 'paper': True}
