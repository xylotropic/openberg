"""Explicit paper orders. An immutable review is persisted before its one broker write."""
from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal, InvalidOperation

import aiohttp

from .alpaca import AlpacaError, asset_class, timestamp

FINAL = {'filled', 'canceled', 'expired', 'rejected', 'replaced'}


def number(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'{name} must be a positive number') from None
    if not result.is_finite() or not 0 < result <= Decimal('1000000000'):
        raise ValueError(f'{name} must be a positive, finite number')
    return result


def normalized(symbol):
    return symbol.replace('/', '')


class ManualTrading:
    def __init__(self, executor):
        self.ex = executor
        self.client, self.db, self.s = executor.client, executor.db, executor.s

    async def account_ready(self):
        await self.ex.sync()
        if self.ex.sync_error or not self.s.alpaca_account_id or self.ex.account_id != self.s.alpaca_account_id:
            raise ValueError('A connected, pinned Alpaca paper account is required')

    async def validate(self, values, *, sending=False, ignore_id=None):
        ex = self.ex
        await self.account_ready()
        if ex.account.get('trading_blocked') or ex.account.get('account_blocked'):
            raise ValueError('Alpaca account is blocked')
        symbol = str(values.get('symbol', '')).strip().upper()
        side, order_type = values.get('side'), values.get('type')
        if side not in ('buy', 'sell') or order_type not in ('market', 'limit'):
            raise ValueError('Choose BUY or SELL and MARKET or LIMIT')
        if not symbol or len(symbol) > 32:
            raise ValueError('Choose a tradable symbol')
        kind = asset_class(symbol)
        if kind == 'crypto' and not symbol.endswith('/USD'):
            raise ValueError('Manual crypto orders currently support USD-quoted pairs')
        qty = number(values.get('qty'), 'Quantity')
        limit = number(values.get('limit_price'), 'Limit price') if order_type == 'limit' else None
        tif = values.get('time_in_force') or ('gtc' if kind == 'crypto' else 'day')
        if tif not in (('gtc', 'ioc') if kind == 'crypto' else ('day',)):
            raise ValueError('Use GTC / IOC for crypto or DAY for stocks and options')
        asset = await self.client.asset(symbol)
        resources = [normalized(symbol)]
        if kind == 'option':
            resources.append(normalized(asset.get('underlying_symbol') or symbol[:-15]))
        if not asset.get('tradable'):
            raise ValueError('Asset is not tradable on this paper account')
        if asset.get('class') == 'crypto' and kind != 'crypto':
            raise ValueError('Use a crypto pair such as BTC/USD')
        if any(normalized(t['symbol']) in resources or (asset_class(t['symbol']) == 'option' and t['symbol'][:-15] in resources) for t in ex.owned()):
            raise ValueError('A model trade owns this symbol. Close it from Positions before placing a manual order')
        if any(normalized(o.get('symbol', '')) in resources for o in ex.open_orders):
            raise ValueError('An open order already exists on this symbol. Cancel or wait for it first')
        unresolved = self.db.query("SELECT id,symbol FROM manual_orders WHERE account_id=? AND attempted_ts IS NOT NULL "
                                   "AND status NOT IN ('filled','canceled','expired','rejected','replaced')", [ex.account_id])
        if any(r['id'] != ignore_id and normalized(r['symbol']) == normalized(symbol) for r in unresolved):
            raise ValueError('A previous manual order on this symbol is still pending reconciliation')
        unseen = self.db.query("SELECT symbol FROM manual_orders WHERE account_id=? AND attempted_ts IS NOT NULL "
                               "AND inventory_seen_ts IS NULL AND status IN ('filled','canceled','expired','rejected','replaced')", [ex.account_id])
        if any(normalized(r['symbol']) == normalized(symbol) for r in unseen):
            raise ValueError('The previous fill is still reconciling with available inventory')
        positions = getattr(ex, 'raw_positions', [])
        position = next((p for p in positions if normalized(p['symbol']) == normalized(symbol)), None)
        held = Decimal(str(position['qty'])) if position else Decimal(0)
        reducing = bool(held > 0 and side == 'sell' or held < 0 and side == 'buy')
        available = abs(Decimal(str(position.get('qty_available', position['qty'])))) if position else Decimal(0)
        if reducing and qty > available:
            raise ValueError('Quantity exceeds the available position. Close it before opening the opposite side')
        if kind in ('crypto', 'option') and side == 'sell' and not (held > 0 and reducing):
            raise ValueError('Selling crypto or options requires an existing long position; short opening is unavailable')
        if kind == 'us_equity' and side == 'sell' and not reducing and not asset.get('shortable'):
            raise ValueError('This equity cannot be sold short on this account')
        step = Decimal(str(asset.get('min_trade_increment') or '0.00000001')) if kind == 'crypto' else Decimal('0.000000001') if asset.get('fractionable') else Decimal(1)
        if kind == 'option':
            step = Decimal(1)
            if order_type != 'limit' or (not reducing and qty > 100):
                raise ValueError('Options require a limit premium and 1–100 whole contracts')
            if not reducing and int(ex.account.get('options_trading_level') or 0) < 2:
                raise ValueError('Long calls / puts require Alpaca options Level 2 or higher')
        full_close = reducing and qty == available
        if qty % step and not (kind == 'crypto' and full_close):
            raise ValueError(f'Quantity must use increments of {step}')
        if kind == 'crypto' and not full_close and qty < Decimal(str(asset.get('min_order_size') or step)):
            raise ValueError('Quantity is below the crypto minimum order size')
        if limit is not None:
            tick = Decimal(str(asset.get('price_increment') or '.01')) if kind == 'crypto' else Decimal('.0001') if limit < 1 and kind == 'us_equity' else Decimal('.01')
            if limit % tick:
                raise ValueError(f'Limit price must use increments of {tick}')
        market_open = kind == 'crypto' or bool(ex.clock.get('is_open'))
        if sending and not market_open:
            raise ValueError('US market is closed. Review again when it opens')
        if not reducing and (ex.halted_reason or self.s.kill_file.exists()):
            raise ValueError('The paper executor is halted')
        snap = (await self.client.snapshots([symbol], kind)).get(symbol, {})
        quote = snap.get('latestQuote') or {}
        ref = Decimal(str(quote.get('ap' if side == 'buy' else 'bp') or (snap.get('latestTrade') or {}).get('p') or 0))
        if not ref.is_finite():
            ref = Decimal(0)
        quote_ts = timestamp(quote.get('t'))
        if sending and order_type == 'market' and (not ref.is_finite() or ref <= 0 or time.time() - quote_ts > 30):
            raise ValueError('A fresh executable quote is required for a market order')
        price = max(limit or Decimal(0), ref)
        if price <= 0:
            raise ValueError('No price is available. Use a limit price')
        if kind == 'crypto' and not full_close and qty * (limit or ref) < 10:
            raise ValueError('Crypto orders require at least $10 at the order price')
        multiplier = Decimal(str(asset.get('size') or 100)) if kind == 'option' else Decimal(1)
        if kind == 'option' and multiplier != 100:
            raise ValueError('Only standard 100-share option contracts are supported')
        estimate = qty * price * multiplier
        cap = Decimal(str(self.s.max_notional_usdt))
        if kind == 'option':
            cap = min(cap, Decimal(str(ex.wallet['equity'])) * Decimal(str(self.s.risk_per_trade_pct)) / 100)
        if not reducing:
            if estimate > cap or abs(held + (qty if side == 'buy' else -qty)) * price * multiplier > cap:
                raise ValueError(f'Opening exposure exceeds the ${cap:,.2f} paper cap')
            power = ex.account.get('cash' if kind == 'crypto' else 'options_buying_power' if kind == 'option' else 'buying_power') or 0
            if kind == 'crypto':
                power = min(Decimal(str(power)), Decimal(str(ex.account.get('non_marginable_buying_power', power))))
            if estimate * Decimal('1.01') > Decimal(str(power)):
                raise ValueError('Insufficient paper buying power including a 1% reserve')
        body = {'symbol': symbol, 'side': side, 'qty': format(qty, 'f'), 'type': order_type, 'time_in_force': tif}
        if limit is not None:
            body['limit_price'] = format(limit, 'f')
        if kind == 'option':
            body['position_intent'] = 'sell_to_close' if reducing else 'buy_to_open'
        return body, {'asset_class': kind, 'estimated_notional': float(estimate), 'multiplier': float(multiplier),
                      'reducing': reducing, 'position_qty': str(held), 'quote_ts': quote_ts,
                      'market_open': market_open, 'feed': 'indicative' if kind == 'option' else 'Alpaca US' if kind == 'crypto' else 'IEX',
                      'paper': True, 'account_id': ex.account_id, 'resources': resources, 'expires_at': time.time() + 120}

    async def preview(self, values):
        async with self.ex._entry_lock:
            await self.account_ready()
            await self.refresh()
            body, review = await self.validate(values)
            identity = uuid.uuid4().hex
            body['client_order_id'] = 'ctba-m-' + identity
            row = {'id': identity, 'account_id': self.ex.account_id, 'symbol': body['symbol'],
                   'client_order_id': body['client_order_id'], 'created_ts': time.time(), 'status': 'preview',
                   'request_json': body, 'review_json': review}
            self.db.insert('manual_orders', row)
            return self.view(row)

    def view(self, row):
        return {'id': row['id'], 'account_id': row['account_id'], 'created_ts': row['created_ts'],
                'status': row['status'], 'request': row['request_json'], 'review': row['review_json'],
                'broker': row.get('broker_json'), 'error': row.get('error'), 'paper': True}

    async def recover(self, row):
        try:
            order = await self.client.order(row['client_order_id'])
            if order.get('client_order_id') != row['client_order_id']:
                raise ValueError('Recovered broker order does not match the manual ticket')
            self.db.update('manual_orders', row['id'], {'broker_json': order, 'status': order['status'], 'error': None})
        except AlpacaError as exc:
            if exc.status != 404:
                raise
            self.db.update('manual_orders', row['id'], {'status': 'unknown', 'error': 'Broker response is unresolved. No order will be resubmitted'})

    async def refresh(self):
        for row in self.db.query("SELECT * FROM manual_orders WHERE account_id=? AND attempted_ts IS NOT NULL "
                                  "AND status NOT IN ('filled','canceled','expired','rejected','replaced')", [self.s.alpaca_account_id]):
            await self.recover(row)
        for row in self.db.query("SELECT * FROM manual_orders WHERE account_id=? AND attempted_ts IS NOT NULL "
                                  "AND inventory_seen_ts IS NULL AND status IN ('filled','canceled','expired','rejected','replaced')", [self.s.alpaca_account_id]):
            body = row['request_json']
            filled = Decimal(str((row.get('broker_json') or {}).get('filled_qty') or 0))
            before = Decimal(row['review_json']['position_qty'])
            expected = before + (filled if body['side'] == 'buy' else -filled)
            position = next((p for p in self.ex.raw_positions if normalized(p['symbol']) == normalized(row['symbol'])), None)
            actual = Decimal(str(position['qty'])) if position else Decimal(0)
            fee_adjusted_buy = asset_class(row['symbol']) == 'crypto' and body['side'] == 'buy' and before < actual <= expected
            if not filled or actual == expected or fee_adjusted_buy:
                self.db.update('manual_orders', row['id'], {'inventory_seen_ts': time.time()})
        # Keep manual ownership through fills, delayed positions and option exercise.
        claims = self.db.query('SELECT * FROM manual_ownership WHERE account_id=?', [self.s.alpaca_account_id])
        groups = {}
        for claim in claims:
            groups.setdefault(claim['position_symbol'], []).append(claim)
        pending = self.db.query("SELECT symbol FROM manual_orders WHERE account_id=? AND attempted_ts IS NOT NULL "
                                "AND status NOT IN ('filled','canceled','expired','rejected','replaced')", [self.s.alpaca_account_id])
        for position_symbol, group in groups.items():
            resources = {g['resource'] for g in group}
            has_inventory = any(normalized(p['symbol']) in resources for p in self.ex.raw_positions)
            has_orders = any(normalized(o.get('symbol', '')) in resources for o in self.ex.open_orders)
            unresolved = any(normalized(r['symbol']) == position_symbol for r in pending)
            if has_inventory or has_orders or unresolved:
                with self.db.lock:
                    self.db.conn.execute('UPDATE manual_ownership SET flat_since=NULL WHERE account_id=? AND position_symbol=?',
                                         [self.s.alpaca_account_id, position_symbol])
            elif all(g['flat_since'] and time.time() - g['flat_since'] >= 10 for g in group):
                with self.db.lock:
                    self.db.conn.execute('DELETE FROM manual_ownership WHERE account_id=? AND position_symbol=?',
                                         [self.s.alpaca_account_id, position_symbol])
            else:
                with self.db.lock:
                    self.db.conn.execute('UPDATE manual_ownership SET flat_since=COALESCE(flat_since,?) WHERE account_id=? AND position_symbol=?',
                                         [time.time(), self.s.alpaca_account_id, position_symbol])

    async def send(self, identity):
        async with self.ex._entry_lock:
            await self.account_ready()
            row = self.db.one('SELECT * FROM manual_orders WHERE id=?', [identity])
            if not row or row['account_id'] != self.s.alpaca_account_id:
                raise ValueError('Paper order review was not found in this account')
            if row.get('attempted_ts'):
                if row['status'] not in FINAL:
                    await self.recover(row)
                return self.view(self.db.one('SELECT * FROM manual_orders WHERE id=?', [identity]))
            if row['review_json']['expires_at'] < time.time():
                raise ValueError('Order review expired. Review the ticket again')
            body, review = await self.validate(row['request_json'], sending=True, ignore_id=identity)
            if (review['position_qty'] != row['review_json']['position_qty']
                    or review['reducing'] != row['review_json']['reducing']):
                raise ValueError('The position changed. Review the ticket again')
            if body['type'] == 'market' and abs(review['estimated_notional'] / row['review_json']['estimated_notional'] - 1) > .01:
                raise ValueError('The market price moved more than 1%. Review the ticket again')
            body['client_order_id'] = row['client_order_id']
            if body != row['request_json']:
                raise ValueError('Order changed. Review the ticket again')
            # This state survives process death between POST and its response. Never POST it twice.
            with self.db.lock:
                self.db.conn.execute('BEGIN IMMEDIATE')
                try:
                    claimed = self.db.conn.execute("UPDATE manual_orders SET attempted_ts=?,status='submitting' WHERE id=? "
                                                   "AND account_id=? AND attempted_ts IS NULL AND status='preview'",
                                                   [time.time(), identity, self.ex.account_id]).rowcount
                    if claimed:
                        for resource in review['resources']:
                            self.db.conn.execute('INSERT INTO manual_ownership(account_id,resource,position_symbol,flat_since) VALUES(?,?,?,NULL) '
                                                 'ON CONFLICT(account_id,resource,position_symbol) DO UPDATE SET flat_since=NULL',
                                                 [self.ex.account_id, resource, normalized(body['symbol'])])
                    self.db.conn.execute('COMMIT')
                except BaseException:
                    self.db.conn.execute('ROLLBACK')
                    raise
            if not claimed:
                return self.view(self.db.one('SELECT * FROM manual_orders WHERE id=?', [identity]))
            try:
                order = await self.client.submit(body)
                self.db.update('manual_orders', identity, {'broker_json': order, 'status': order['status'], 'error': None})
            except AlpacaError as exc:
                ambiguous = exc.status >= 500 or exc.status in (408, 429)
                self.db.update('manual_orders', identity, {'status': 'unknown' if ambiguous else 'rejected', 'error': str(exc)})
            except (aiohttp.ClientError, asyncio.TimeoutError):
                self.db.update('manual_orders', identity, {'status': 'unknown', 'error': 'Broker response is unresolved. No order will be resubmitted'})
            except Exception:
                self.db.update('manual_orders', identity, {'status': 'unknown', 'error': 'Broker response could not be confirmed. Reconciliation will use the same order ID'})
            self.ex.emit('manual_order', {'id': identity})
            self.ex.emit('event', self.db.event('info', 'manual_paper_order', f"Manual {body['side']} {body['qty']} {body['symbol']} submitted", body['symbol']))
            return self.view(self.db.one('SELECT * FROM manual_orders WHERE id=?', [identity]))

    async def orders(self):
        await self.ex.sync()
        if self.ex.sync_error or self.ex.account_id != self.s.alpaca_account_id:
            raise ValueError('Paper account is disconnected or does not match')
        await self.refresh()
        rows = self.db.query('SELECT * FROM manual_orders WHERE account_id=? AND attempted_ts IS NOT NULL ORDER BY created_ts DESC LIMIT 100', [self.ex.account_id])
        return {'orders': [self.view(r) for r in rows], 'positions': self.ex.account_view()['positions'],
                'account': self.ex.account_view(), 'paper': True}

    async def cancel(self, identity):
        async with self.ex._entry_lock:
            await self.ex.sync()
            if self.ex.sync_error or self.ex.account_id != self.s.alpaca_account_id:
                raise ValueError('Paper account is disconnected or does not match')
            row = self.db.one('SELECT * FROM manual_orders WHERE id=? AND account_id=?', [identity, self.ex.account_id])
            if not row or not row.get('attempted_ts'):
                raise ValueError('Manual order was not found in this account')
            await self.recover(row)
            row = self.db.one('SELECT * FROM manual_orders WHERE id=?', [identity])
            order = row.get('broker_json')
            if order and row['status'] not in FINAL:
                await self.client.cancel(order)
                self.db.update('manual_orders', identity, {'status': 'pending_cancel'})
            self.ex.emit('manual_order', {'id': identity})
            return self.view(self.db.one('SELECT * FROM manual_orders WHERE id=?', [identity]))
