"""Failure-mode checks for the paper adapter, source provenance and provider integration."""
import asyncio
import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from ctb.broker.alpaca import AlpacaPaper, AlpacaError, asset_class
from ctb.broker.alpaca_executor import AlpacaExecutor
from ctb.config import Settings
from ctb.db import DB
from ctb.kairos.client import _error_from
from ctb.market.alpaca import AlpacaFeed
from ctb.market.store import MarketStore
from ctb.models import MODELS
from ctb.research import AlpacaContext, Research
from ctb.market.stream import AlpacaStream
from ctb.server import make_app


class Broker:
    def __init__(self, positions=None):
        self.submitted, self.cancelled = [], []
        self.pos = positions or []
        self.by_link = {}

    async def account(self):
        return {'id': 'paper-test', 'equity': '100000', 'cash': '100000', 'buying_power': '200000',
                'options_buying_power': '100000', 'options_trading_level': 3}

    async def positions(self): return self.pos
    async def orders(self): return []
    async def clock(self):
        from ctb.broker.alpaca import iso
        return {'is_open': True, 'next_close': iso(time.time() + 8 * 3600)}
    async def asset(self, symbol): return {'tradable': True, 'shortable': True, 'size': '100'}
    async def order(self, link):
        if link not in self.by_link: raise AlpacaError(404, 'not found')
        return self.by_link[link]
    async def submit(self, body):
        self.submitted.append(body)
        return {'id': 'new', 'status': 'new', **body}
    async def cancel(self, order): self.cancelled.append(order['id'])


def executor(tmp_path, broker=None):
    s = Settings()
    s.venue, s.symbols, s.alpaca_account_id = 'alpaca', ['BTC/USD'], 'paper-test'
    s.trading_enabled = True
    s.kill_file = tmp_path / 'KILL'
    db = DB(tmp_path / 'test.db')
    store = MarketStore(s.symbols)
    store['BTC/USD'].last_update = time.time()
    store['BTC/USD'].ticker = {'bid1Price': 60000, 'ask1Price': 60001}
    b = broker or Broker()
    feed = AlpacaFeed(store, b)
    ex = AlpacaExecutor(s, db, b, lambda *_: None, feed)
    ex.account_id = 'paper-test'
    return ex, b, db


def add_trade(db, **values):
    row = {'venue': 'alpaca', 'account_id': 'paper-test', 'symbol': 'BTC/USD', 'side': 'long',
           'qty': 1, 'entry_price': 60000, 'status': 'open', 'opened_ts': time.time(),
           'entry_link_id': 'ctba-entry', 'deadline_ts': time.time() + 3600, 'risk_usd': 600,
           'planned_sl_bp': 100, 'planned_tp_bp': 150, 'sl_price': 59400, 'tp_price': 60900}
    return db.insert('trades', {**row, **values})


def test_asset_rules_distinguish_spot_equity_and_option():
    assert asset_class('BTC/USD') == 'crypto'
    assert asset_class('SPY') == 'us_equity'
    assert asset_class('SPY261120C00750000') == 'option'


def test_candle_executor_allocates_five_percent_of_current_equity(tmp_path):
    from test_scalper import history
    from ctb.scalper import packet,VERSION
    ex,b,db=executor(tmp_path)
    store,now=history(end=time.time());ex.feed.store=store
    ex.s.scalper_enabled=True;ex.s.maker_wait_s=0;ex.s.symbols=['TSLA']
    # Quote freshness is evaluated at execution time, independently of trade prints.
    store['TSLA'].book_ts=time.time();store['TSLA'].last_update=time.time()
    data=packet(store['TSLA'],time.time())
    did=db.insert('decisions',{'ts':time.time(),'symbol':'TSLA','qset_version':VERSION,'state_json':data,
        'decision_json':{'signal_at':time.time()}})
    async def rejected(body):
        b.submitted.append(body);return {'id':'rejected','status':'rejected','filled_qty':'0',**body}
    b.submit=rejected
    asyncio.run(ex.open(symbol='TSLA',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0,horizon_min=30))
    assert len(b.submitted)==1
    body=b.submitted[0]
    assert float(body['qty'])*float(body['limit_price'])<=5000.000001
    assert float(body['qty'])*float(body['limit_price'])>4800
    trade=db.one('SELECT * FROM trades WHERE decision_id=?',[did])
    assert trade['risk_usd']<=50.000001 and trade['mode']=='candle'


