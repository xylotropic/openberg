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
from ctb.research import Research
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
