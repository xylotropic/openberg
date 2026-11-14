import asyncio
import time
from types import SimpleNamespace

import aiohttp
from aiohttp import web

from ctb.broker.equity import equity_series
from ctb.server import make_app


def test_recorded_equity_keeps_true_zero_and_negative_balances():
    series = equity_series({'timestamp': [1, 2, 3, 4, 5], 'equity': [0, 0, 100, 0, -5]}, now=10)
    assert series == {'points': [(1, 0), (2, 0), (3, 100), (4, 0), (5, -5)]}
    assert equity_series({'timestamp': [1, 2], 'equity': [0, 0]}, now=10)['points'] == [(1, 0), (2, 0)]


def test_equity_sorts_deduplicates_and_rejects_invalid_or_future_values():
    raw = {'timestamp': [4, 3, 2, True, 5, 50, 6], 'equity': [101, 100, float('nan'), 10, False, 200, None]}
    assert equity_series(raw, now=10)['points'] == [(3, 100), (4, 101)]
    assert equity_series(raw, now=10, current=(4.5, 102))['points'] == [(3, 100), (4, 102)]
    assert equity_series(raw, now=10, current=(2, 99))['points'][-1] == (4, 101)


def test_current_funded_equity_can_start_an_empty_history_without_zero_jump():
    result = equity_series({}, now=10, current=(9, 100))
    assert result['points'] == [(9, 100)]


def test_old_same_second_overlay_and_out_of_order_rows_cannot_replace_a_newer_value():
    raw={'timestamp':[10.9,10.1],'equity':[102,101]}
    assert equity_series(raw,now=20,current=(10.2,100))['points']==[(10,102)]
    assert equity_series(raw,now=20,current=(10.95,103))['points']==[(10,103)]


def test_equity_api_reads_recorded_account_scoped_history_and_no_broker_backfill(tmp_path):
    from ctb.db import DB
    async def run():
        db=DB(tmp_path/'curve.db');now=time.time()
        db.conn.executemany('INSERT INTO account_equity_snapshots VALUES(?,?,?,?,?)',[
            ('test',int((now-120)//60),now-120,100,100),
            ('other',int((now-120)//60),now-120,200000,200000)])
        ex=SimpleNamespace(account_id='test',sync_error=None,last_sync=now,wallet={'equity':101})
        engine=SimpleNamespace(s=SimpleNamespace(venue='alpaca'),executor=ex,db=db)
        app=make_app(engine);app.on_startup.clear();app.on_cleanup.clear()
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                async def read(period='1D'):
                    async with session.get(f'http://127.0.0.1:{port}/api/equity-curve?period={period}') as response:
                        return response.status,await response.json()
                status,first=await read()
                assert status==200 and first['points'][0][1]==100 and first['points'][-1][1]==101
                ex.wallet['equity']=102
                assert (await read())[1]['points'][-1][1]==102
                ex.last_sync=time.time()-61
                assert [p[1] for p in (await read())[1]['points']]==[100]
                assert (await read('BAD'))[0]==400
                assert first['source']=='Recorded Alpaca account snapshots'
        finally:await runner.cleanup()
    asyncio.run(run())
