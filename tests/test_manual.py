"""Paper ticket acceptance, duplicate protection and order-book reset/delta checks."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from ctb.broker.alpaca import AlpacaError, iso
from ctb.broker.alpaca_executor import AlpacaExecutor
from ctb.broker.manual import ManualTrading
from ctb.config import Settings
from ctb.db import DB
from ctb.market.alpaca import AlpacaFeed
from ctb.market.store import MarketStore


class Paper:
    def __init__(self):
        self.account_id = 'paper-test'
        self.pos, self.open, self.sent, self.cancelled = [], [], [], []
        self.by_id = {}
        self.timeout, self.hidden, self.reject = False, False, False
        self.price, self.quote_age, self.market_open = 100, 0, True

    async def account(self):
        return {'id': self.account_id, 'equity': '100000', 'cash': '100000', 'buying_power': '200000',
                'options_buying_power': '100000', 'options_trading_level': 3}
    async def positions(self): return self.pos
    async def orders(self): return self.open
    async def clock(self): return {'is_open': self.market_open, 'next_close': iso(time.time() + 3600)}
    async def asset(self, symbol):
        return {'tradable': True, 'shortable': True, 'fractionable': True, 'size': '100',
                'min_trade_increment': '0.00000001', 'min_order_size': '.0001'}
    async def snapshots(self, symbols, _kind):
        return {s: {'latestQuote': {'bp': self.price, 'ap': self.price, 't': iso(time.time() - self.quote_age)}} for s in symbols}
    async def submit(self, body):
        self.sent.append(body)
        if self.reject: raise AlpacaError(422, 'paper rejection')
        result = {'id': 'broker-' + str(len(self.sent)), **body, 'status': 'new', 'filled_qty': '0'}
        self.by_id[body['client_order_id']] = result
        if self.timeout: raise asyncio.TimeoutError()
        return result
    async def order(self, client_id):
        if self.hidden or client_id not in self.by_id: raise AlpacaError(404, 'not found')
        return self.by_id[client_id]
    async def cancel(self, order):
        self.cancelled.append(order['id'])
        self.by_id[order['client_order_id']]['status'] = 'canceled'


def setup(tmp_path):
    s = Settings(); s.venue = 'alpaca'; s.symbols = ['SPY', 'BTC/USD']
    s.alpaca_account_id = 'paper-test'; s.trading_enabled = False
    s.risk_per_trade_pct = 1; s.kill_file = tmp_path / 'KILL'
    broker = Paper(); store = MarketStore(s.symbols)
    db = DB(tmp_path / 'manual.db')
    ex = AlpacaExecutor(s, db, broker, lambda *_: None, AlpacaFeed(store, broker))
    return ex, broker, db


def ticket(**values):
    return {'symbol': 'SPY', 'side': 'buy', 'type': 'market', 'qty': '1', 'time_in_force': 'day', **values}


def holding(symbol='SPY', qty='1'):
    return {'symbol': symbol, 'side': 'long' if float(qty) > 0 else 'short', 'qty': qty,
            'qty_available': str(abs(float(qty))), 'avg_entry_price': '100', 'current_price': '100',
            'unrealized_pl': '0', 'market_value': str(float(qty) * 100)}


def test_review_does_not_trade_and_concurrent_send_submits_once(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path)
        r = await ex.manual.preview(ticket())
        assert r['status'] == 'preview' and not broker.sent
        a, b = await asyncio.gather(ex.manual.send(r['id']), ex.manual.send(r['id']))
        assert len(broker.sent) == 1 and a['request'] == b['request']
        assert a['paper'] and not ex.s.trading_enabled
    asyncio.run(run())


def test_timeout_recovers_same_order_across_service_instance(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path)
        r = await ex.manual.preview(ticket()); broker.timeout = True
        assert (await ex.manual.send(r['id']))['status'] == 'unknown'
        recovered = await ManualTrading(ex).send(r['id'])
        assert recovered['status'] == 'new' and len(broker.sent) == 1
        assert recovered['broker']['client_order_id'] == r['request']['client_order_id']
    asyncio.run(run())


def test_unknown_404_never_reposts_or_allows_model_or_manual_entry(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path)
        r = await ex.manual.preview(ticket()); broker.timeout = broker.hidden = True
        await ex.manual.send(r['id'])
        assert (await ex.manual.send(r['id']))['status'] == 'unknown'
        with pytest.raises(ValueError, match='reconciliation'):
            await ex.manual.preview(ticket())
        out = await ex.open(symbol='SPY', side='long', sl_bp=100, tp_bp=150, decision_id=None, ev_r=1)
        assert not out['ok'] and 'manual' in out['reason'].lower()
        assert len(broker.sent) == 1
    asyncio.run(run())


def test_broker_rejection_is_final_for_the_review(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path)
        r = await ex.manual.preview(ticket()); broker.reject = True
        assert (await ex.manual.send(r['id']))['status'] == 'rejected'
        await ex.manual.send(r['id']); assert len(broker.sent) == 1
    asyncio.run(run())


def test_account_switch_blocks_send_and_cancel(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path)
        r = await ex.manual.preview(ticket()); broker.account_id = 'different-account'
        for fn in [lambda: ex.manual.send(r['id']), lambda: ex.manual.cancel(r['id']), lambda: ex.manual.preview(ticket())]:
            with pytest.raises(ValueError): await fn()
        assert not broker.sent and not broker.cancelled
    asyncio.run(run())


@pytest.mark.parametrize('values,match', [
    ({'symbol': 'BTC/USD', 'side': 'sell', 'time_in_force': 'gtc'}, 'existing long'),
    ({'symbol': 'SPY261120C00750000', 'side': 'sell'}, 'existing long'),
    ({'symbol': 'SPY261120C00750000'}, 'limit premium'),
    ({'symbol': 'SPY261120C00750000', 'type': 'limit', 'limit_price': '1', 'qty': '1.5'}, 'increments'),
    ({'qty': 'NaN'}, 'finite'), ({'qty': 'Infinity'}, 'finite'), ({'qty': '-1'}, 'positive'),
    ({'qty': '1000'}, 'paper cap'), ({'type': 'limit', 'limit_price': '1.001'}, 'increments'),
])
def test_invalid_or_unapproved_exposure_is_rejected(tmp_path, values, match):
    async def run():
        ex, broker, _ = setup(tmp_path)
        with pytest.raises(ValueError, match=match): await ex.manual.preview(ticket(**values))
        assert not broker.sent
    asyncio.run(run())


def test_full_crypto_close_keeps_exact_fee_adjusted_available_quantity(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path)
        p = holding('BTCUSD', '0.0001007512345'); p['qty_available'] = p['qty']; broker.pos = [p]
        r = await ex.manual.preview(ticket(symbol='BTC/USD', side='sell', qty=p['qty'], time_in_force='gtc'))
        await ex.manual.send(r['id'])
        assert broker.sent[0]['qty'] == '0.0001007512345' and r['review']['reducing']
        broker.by_id[r['request']['client_order_id']]['status'] = 'canceled'
        await ex.manual.refresh()
        with pytest.raises(ValueError, match='exceeds'):
            await ex.manual.preview(ticket(symbol='BTC/USD', side='sell', qty='0.000101', time_in_force='gtc'))
    asyncio.run(run())


def test_position_change_cannot_turn_reviewed_close_into_short(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path); broker.pos = [holding()]
        r = await ex.manual.preview(ticket(side='sell')); broker.pos = []
        with pytest.raises(ValueError, match='position changed'): await ex.manual.send(r['id'])
        assert not broker.sent
    asyncio.run(run())


def test_stale_market_quote_price_move_and_expired_review_require_new_review(tmp_path):
    async def run():
        ex, broker, db = setup(tmp_path)
        r = await ex.manual.preview(ticket()); broker.quote_age = 60
        with pytest.raises(ValueError, match='fresh executable'): await ex.manual.send(r['id'])
        broker.quote_age = 0; broker.price = 105
        with pytest.raises(ValueError, match='moved'): await ex.manual.send(r['id'])
        db.update('manual_orders', r['id'], {'review_json': {**r['review'], 'expires_at': 0}})
        with pytest.raises(ValueError, match='expired'): await ex.manual.send(r['id'])
        assert not broker.sent
    asyncio.run(run())


def test_closed_market_can_be_reviewed_but_not_submitted(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path); broker.market_open = False
        r = await ex.manual.preview(ticket(type='limit', limit_price='100'))
        assert not r['review']['market_open']
        with pytest.raises(ValueError, match='market is closed'): await ex.manual.send(r['id'])
        assert not broker.sent
    asyncio.run(run())


def test_option_limit_uses_contract_multiplier_and_sell_to_close(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path); broker.price = 2
        symbol = 'SPY261120C00750000'
        r = await ex.manual.preview(ticket(symbol=symbol, type='limit', limit_price='2'))
        assert r['review']['estimated_notional'] == 200 and r['request']['position_intent'] == 'buy_to_open'
        await ex.manual.send(r['id'])
        broker.by_id[r['request']['client_order_id']]['status'] = 'filled'; await ex.manual.refresh()
        broker.pos = [holding(symbol)]
        r = await ex.manual.preview(ticket(symbol=symbol, side='sell', type='limit', limit_price='2'))
        assert r['request']['position_intent'] == 'sell_to_close'
        await ex.manual.send(r['id']); assert len(broker.sent) == 2
    asyncio.run(run())


def test_cancel_requires_own_ledger_record_and_confirms_async_cancellation(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path)
        with pytest.raises(ValueError): await ex.manual.cancel('not-owned')
        assert not broker.cancelled
        r = await ex.manual.preview(ticket()); await ex.manual.send(r['id'])
        assert (await ex.manual.cancel(r['id']))['status'] == 'pending_cancel'
        rows = await ex.manual.orders()
        assert rows['orders'][0]['status'] == 'canceled' and len(broker.cancelled) == 1
    asyncio.run(run())


def test_model_owned_symbol_and_resting_orders_block_manual_ticket(tmp_path):
    async def run():
        ex, broker, db = setup(tmp_path)
        db.insert('trades', {'symbol': 'SPY', 'side': 'long', 'venue': 'alpaca', 'account_id': 'paper-test', 'status': 'open'})
        with pytest.raises(ValueError, match='model trade'): await ex.manual.preview(ticket())
        db.conn.execute("UPDATE trades SET status='closed'")
        broker.open = [{'symbol': 'SPY', 'client_order_id': 'foreign'}]
        with pytest.raises(ValueError, match='open order'): await ex.manual.preview(ticket())
        assert not broker.sent
    asyncio.run(run())


def test_depth_snapshot_delta_delete_and_reconnect_require_new_snapshot():
    async def run():
        store = MarketStore(['BTC/USD']); feed = AlpacaFeed(store, None)
        def msg(**v): return {'T': 'o', 'S': 'BTC/USD', 't': iso(time.time()), **v}
        await feed.stream_message(msg(b=[{'p': 99, 's': 1}]))
        assert not store['BTC/USD'].bids  # No deltas before a reset snapshot.
        await feed.stream_message(msg(r=True, b=[{'p': 99, 's': 1}, {'p': 98, 's': 2}], a=[{'p': 101, 's': 3}]))
        await feed.stream_message(msg(b=[{'p': 99, 's': 0}, {'p': 98, 's': 4}]))
        assert store['BTC/USD'].bids == [(98, 4)] and store['BTC/USD'].asks == [(101, 3)]
        feed.stream_status('crypto', 'reconnecting', 'test disconnect')
        assert not store['BTC/USD'].bids and not feed.book_info('BTC/USD')['depth']
        await feed.stream_message(msg(b=[{'p': 99, 's': 10}]))
        assert not store['BTC/USD'].bids
        await feed.stream_message(msg(r=True, b=[{'p': 100, 's': 1}], a=[{'p': 102, 's': 2}]))
        assert store['BTC/USD'].bids == [(100, 1)]
    asyncio.run(run())


def test_stock_quote_sizes_are_lots_crypto_sizes_are_base_units():
    async def run():
        store = MarketStore(['SPY', 'BTC/USD']); feed = AlpacaFeed(store, None)
        for sym in store.symbols:
            await feed.stream_message({'T': 'q', 'S': sym, 't': iso(time.time()), 'bp': 100, 'ap': 101, 'bs': 2, 'as': 3})
        assert store['SPY'].bids == [(100, 200)]
        assert store['BTC/USD'].bids == [(100, 2)]
        assert not feed.book_info('SPY')['depth'] and feed.book_info('SPY')['source'] == 'IEX'
    asyncio.run(run())


def test_filled_close_keeps_inventory_reserved_until_position_reflects_fill(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path); broker.pos = [holding(qty='10')]
        r = await ex.manual.preview(ticket(side='sell', qty='8')); await ex.manual.send(r['id'])
        broker.by_id[r['request']['client_order_id']].update(status='filled', filled_qty='8')
        await ex.manual.refresh()
        with pytest.raises(ValueError, match='inventory'):
            await ex.manual.preview(ticket(side='sell', qty='8'))
        broker.pos = [holding(qty='2')]
        await ex.manual.refresh()
        with pytest.raises(ValueError, match='exceeds'):
            await ex.manual.preview(ticket(side='sell', qty='8'))
        r = await ex.manual.preview(ticket(side='sell', qty='2'))
        assert r['review']['reducing'] and len(broker.sent) == 1
    asyncio.run(run())


def test_manual_option_reserves_underlying_through_exercise_and_restart(tmp_path):
    async def run():
        ex, broker, db = setup(tmp_path); broker.price = 2
        symbol = 'SPY261120C00750000'
        r = await ex.manual.preview(ticket(symbol=symbol, type='limit', limit_price='2'))
        await ex.manual.send(r['id'])
        broker.by_id[r['request']['client_order_id']].update(status='filled', filled_qty='1')
        broker.pos = [holding(symbol)]
        await ex.sync(); await ex.manual.refresh()
        assert ex.manual_pending('SPY') and ex.manual_pending(symbol)
        # The option disappears and Alpaca delivers stock. A fresh executor uses the same claims.
        broker.pos = [holding('SPY', '100')]
        restarted = AlpacaExecutor(ex.s, db, broker, lambda *_: None, ex.feed)
        await restarted.sync(); await restarted.manual.refresh()
        assert restarted.manual_pending('SPY')
        assert db.one('SELECT * FROM manual_ownership WHERE resource=?', ['SPY'])
    asyncio.run(run())


def test_model_underlying_blocks_manual_option(tmp_path):
    async def run():
        ex, broker, db = setup(tmp_path); broker.price = 2
        db.insert('trades', {'symbol': 'SPY', 'side': 'long', 'venue': 'alpaca', 'account_id': 'paper-test', 'status': 'opening'})
        with pytest.raises(ValueError, match='model trade'):
            await ex.manual.preview(ticket(symbol='SPY261120C00750000', type='limit', limit_price='2'))
        assert not broker.sent
    asyncio.run(run())


def test_done_for_day_does_not_release_an_order_reservation(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path)
        r = await ex.manual.preview(ticket()); await ex.manual.send(r['id'])
        broker.by_id[r['request']['client_order_id']]['status'] = 'done_for_day'
        await ex.manual.refresh()
        assert ex.manual_pending('SPY')
        with pytest.raises(ValueError, match='reconciliation'):
            await ex.manual.preview(ticket())
    asyncio.run(run())


def test_quotes_continue_while_a_model_bar_callback_waits():
    async def run():
        store = MarketStore(['BTC/USD']); feed = AlpacaFeed(store, None)
        waiting = asyncio.Event()
        async def slow(*_): await waiting.wait()
        feed.on_bar_close.append(slow)
        await asyncio.wait_for(feed.stream_message({'T': 'b', 'S': 'BTC/USD', 't': iso(time.time() - 60),
                                                   'o': 100, 'h': 101, 'l': 99, 'c': 100, 'v': 1}), timeout=.2)
        await feed.stream_message({'T': 'q', 'S': 'BTC/USD', 't': iso(time.time()), 'bp': 102, 'ap': 103, 'bs': 1, 'as': 1})
        assert store['BTC/USD'].ticker['bid1Price'] == 102
        waiting.set(); await asyncio.gather(*feed._callbacks)
    asyncio.run(run())


def test_independent_executor_instances_cannot_claim_the_same_review_twice(tmp_path):
    async def run():
        ex, broker, db = setup(tmp_path)
        r = await ex.manual.preview(ticket())
        second = AlpacaExecutor(ex.s, db, broker, lambda *_: None, ex.feed)
        results = await asyncio.gather(ex.manual.send(r['id']), second.manual.send(r['id']))
        assert len(broker.sent) == 1 and all(r['request']['client_order_id'] == broker.sent[0]['client_order_id'] for r in results)
    asyncio.run(run())


def test_crypto_preview_enforces_observed_ten_dollar_order_minimum(tmp_path):
    async def run():
        ex, broker, _ = setup(tmp_path)
        with pytest.raises(ValueError, match='at least .10'):
            await ex.manual.preview(ticket(symbol='BTC/USD', qty='.05', time_in_force='gtc'))
        assert not broker.sent
    asyncio.run(run())
