import asyncio
import math
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from ctb.config import Settings
from ctb.market.alpaca import AlpacaFeed
from ctb.market.store import Bar, MarketStore
from ctb.market.watchlist import DEFAULT_WATCHLIST
from ctb.research import Research
from ctb.server import make_app
from ctb.engine import Engine
from ctb.market.stream import AlpacaStream


def test_large_default_watchlist_is_unique_and_does_not_expand_model_universe():
    assert len(DEFAULT_WATCHLIST) >= 200 and len(DEFAULT_WATCHLIST) == len(set(DEFAULT_WATCHLIST))
    assert 'MSFT' in DEFAULT_WATCHLIST and 'BTC/USD' in DEFAULT_WATCHLIST
    s = Settings(); s.symbols = ['SPY']
    assert len(s.watchlist_symbols) >= 200 and s.symbols == ['SPY']


def test_selected_stock_gets_stream_slot_without_exceeding_free_limit():
    symbols = [f'S{i}' for i in range(100)] + ['BTC/USD']
    feed = AlpacaFeed(MarketStore(symbols), None, model_symbols=['S0', 'BTC/USD'])
    assert len(feed.subscription('us_equity')['quotes']) == 30
    assert 'S99' not in feed.subscription('us_equity')['quotes']
    feed.watch_symbol('S99')
    assert feed.subscription('us_equity')['quotes'][0] == 'S99'
    assert 'S0' in feed.subscription('us_equity')['quotes']
    assert len(feed.subscription('us_equity')['quotes']) == 30
    assert feed.book_info('S98')['state'] == 'polling'
    assert feed.subscription('crypto')['orderbooks'] == ['BTC/USD']


def test_concurrent_cold_chart_requests_fetch_history_once():
    async def run():
        class Feed(AlpacaFeed):
            calls = 0
            async def history(self, symbol, tf, **_):
                self.calls += 1
                await asyncio.sleep(.01)
                self.store[symbol].upsert_bar(tf, Bar(0, 100, 101, 99, 100, 1, 100))
        feed = Feed(MarketStore(['SPY', 'MSFT']), None, model_symbols=['SPY'])
        await asyncio.gather(feed.ensure_history('MSFT', '5'), feed.ensure_history('MSFT', '5'))
        assert feed.calls == 1 and feed.model_symbols == ['SPY']
        assert ('MSFT', '5') in feed._watched
    asyncio.run(run())


def test_add_ticker_persists_watchlist_without_changing_trading_universe(monkeypatch):
    async def run():
        settings = Settings(); settings.venue = 'alpaca'; settings.symbols = ['SPY']; settings.watchlist_symbols = ['SPY']
        store = MarketStore(['SPY']); saved = []
        monkeypatch.setattr('ctb.server.save_env', lambda values: saved.append(values))
        class Client:
            async def asset(self, symbol): return {'symbol': symbol, 'tradable': True}
        class Feed:
            def watch_symbol(self, *_): pass
            async def refresh_quotes(self): pass
        engine = SimpleNamespace(s=settings, store=store, alpaca=Client(), feed=Feed(), emit=lambda *_: None)
        app = make_app(engine); app.on_startup.clear(); app.on_cleanup.clear()
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f'http://127.0.0.1:{port}/api/watchlist', json={'symbol': 'AMD'}) as response:
                    assert response.status == 200 and (await response.json())['symbol'] == 'AMD'
            assert 'AMD' in store.state and store.symbols == ['SPY', 'AMD']
            assert settings.symbols == ['SPY'] and saved == [{'CTB_WATCHLIST': 'SPY,AMD'}]
        finally: await runner.cleanup()
    asyncio.run(run())


def test_surface_uses_actual_points_filters_expiries_and_keeps_missing_values():
    async def run():
        expiry = datetime.now(timezone.utc).date() + timedelta(days=30)
        root = 'SPY' + expiry.strftime('%y%m%d')
        class Client:
            calls = []
            async def snapshots(self, symbols, kind): return {'SPY': {'latestTrade': {'p': 100}}}
            async def request(self, *_args, **kwargs):
                self.calls.append(kwargs['params'])
                if len(self.calls) == 1:
                    return {'snapshots': {root + 'C00095000': {'impliedVolatility': .25, 'latestQuote': {'bp': 4, 'ap': 6}, 'greeks': {'delta': .6}}}, 'next_page_token': 'page2'}
                return {'snapshots': {root + 'C00100000': {'impliedVolatility': 0, 'latestQuote': {'bp': 3, 'ap': 2}, 'greeks': {'delta': math.nan}},
                                      root + 'P00100000': {'impliedVolatility': .3}}}
        client = Client(); research = Research(None, client)
        result = await research.option_surface('SPY')
        assert len(result['points']) == 2 and result['expiries'] == [expiry.isoformat()]
        assert result['points'][0]['iv'] == .25 and result['points'][0]['mid'] == 5
        assert result['points'][1]['iv'] is None and result['points'][1]['mid'] is None and result['points'][1]['delta'] is None
        assert client.calls[1]['page_token'] == 'page2' and client.calls[0]['feed'] == 'indicative'
        assert client.calls[0]['limit'] == 1000 and client.calls[0]['strike_price_gte'] == 75
        await research.option_surface('SPY'); assert len(client.calls) == 2
    asyncio.run(run())