def test_candle_executor_refuses_expired_model_signal(tmp_path):
    from ctb.scalper import VERSION
    ex,b,db=executor(tmp_path);ex.s.scalper_enabled=True;ex.s.symbols=['TSLA']
    did=db.insert('decisions',{'ts':time.time(),'symbol':'TSLA','qset_version':VERSION,'decision_json':{'signal_at':time.time()-26}})
    res=asyncio.run(ex.open(symbol='TSLA',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0))
    assert not res['ok'] and 'expired' in res['reason'] and not b.submitted


def test_candle_pending_exit_survives_a_late_fill(tmp_path,monkeypatch):
    ex,b,db=executor(tmp_path);ex.s.symbols=['TSLA']
    tid=add_trade(db,symbol='TSLA',side='long',mode='candle',status='opening',qty=None,requested_qty=1,
        planned_sl_bp=100,planned_tp_bp=200)
    b.by_link['ctba-entry']={'id':'entry','client_order_id':'ctba-entry','symbol':'TSLA','status':'new','filled_qty':'0'}
    async def cancel(order):
        b.cancelled.append(order['id']);b.by_link[order['client_order_id']]['status']='canceled'
    b.cancel=cancel
    asyncio.run(ex.close('TSLA','drex_exit'))
    assert db.one('SELECT close_reason FROM trades WHERE id=?',[tid])['close_reason']=='drex_exit'
    b.pos=[{'symbol':'TSLA','qty':'1','side':'long','avg_entry_price':'100','current_price':'100','unrealized_pl':'0','market_value':'100'}]
    order=b.by_link['ctba-entry'];order.update(filled_qty='1',filled_avg_price='100')
    async def submit(body):
        b.submitted.append(body);r={'id':body['client_order_id'],**body,'status':'new','filled_qty':'0'}
        b.by_link[body['client_order_id']]=r;return r
    b.submit=submit
    asyncio.run(ex.resolve_entry(db.one('SELECT * FROM trades WHERE id=?',[tid]),order))
    assert db.one('SELECT status FROM trades WHERE id=?',[tid])['status']=='open'
    async def refresh(): pass
    ex.manual.refresh=refresh
    async def stop_after_iteration(seconds):
        if seconds==5: raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio,'sleep',stop_after_iteration)
    with pytest.raises(asyncio.CancelledError): asyncio.run(ex.reconcile_loop())
    assert any(o['type']=='market' and o['side']=='sell' and o['qty']=='1.0' for o in b.submitted)


def test_exact_trade_close_does_not_close_a_replacement(tmp_path):
    ex,b,db=executor(tmp_path)
    old=add_trade(db,status='closed')
    add_trade(db,status='opening',entry_link_id='replacement',mode='candle')
    assert not asyncio.run(ex.close('BTC/USD','drex_exit',expected_trade_id=old))
    assert not b.cancelled and not b.submitted


def test_drex_spec_uses_independent_credentials_and_finance_model(monkeypatch):
    monkeypatch.delenv('CTB_DREX_MODEL', raising=False)
    monkeypatch.delenv('NACE_BASE_URL', raising=False)
    monkeypatch.setenv('NACE_API_KEY', 'nace-test')
    monkeypatch.setenv('TYPESAFE_API_KEY', 'different-jev-key')
    spec = MODELS['drex']
    assert spec.api_key == 'nace-test'
    assert spec.model == 'drex-max-finance'
    assert spec.base_url == 'https://console.nace.ai'
    assert not spec.supports_news_return


def test_drex_errors_retain_request_id_and_do_not_retry_billing():
    err = _error_from(402, {'request_id': 'req_billing', 'error': {'type': 'insufficient_credit', 'message': 'No credit'}}, MODELS['drex'])
    assert err.kind == 'billing' and err.request_id == 'req_billing'
    assert not err.retryable
    assert _error_from(529, {}, MODELS['drex']).retryable


def test_foreign_quantity_blocks_close_without_submitting(tmp_path):
    b = Broker([{'symbol': 'BTCUSD', 'qty': '2', 'side': 'long', 'avg_entry_price': '60000',
                 'current_price': '60000', 'unrealized_pl': '0', 'market_value': '120000'}])
    ex, b, db = executor(tmp_path, b)
    add_trade(db)
    assert not asyncio.run(ex.close('BTC/USD', 'manual'))
    assert not b.submitted and not b.cancelled
    assert 'External change' in ex.halted_reason


