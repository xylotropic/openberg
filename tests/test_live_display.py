import asyncio
import time
from types import SimpleNamespace

import pytest
import aiohttp
from aiohttp import web

from ctb.broker.display import mark_positions,mark_key,mark_basis,chart_fills
from ctb.market.store import MarketStore
from test_alpaca import executor,Broker
from test_session_execution import candle
from ctb.server import make_app


def test_live_marks_move_with_bid_and_ask_without_mutating_broker_state():
    store=MarketStore(['ETH/USD']);st=store['ETH/USD'];now=time.time()
    positions=[{'symbol':'ETH/USD','side':'long','entry':1832.96,'size':2.7,'mark':1831.73,'upnl':-3.321,'value':4945}]
    st.book_ts=now;st.ticker={'bid1Price':1829,'ask1Price':1830}
    one=mark_positions(positions,store,now)[0]
    st.ticker.update(bid1Price=1831,ask1Price=1832)
    two=mark_positions(positions,store,now)[0]
    assert one['upnl']==pytest.approx((1829-1832.96)*2.7)
    assert two['upnl']-one['upnl']==pytest.approx(5.4)
    assert two['mark_source']=='live_bid' and positions[0]['mark']==1831.73
    positions[0]['side']='short'
    short=mark_positions(positions,store,now)[0]
    assert short['mark']==1832 and short['upnl']==pytest.approx((1832.96-1832)*2.7)


@pytest.mark.parametrize('fault',['stale','future','crossed','nan','missing','malformed'])
def test_unusable_quotes_fall_back_visibly_to_broker(fault):
    store=MarketStore(['ETH/USD']);now=time.time();st=store['ETH/USD'];st.book_ts=now
    st.ticker={'bid1Price':100,'ask1Price':101}
    if fault=='stale':st.book_ts-=6
    elif fault=='future':st.book_ts+=3
    elif fault=='crossed':st.ticker['bid1Price']=102
    elif fault=='nan':st.ticker['bid1Price']=float('nan')
    elif fault=='malformed':st.ticker['bid1Price']='invalid'
    else:st.ticker={}
    row={'symbol':'ETH/USD','side':'long','size':1,'entry':90,'mark':99,'upnl':9,'value':99}
    result=mark_positions([row],store,now)[0]
    assert result['mark']==99 and result['upnl']==9 and result['mark_stale'] and result['mark_source']=='broker'


def test_aggregate_mark_labels_distinguish_flat_live_broker_and_mixed():
    live={'mark_source':'live_bid'};broker={'mark_source':'broker'}
    assert mark_basis([])=='flat' and mark_basis([live])=='live'
    assert mark_basis([broker])=='broker' and mark_basis([live,broker])=='mixed'


def test_mark_stream_key_changes_for_unwatched_holdings_and_quote_expiry():
    store=MarketStore(['ETH/USD','SPY']);now=time.time();st=store['ETH/USD'];st.book_ts=now;st.ticker={'bid1Price':100,'ask1Price':101}
    ex=SimpleNamespace(last_sync=now,positions=[{'symbol':'ETH/USD','side':'Buy','size':2}])
    original=mark_key(ex,store,now)
    st.ticker['bid1Price']=100.5
    assert mark_key(ex,store,now)!=original
    assert mark_key(ex,store,now+6)!=mark_key(ex,store,now)


def test_account_view_is_live_and_does_not_overwrite_execution_wallet(tmp_path):
    pos={'symbol':'BTCUSD','qty':'1','side':'long','avg_entry_price':'60000','current_price':'60001',
        'unrealized_pl':'1','market_value':'60001','qty_available':'1'}
    ex,b,db=executor(tmp_path,Broker([pos]));asyncio.run(ex.sync())
    ex.feed.store['BTC/USD'].book_ts=time.time();ex.feed.store['BTC/USD'].ticker={'bid1Price':60010,'ask1Price':60011}
    view=ex.account_view()
    assert view['positions'][0]['upnl']==10 and view['wallet']['unrealized_pnl']==10
    assert ex.wallet['unrealized_pnl']==1 and ex.positions[0]['markPrice']==60001
    assert view['daily_pnl'] is None and view['daily_pnl_available'] is False


def test_chart_fills_use_broker_fill_time_and_exclude_unfilled_and_foreign_orders(tmp_path):
    co,b,db,ex,did=candle(tmp_path)
    async def run():
        await ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0,horizon_min=30)
        intent=co.one('SELECT * FROM execution_intents WHERE effect=?',['increase'])
        assert chart_fills(db,co.account_id)==[]
        order={**intent['broker_json'],'status':'filled','filled_qty':'2','filled_avg_price':'100','filled_at':'2026-11-14T19:05:08Z'}
        co.update(intent['id'],{'status':'filled','broker_json':order})
        rows=chart_fills(db,co.account_id,'SPY')
        assert len(rows)==1 and rows[0]['at']==1794683108 and rows[0]['effect']=='entry' and rows[0]['qty']==2
        assert rows[0]['position_side']=='long'
        assert chart_fills(db,'other-account','SPY')==[] and chart_fills(db,co.account_id,'ETH/USD')==[]
    try:asyncio.run(run())
    finally:co.close()


def test_websocket_pushes_live_pnl_for_an_unwatched_position(tmp_path):
    pos={'symbol':'BTCUSD','qty':'1','side':'long','avg_entry_price':'60000','current_price':'60001',
        'unrealized_pl':'1','market_value':'60001','qty_available':'1'}
    ex,b,db=executor(tmp_path,Broker([pos]));ex.s.symbols=['SPY','BTC/USD']
    store=MarketStore(ex.s.symbols);ex.feed.store=store
    async def run():
        await ex.sync()
        st=store['BTC/USD'];st.book_ts=time.time();st.ticker={'bid1Price':60010,'ask1Price':60011}
        engine=SimpleNamespace(s=ex.s,store=store,executor=ex,feed=ex.feed,subscribers=set())
        app=make_app(engine);app.on_startup.clear();app.on_cleanup.clear()
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f'http://127.0.0.1:{port}/ws') as ws:
                    async def account():
                        for _ in range(20):
                            message=await ws.receive_json(timeout=2)
                            if message.get('kind')=='market' and 'account' in message['data']:return message['data']['account']
                        raise AssertionError('No account mark was pushed')
                    assert (await account())['positions'][0]['upnl']==10
                    st.ticker.update(bid1Price=60020,ask1Price=60021);st.book_ts=time.time()
                    assert (await account())['positions'][0]['upnl']==20
                    st.book_ts=time.time()-6
                    stale=(await account())['positions'][0]
                    assert stale['mark_stale'] and stale['upnl']==1
                    assert not b.submitted and ex.wallet['unrealized_pnl']==1
        finally:await runner.cleanup()
    asyncio.run(run())