def test_watchlist_bars_do_not_trigger_automatic_model_requests():
    async def run():
        calls = []
        async def decide(symbol): calls.append(symbol)
        engine = SimpleNamespace(s=SimpleNamespace(venue='alpaca', symbols=['SPY'], decision_tf='5', bar_settle_s=0),
                                 decide=decide, emit=lambda *_: None)
        await Engine.on_bar_close(engine, 'MSFT', '5')
        assert not calls
        await Engine.on_bar_close(engine, 'SPY', '5')
        assert calls == ['SPY']
    asyncio.run(run())


def test_stream_rebalances_quotes_with_unsubscribe_ack_before_new_subscription():
    async def run():
        desired, received, states = {'quotes': ['SPY']}, [], []
        async def handler(req):
            ws = web.WebSocketResponse(); await ws.prepare(req)
            await ws.send_json([{'T': 'success', 'msg': 'authenticated'}])
            assert await ws.receive_json() == {'action': 'subscribe', 'quotes': ['SPY']}
            await ws.send_json([{'T': 'subscription', 'quotes': ['SPY']}, {'T': 'q', 'S': 'SPY'}])
            assert await ws.receive_json() == {'action': 'unsubscribe', 'quotes': ['SPY']}
            await ws.send_json([{'T': 'subscription', 'quotes': []}])
            assert await ws.receive_json() == {'action': 'subscribe', 'quotes': ['MSFT']}
            await ws.send_json([{'T': 'subscription', 'quotes': ['MSFT']}, {'T': 'q', 'S': 'MSFT'}])
            await ws.receive()
            return ws
        app = web.Application(); app.router.add_get('/stream', handler)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        try:
            async with aiohttp.ClientSession() as session:
                async def message(n):
                    received.append(n['S'])
                    if n['S'] == 'SPY': desired['quotes'] = ['MSFT']
                    else: stream.stop()
                client = SimpleNamespace(configured=True, session=session, headers={})
                port = site._server.sockets[0].getsockname()[1]
                stream = AlpacaStream(client, f'http://127.0.0.1:{port}/stream', lambda: dict(desired), message,
                                      lambda state, error: states.append(state))
                await asyncio.wait_for(stream.run(), timeout=5)
                assert received == ['SPY', 'MSFT'] and states.count('live') == 2 and 'reconnecting' not in states
        finally: await runner.cleanup()
    asyncio.run(run())


def test_stream_recovers_lower_entitlement_without_dropping_selected_ticker():
    async def run():
        requests, states = [], []
        feed = AlpacaFeed(MarketStore(['SPY', 'AAPL', 'MSFT', 'NVDA']), None, model_symbols=['SPY'])
        feed.watch_symbol('NVDA')
        async def handler(req):
            ws = web.WebSocketResponse(); await ws.prepare(req)
            await ws.send_json([{'T': 'success', 'msg': 'authenticated'}])
            request = await ws.receive_json(); requests.append(request)
            if len(request['quotes']) > 2:
                await ws.send_json([{'T': 'error', 'code': 405, 'msg': 'symbol limit exceeded'}])
            else:
                await ws.send_json([{'T': 'subscription', **{k: v for k, v in request.items() if k != 'action'}},
                                    {'T': 'q', 'S': 'NVDA'}])
            await ws.receive()
            return ws
        app = web.Application(); app.router.add_get('/stream', handler)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        try:
            async with aiohttp.ClientSession() as session:
                async def message(_): stream.stop()
                client = SimpleNamespace(configured=True, session=session, headers={})
                port = site._server.sockets[0].getsockname()[1]
                stream = AlpacaStream(client, f'http://127.0.0.1:{port}/stream',
                                      lambda: feed.subscription('us_equity'), message,
                                      lambda state, error: states.append(state))
                feed._streams_by_kind['us_equity'] = stream
                await asyncio.wait_for(stream.run(), timeout=5)
                assert [len(r['quotes']) for r in requests] == [4, 2]
                assert requests[-1]['quotes'] == ['NVDA', 'SPY']
                assert 'live' in states and stream.symbol_limit == 2
                assert feed.book_info('AAPL')['state'] == 'polling'
        finally: await runner.cleanup()
    asyncio.run(run())