def test_kill_cancels_only_recorded_opening_order(tmp_path):
    ex, b, db = executor(tmp_path)
    add_trade(db, status='opening')
    b.by_link['ctba-entry'] = {'id': 'owned-entry', 'client_order_id': 'ctba-entry', 'status': 'new'}
    asyncio.run(ex.kill('test kill'))
    assert b.cancelled == ['owned-entry']
    assert not b.submitted and ex.kill_requested and not ex.s.trading_enabled


def test_crypto_short_is_rejected_before_broker_write(tmp_path):
    ex, b, _ = executor(tmp_path)
    result = asyncio.run(ex.open(symbol='BTC/USD', side='short', sl_bp=100, tp_bp=150, decision_id=1, ev_r=1))
    assert not result['ok'] and 'spot-only' in result['reason']
    assert not b.submitted


@pytest.mark.parametrize('quantity', [True, 1.5, 0, -1])
def test_option_contract_quantity_is_whole_and_positive(tmp_path, quantity):
    ex, b, _ = executor(tmp_path)
    with pytest.raises(ValueError, match='whole quantity'):
        asyncio.run(ex.option_entry('SPY261120C00750000', quantity, 1))
    assert not b.submitted


def test_option_sizing_accounts_for_contract_multiplier(tmp_path):
    ex, b, _ = executor(tmp_path)
    with pytest.raises(ValueError, match='risk cap'):
        asyncio.run(ex.option_entry('SPY261120C00750000', 1, 10))  # $1,000 debit > $500 cap
    assert not b.submitted


def test_free_option_feed_cannot_trigger_automatic_forecast(tmp_path):
    store = MarketStore(['SPY261120C00750000'])
    store[store.symbols[0]].last_update = time.time()
    feed = AlpacaFeed(store, Broker())
    feed.clock = {'is_open': True}
    ready, reason = feed.decision_ready(store.symbols[0])
    assert not ready and 'indicative' in reason


def test_market_closed_is_explicit(tmp_path):
    feed = AlpacaFeed(MarketStore(['SPY']), Broker())
    ready, reason = feed.decision_ready('SPY')
    assert not ready and 'market closed' in reason


def test_sec_all_form_types_and_archive_manifest_validation():
    async def run():
        r = Research(None, None)
        async def cik(_): return '0000320193'
        async def sec(path):
            assert path == 'submissions/CIK0000320193.json'
            return {'name': 'Issuer', 'filings': {'recent': {'accessionNumber': ['1', '2', '3'],
                'form': ['4', 'S-1/A', '144'], 'filingDate': ['2026-01-01'] * 3,
                'primaryDocument': ['a.xml', 'b.htm', 'c.xml']}, 'files': [{'name': 'allowed.json'}]}}
        r.resolve_cik, r.sec = cik, sec
        result = await r.filings('AAPL')
        assert {x['form'] for x in result['items']} == {'4', 'S-1/A', '144'}
        with pytest.raises(ValueError, match='manifest'):
            await r.filings('AAPL', archive='../../not-an-archive')
    asyncio.run(run())


def test_keys_api_masks_secrets_and_cross_site_writes_are_blocked(monkeypatch):
    monkeypatch.setenv('NACE_API_KEY', 'private-nace-credential-1234')
    monkeypatch.setenv('APCA_API_KEY_ID', 'private-alpaca-key-5678')
    monkeypatch.setenv('APCA_API_SECRET_KEY', 'private-alpaca-secret-9012')
    async def run():
        app = make_app(SimpleNamespace(s=Settings(), executor=None))
        app.on_startup.clear(); app.on_cleanup.clear()
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f'http://127.0.0.1:{port}/api/keys') as resp:
                    text = await resp.text()
                    assert resp.status == 200
                    assert 'private-' not in text and '5678' in text and '9012' in text
                async with session.post(f'http://127.0.0.1:{port}/api/keys/drex', json={'api_key': 'x'},
                                        headers={'Origin': 'https://unrelated.example'}) as resp:
                    assert resp.status == 403
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_crypto_fee_uses_credited_quantity_for_backstop(tmp_path):
    b = Broker([{'symbol': 'BTCUSD', 'qty': '0.009975001', 'side': 'long', 'avg_entry_price': '60000',
                 'current_price': '60000', 'unrealized_pl': '0', 'market_value': '598.50006'}])
    ex, b, db = executor(tmp_path, b)
    async def activities(*_a, **_kw): return [{'symbol': 'BTC/USD', 'order_id': 'entry-id'}]
    b.request = activities
    tid = add_trade(db, status='opening', qty=None, ref_price=60000)
    t = db.one('SELECT * FROM trades WHERE id=?', [tid])
    asyncio.run(ex.resolve_entry(t, {'id': 'entry-id', 'status': 'filled', 'filled_qty': '0.01', 'filled_avg_price': '60000'}))
    t = db.one('SELECT * FROM trades WHERE id=?', [tid])
    assert t['qty'] == .009975001 and t['gross_qty'] == .01
    assert b.submitted[0]['qty'] == '0.009975001'
    assert b.submitted[0]['client_order_id'] == 'ctba-entry-stop'
    assert t['entry_fee'] == pytest.approx((.01 - .009975001) * 60000)


