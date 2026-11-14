"""Seven-stock strategy boundaries: time alignment, spend, and owned actions."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from ctb.db import DB
from ctb.kairos.client import RateBudget, KairosResult
from ctb.market.store import MarketStore, Bar
from ctb.scalper import CAP, RESERVE, PRICE, SYMBOLS, EQUITIES, CRYPTO, VERSION, Scalper, Spend, packet, questions


def history(symbol='TSLA', end=None):
    now=end or int(time.time()//900)*900+61
    store=MarketStore(SYMBOLS);st=store[symbol]
    start=int(now//60)*60000-500*60000
    for i in range(500):
        close=100+i/100
        st.upsert_bar('1',Bar(start+i*60000,close,close+.1,close-.1,close+.02,10+i, (10+i)*(close+.01)))
    st.book_ts=now;st.last_update=now;st.ticker={'bid1Price':105,'ask1Price':105.01}
    return store,now


def budget(tmp_path, name='test.db'):
    db=DB(tmp_path/name);rate=RateBudget(200);rate.bind(db,'primary')
    return Spend(db,rate)


def result(tokens=6000, answers=None):
    return KairosResult(answers or {},{'input_tokens':tokens},{'model':'drex-max-finance'},10,1,100,{'usage':{'input_tokens':tokens}})


def test_packet_windows_are_closed_aligned_and_use_minutes():
    store,now=history();st=store['TSLA'];p=packet(st,now)
    assert [len(p['closed_'+tf]) for tf in ('1m','5m','15m')]==[30,12,8]
    assert p['closed_15m'][-1][0]+900<=now
    assert p['closed_5m'][-1][0]+300<=now
    closes=st.closes('1'); assert p['indicators']['return_60m_pct']==pytest.approx(100*(closes[-1]/closes[-61]-1),abs=1e-6)
    expected=sum(b.turnover for b in st.closed_bars('1')[-60:])/sum(b.v for b in st.closed_bars('1')[-60:])
    assert p['indicators']['vwap60']==pytest.approx(expected,abs=1e-6)
    st.upsert_bar('1',Bar(int(now//60)*60000,500,600,400,550,100,55000,False))
    assert packet(st,now)==p


@pytest.mark.parametrize('fault',['gap','stale','nan','zero_volume','bad_ohlc'])
def test_packet_refuses_incomplete_or_invalid_inputs(fault):
    store,now=history();st=store['TSLA']
    if fault=='gap': del st.bars['1'][-40]
    elif fault=='stale': now+=200
    elif fault=='nan': st.bars['1'][-1].c=float('nan')
    elif fault=='zero_volume':
        for b in st.bars['1'][-60:]: b.v=0;b.turnover=0
    else: st.bars['1'][-1].h=1
    with pytest.raises(ValueError): packet(st,now)


def test_all_seven_stocks_support_target_directions():
    q=questions(SYMBOLS)
    assert all(set(q[s]['criteria'])=={'LONG','SHORT','HOLD','EXIT'} for s in EQUITIES)
    assert all(set(q[s]['criteria'])=={'LONG','HOLD','EXIT'} for s in CRYPTO)


def test_monthly_reservations_survive_restart_and_never_exceed_cap(tmp_path):
    spend=budget(tmp_path)
    count=int(CAP//RESERVE)
    for _ in range(count):
        # Separate hours avoid testing the independent hourly ceiling here.
        spend.hourly.per_hour=1000;spend.reserve({},questions(['MSFT']))
    assert spend.spent()==pytest.approx(count*RESERVE)
    reopened=budget(tmp_path)
    with pytest.raises(ValueError,match='Monthly'): reopened.reserve({},questions(['MSFT']))
    assert reopened.spent()<=CAP


def test_success_releases_unused_reserve_but_unknown_usage_does_not(tmp_path):
    spend=budget(tmp_path);identity=spend.reserve({},questions(['MSFT']));spend.settle(identity,result(6000))
    assert spend.spent()==pytest.approx(6000*PRICE)
    identity=spend.reserve({},questions(['MSFT']));spend.settle(identity,result(None))
    assert spend.spent()==pytest.approx(6000*PRICE+RESERVE)


def test_month_rollover_and_pacing(tmp_path):
    spend=budget(tmp_path);old=1767225600
    spend.reserve({},questions(['MSFT']),now=old)
    assert spend.spent(now=old)==pytest.approx(RESERVE)
    assert spend.spent(now=old+32*86400)==0
    assert spend.interval()>=30


def test_budget_reservation_is_atomic_across_two_connections(tmp_path):
    first=budget(tmp_path);second=budget(tmp_path)
    with first.db.lock:
        first.db.conn.execute('INSERT INTO scalper_calls(id,version,started_at,request_json,state,reserved_usd) VALUES(?,?,?,?,?,?)',
            ['spent',VERSION,time.time(),'{}','reserved',CAP-RESERVE*1.5])
    first.reserve({},questions(['MSFT']))
    with pytest.raises(ValueError,match='Monthly'): second.reserve({},questions(['MSFT']))


def service(tmp_path):
    store,now=history(end=time.time())
    for sym in SYMBOLS[1:]:
        other,_=history(sym,now);store.state[sym]=other[sym]
    spend=budget(tmp_path);db=spend.db
    class Executor:
        def __init__(self): self.opened=[];self.closed=[]
        async def open(self,**kwargs): self.opened.append(kwargs);return {'ok':True}
        async def close(self,symbol,reason,**kwargs): self.closed.append((symbol,reason));return True
    engine=SimpleNamespace(db=db,budget=spend.hourly,session=None,store=store,
        s=SimpleNamespace(symbols=SYMBOLS,alpaca_account_id='paper-test',trading_enabled=True),
        feed=SimpleNamespace(clock={'is_open':True}),executor=Executor(),run={'id':1},emit=lambda *_:None,_decision_view=lambda d:d)
    strategy=Scalper(engine)
    async def clock(): return engine.feed.clock
    engine.coordinator=SimpleNamespace(clock=clock)
    return strategy,now


def owned(strategy, symbol='TSLA', status='open'):
    did=strategy.db.insert('decisions',{'ts':time.time(),'symbol':symbol,'qset_version':VERSION})
    return strategy.db.insert('trades',{'venue':'alpaca','account_id':'paper-test','symbol':symbol,'status':status,'side':'long','decision_id':did})


def test_repeated_long_keeps_owned_position_without_adding(tmp_path):
    strategy,now=service(tmp_path);owned(strategy)
    state=strategy.build(now)
    asyncio.run(strategy.apply('TSLA','LONG',.99,state,result(6000,{'TSLA':{}}),'test'))
    assert strategy.e.executor.opened==[] and strategy.e.executor.closed==[]


def test_exit_closes_only_proven_strategy_position(tmp_path):
    strategy,now=service(tmp_path);state=strategy.build(now)
    strategy.db.insert('trades',{'account_id':'paper-test','symbol':'TSLA','status':'open','side':'long'})
    asyncio.run(strategy.apply('TSLA','EXIT',.9,state,result(6000,{'TSLA':{}}),'test'))
    assert strategy.e.executor.closed==[]
    owned(strategy)
    asyncio.run(strategy.apply('TSLA','EXIT',.9,state,result(6000,{'TSLA':{}}),'test'))
    assert len(strategy.e.executor.closed)==1


def test_opposite_signal_closes_without_reversing(tmp_path):
    strategy,now=service(tmp_path);owned(strategy,'MSFT');state={'as_of':now,'symbols':{'MSFT':packet(strategy.e.store['MSFT'],now)}}
    asyncio.run(strategy.apply('MSFT','SHORT',.99,state,result(6000,{'MSFT':{}}),'test'))
    assert strategy.e.executor.closed==[('MSFT','drex_opposite')]
    assert strategy.e.executor.opened==[]


def test_old_bar_callbacks_make_no_legacy_model_calls_when_enabled(tmp_path):
    from ctb.config import Settings
    from ctb.engine import Engine
    s=Settings();s.db_path=tmp_path/'engine.db';s.scalper_enabled=True
    e=Engine(s)
    assert asyncio.run(e.decide('TSLA')) is None


def test_stale_quote_and_closed_spy_are_not_sent(tmp_path):
    strategy,now=service(tmp_path)
    strategy.e.store['AAPL'].book_ts=now-6
    assert 'AAPL' not in strategy.build(now)['symbols']
    strategy.e.feed.clock={'is_open':False}
    state=strategy.build(now)
    assert set(state['symbols'])==set(CRYPTO)
    assert 'closed' in strategy.readiness['MSFT']


def test_invalid_distribution_does_not_trade_and_keeps_cost(tmp_path):
    strategy,now=service(tmp_path)
    class Client:
        configured=True
        async def ask(self,state,qs): return result(5000,{s:{'type':'choice','choice':'LONG','probabilities':{'LONG':1}} for s in qs})
    strategy.client=Client()
    # Use a real current as-of so expiry checks cannot explain the result.
    strategy.build=lambda _: {'as_of':time.time(),'symbols':{'TSLA':{}}}
    with pytest.raises(ValueError,match='choice'): asyncio.run(strategy.tick())
    assert strategy.e.executor.opened==[]
    assert strategy.spend.spent()==pytest.approx(5000*PRICE)


def test_clock_switches_stock_and_crypto_universes(tmp_path):
    strategy,now=service(tmp_path)
    assert set(strategy.build(now)['symbols'])==set(EQUITIES)
    strategy.e.feed.clock={'is_open':False}
    assert set(strategy.build(now)['symbols'])==set(CRYPTO)


def test_session_flip_during_model_request_cannot_open_crypto(tmp_path):
    strategy,_=service(tmp_path);now=time.time()
    for s in SYMBOLS:
        store,_=history(s,end=now);strategy.e.store.state[s]=store[s]
    strategy.e.feed.clock={'is_open':False,'next_open':'2099-01-01T00:00:00Z'}
    async def asset(_): return {'tradable':True,'shortable':True,'easy_to_borrow':True}
    strategy.e.executor.client=SimpleNamespace(asset=asset)
    class Client:
        configured=True
        async def ask(self,state,qs):
            assert set(qs)==set(CRYPTO)
            strategy.e.feed.clock={'is_open':True,'next_close':'2099-01-01T00:00:00Z'}
            return result(6000,{s:{'type':'choice','choice':'LONG','probabilities':{'LONG':.99,'HOLD':.005,'EXIT':.005}} for s in qs})
    strategy.client=Client();asyncio.run(strategy.tick())
    assert not strategy.e.executor.opened
    assert all(d['action']=='hold' for d in strategy.db.query('SELECT action FROM decisions'))


def test_session_exit_runs_when_model_and_entries_are_off(tmp_path):
    strategy,_=service(tmp_path);owned(strategy,'BTC/USD')
    strategy.e.s.trading_enabled=False
    class Client: configured=False
    strategy.client=Client();asyncio.run(strategy.tick())
    assert strategy.e.executor.closed==[('BTC/USD','session_switch')]
    assert strategy.db.one('SELECT COUNT(*) n FROM scalper_calls')['n']==0


def test_exit_also_cancels_an_owned_opening_trade(tmp_path):
    strategy,now=service(tmp_path);owned(strategy,'TSLA',status='opening')
    asyncio.run(strategy.apply('TSLA','EXIT',.9,strategy.build(now),result(6000,{'TSLA':{}}),'test'))
    assert strategy.e.executor.closed==[('TSLA','drex_exit')]