def test_partial_exit_retries_only_remaining_quantity_with_new_id(tmp_path):
    b = Broker([{'symbol': 'BTCUSD', 'qty': '0.4', 'side': 'long', 'avg_entry_price': '60000',
                 'current_price': '60000', 'unrealized_pl': '0', 'market_value': '24000'}])
    ex, b, db = executor(tmp_path, b)
    tid = add_trade(db, status='closing', exit_link_id='ctba-entry-x1', exit_links_json=['ctba-entry-x1'])
    b.by_link['ctba-entry-x1'] = {'client_order_id': 'ctba-entry-x1', 'status': 'canceled', 'filled_qty': '.6'}
    asyncio.run(ex.continue_close(db.one('SELECT * FROM trades WHERE id=?', [tid])))
    assert b.submitted[0]['qty'] == '0.4'
    assert b.submitted[0]['client_order_id'] == 'ctba-entry-x2'


def test_ambiguous_exit_reuses_same_durable_client_id(tmp_path):
    b = Broker([{'symbol': 'BTCUSD', 'qty': '1', 'side': 'long', 'avg_entry_price': '60000',
                 'current_price': '60000', 'unrealized_pl': '0', 'market_value': '60000'}])
    ex, b, db = executor(tmp_path, b)
    tid = add_trade(db, status='closing', exit_link_id='ctba-entry-x1', exit_links_json=['ctba-entry-x1'])
    asyncio.run(ex.continue_close(db.one('SELECT * FROM trades WHERE id=?', [tid])))
    assert b.submitted[0]['client_order_id'] == 'ctba-entry-x1'
    assert len(db.one('SELECT * FROM trades WHERE id=?', [tid])['exit_links_json']) == 1


def test_filled_backstop_is_booked_without_a_second_exit(tmp_path):
    from ctb.broker.alpaca import iso
    ex, b, db = executor(tmp_path)
    tid = add_trade(db)
    b.by_link['ctba-entry-stop'] = {'client_order_id': 'ctba-entry-stop', 'status': 'filled',
        'filled_qty': '1', 'filled_avg_price': '59000', 'filled_at': iso(time.time())}
    assert asyncio.run(ex.close('BTC/USD', 'stop'))
    assert not b.submitted
    assert db.one('SELECT status FROM trades WHERE id=?', [tid])['status'] == 'closed'


def test_equity_base_rates_exclude_overnight_windows():
    from ctb.features import base_rates
    from ctb.market.store import Bar
    # Two sessions of three bars each. A three-bar horizon would cross the overnight gap.
    bars = [Bar(t, 100, 101, 99, 100, 1, 100) for t in [0, 60000, 120000, 86400000, 86460000, 86520000]]
    assert base_rates(bars, 3, 100, 150, min_windows=1, interval_ms=60000) is None
    assert base_rates(bars, 3, 100, 150, min_windows=1) is not None


def test_crypto_pair_route_accepts_slashes(tmp_path):
    async def run():
        s = Settings(); s.symbols = ['ETH/USD']
        store = MarketStore(s.symbols)
        engine = SimpleNamespace(s=s, store=store, latest_state={})
        app = make_app(engine)
        app.on_startup.clear(); app.on_cleanup.clear()
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                for path in ['ETH/USD', 'ETH%2FUSD']:
                    async with session.get(f'http://127.0.0.1:{port}/api/market/{path}?tf=5') as resp:
                        assert resp.status == 200
                        assert (await resp.json())['symbol'] == 'ETH/USD'
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_default_option_chain_excludes_expired_contracts():
    from datetime import datetime, timezone
    captured = []
    class DataClient:
        async def request(self, *_args, **kwargs):
            captured.append(kwargs['params'])
            return {'snapshots': {}}
    async def run():
        r = Research(None, DataClient())
        await r.options('SPY')
        assert captured[-1]['expiration_date_gte'] == datetime.now(timezone.utc).date().isoformat()
        await r.options('SPY', expiry='2026-11-20')
        assert captured[-1]['expiration_date'] == '2026-11-20'
        assert 'expiration_date_gte' not in captured[-1]
    asyncio.run(run())


def test_news_wire_deduplicates_and_preserves_newer_corrections():
    async def run():
        store = MarketStore(['AAPL', 'BTC/USD'])
        emits, callbacks = [], []
        ctx = AlpacaContext(store, None, lambda *args: emits.append(args))
        async def seen(n): callbacks.append(n)
        ctx.on_news.append(seen)
        item = {'id': 42, 'headline': 'Initial headline', 'source': 'benzinga', 'symbols': ['AAPL', 'MSFT'],
                'created_at': '2026-11-13T20:00:00Z', 'updated_at': '2026-11-13T20:00:00Z', 'summary': ''}
        await ctx.ingest([item])
        first_seen = store.context['news'][0]['first_seen_ts']
        await ctx.ingest([item])
        assert len(emits) == len(callbacks) == 1
        corrected = {**item, 'headline': 'Corrected headline', 'updated_at': '2026-11-13T20:01:00Z'}
        await ctx.ingest_message({'T': 'n', **corrected})
        await ctx.ingest([item])  # A stale REST page arrives after the wire correction.
        assert ctx.articles['42']['headline'] == 'Corrected headline'
        assert len(store.context['news']) == 1 and len(callbacks) == 1 and len(emits) == 2
        assert store.context['news'][0]['first_seen_ts'] == first_seen
        assert store.context['news'][0]['symbols'] == ['AAPL']
        assert store.context['news_articles'][0]['symbols'] == ['AAPL', 'MSFT']
        assert store.context['news_status']['last_delivery'] == 'stream'
    asyncio.run(run())


def test_news_cache_keeps_wire_stories_missing_from_backfill():
    async def run():
        store = MarketStore(['BTC/USD'])
        ctx = AlpacaContext(store, None)
        def article(i):
            return {'id': i, 'headline': str(i), 'source': 'fiscal.ai', 'symbols': ['BTCUSD'],
                    'created_at': f'2026-11-13T20:{i:02}:00Z'}
        await ctx.ingest([article(2)], live=True)
        await ctx.ingest([article(1)])
        assert [n['id'] for n in store.context['news']] == ['2', '1']
        assert all(n['symbols'] == ['BTC/USD'] and n['source'] == 'fiscal.ai' for n in store.context['news'])
    asyncio.run(run())


def test_stream_requires_subscription_and_recovers_disconnect():
    async def run():
        connections, states, received = [], [], []
        async def handler(req):
            assert req.headers['APCA-API-KEY-ID'] == 'test-key'
            ws = web.WebSocketResponse(); await ws.prepare(req)
            connections.append(ws)
            await ws.send_json([{'T': 'success', 'msg': 'authenticated'}])
            sub = await ws.receive_json()
            assert sub == {'action': 'subscribe', 'news': ['*']}
            await ws.send_json([{'T': 'subscription', 'news': ['*']}])
            if len(connections) == 1:
                await ws.close()
            else:
                await ws.send_json([{'T': 'n', 'id': 42}])
                await ws.receive()
            return ws
        app = web.Application(); app.router.add_get('/stream', handler)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                async def message(n): received.append(n); stream.stop()
                stream = AlpacaStream(AlpacaPaper(session, key='test-key', secret='test-secret'),
                                      f'http://127.0.0.1:{port}/stream', lambda: {'news': ['*']},
                                      message, lambda state, error: states.append(state))
                await asyncio.wait_for(stream.run(), timeout=5)
                assert received == [{'T': 'n', 'id': 42}]
                assert states.count('live') == 2 and 'reconnecting' in states
        finally:
            await runner.cleanup()
    asyncio.run(run())
