"""Session strategy boundaries: time alignment, spend, and owned actions."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from ctb.db import DB
from ctb.kairos.client import RateBudget, KairosResult
from ctb.market.store import MarketStore, Bar
from ctb.scalper import RESERVE, PRICE, SYMBOLS, EQUITIES, CRYPTO, VERSION, Scalper, Spend, packet, questions


def history(symbol='TSLA', end=None):
    now=end or int(time.time()//900)*900+61
    store=MarketStore(SYMBOLS);st=store[symbol]
    start=int(now//60)*60000-500*60000
    for i in range(500):
        close=100+i/100
        st.upsert_bar('1',Bar(start+i*60000,close,close+.1,close-.1,close+.02,10+i, (10+i)*(close+.01)))
    st.book_ts=now;st.last_update=now;st.ticker={'bid1Price':105,'ask1Price':105.01}
    return store,now


CAP=2.0


def budget(tmp_path, name='test.db'):
    db=DB(tmp_path/name);rate=RateBudget(200);rate.bind(db,'primary')
    return Spend(db,rate,monthly_cap=CAP)


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


def test_all_stocks_support_target_directions():
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
    for sym in SYMBOLS:
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
    from ctb.runs import config_snapshot
    cfg=config_snapshot(s)
    assert cfg['qset']==VERSION and cfg['allocation_pct_per_position']==5 and cfg['horizon_minutes']==30
    assert s.required_rph()==120


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
    strategy.client=Client()
    async def run():
        await strategy.tick()
        await asyncio.gather(*strategy.executions.values())
    asyncio.run(run())
    assert not strategy.e.executor.opened
    assert all(d['action']=='hold' for d in strategy.db.query('SELECT action FROM decisions'))


def test_session_exit_runs_when_model_and_entries_are_off(tmp_path):
    strategy,_=service(tmp_path);owned(strategy,'BTC/USD')
    strategy.e.s.trading_enabled=False
    class Client: configured=False
    strategy.client=Client()
    async def run():
        await strategy.tick()
        await asyncio.gather(*strategy.executions.values())
    asyncio.run(run())
    assert strategy.e.executor.closed==[('BTC/USD','session_switch')]
    assert strategy.db.one('SELECT COUNT(*) n FROM scalper_calls')['n']==0


def test_exit_also_cancels_an_owned_opening_trade(tmp_path):
    strategy,now=service(tmp_path);owned(strategy,'TSLA',status='opening')
    asyncio.run(strategy.apply('TSLA','EXIT',.9,strategy.build(now),result(6000,{'TSLA':{}}),'test'))
    assert strategy.e.executor.closed==[('TSLA','drex_exit')]


def test_session_run_metrics_preserve_unknown_request_sizes(tmp_path):
    from ctb.evaluation.metrics import compute
    strategy,now=service(tmp_path)
    asyncio.run(strategy.apply('TSLA','HOLD',.9,strategy.build(now),result(6000,{'TSLA':{}}),'test'))
    assert compute(strategy.db)['ops']['request_bytes_mean'] is None
    strategy.db.conn.execute('UPDATE decisions SET request_bytes=1234')
    assert compute(strategy.db)['ops']['request_bytes_mean']==1234


def test_requested_cadence_and_restart_reservations(tmp_path,monkeypatch):
    strategy,now=service(tmp_path);sent=[];observed=[];clock=[now]
    monkeypatch.setattr('ctb.scalper.time.time',lambda:clock[0])
    class Client:
        configured=True
        async def ask(self,state,qs):
            sent.append((clock[0],set(qs)))
            return result(5000,{s:{'type':'choice','choice':'HOLD','probabilities':{k:1.0 if k=='HOLD' else 0.0 for k in qs[s]['criteria']}} for s in qs})
    strategy.client=Client()
    strategy.build=lambda now:{'as_of':now,'symbols':{s:{} for s in (EQUITIES if strategy.e.feed.clock['is_open'] else CRYPTO)}}
    async def apply(s,*_,**__): observed.append(s)
    strategy.apply=apply
    async def run():
        await strategy.tick();await asyncio.sleep(0)
        assert sent[-1][1]==set(EQUITIES)
        clock[0]=now+29;await strategy.tick();assert len(sent)==1
        clock[0]=now+30;await strategy.tick();assert sent[-1][1]=={'SPY'}
        restarted=Scalper(strategy.e);restarted.restore_schedule()
        assert restarted.last_sent['SPY']==now+30 and restarted.last_sent['TSLA']==now
        clock[0]=now+600;await strategy.tick();assert sent[-1][1]==set(EQUITIES)
        strategy.e.feed.clock={'is_open':False}
        clock[0]=now+630;await strategy.tick();assert sent[-1][1]==set(CRYPTO)
        clock[0]=now+660;await strategy.tick();assert sent[-1][1]==set(CRYPTO)
        await strategy.stop()
    asyncio.run(run())


def test_slow_execution_does_not_block_model_and_is_bounded_per_symbol(tmp_path,monkeypatch):
    strategy,now=service(tmp_path);clock=[now];entered=[]
    monkeypatch.setattr('ctb.scalper.time.time',lambda:clock[0])
    class Client:
        configured=True
        async def ask(self,state,qs): return result(5000,{'SPY':{'type':'choice','choice':'HOLD','probabilities':{'LONG':0,'SHORT':0,'HOLD':1,'EXIT':0}}})
    strategy.client=Client();strategy.build=lambda now:{'as_of':now,'symbols':{'SPY':{}}}
    async def run():
        gate=asyncio.Event()
        async def apply(symbol,*_,entry_busy=False):
            entered.append(entry_busy)
            if not entry_busy: await gate.wait()
        strategy.apply=apply
        await strategy.tick();await asyncio.sleep(0)
        clock[0]+=30
        await asyncio.wait_for(strategy.tick(),1);await asyncio.sleep(0)
        assert entered==[False,True]
        assert strategy.db.one('SELECT COUNT(*) n FROM scalper_calls')['n']==2
        await strategy.stop()
        assert strategy.executions['SPY'].cancelled()
    asyncio.run(run())


def test_prepaid_mode_removes_monthly_cap_and_billing_failure_survives_restart(tmp_path,monkeypatch):
    from ctb.kairos.client import KairosError
    strategy,now=service(tmp_path);strategy.e.feed.clock={'is_open':False};clock=[now];calls=[]
    monkeypatch.setattr('ctb.scalper.time.time',lambda:clock[0])
    strategy.db.conn.execute('INSERT INTO scalper_calls(id,version,started_at,request_json,state,reserved_usd) VALUES(?,?,?,?,?,?)',['old',VERSION,now-3600,'{}','complete',10])
    assert strategy.spend.monthly_cap is None
    class Client:
        configured=True
        async def ask(self,*_): calls.append(1);raise KairosError('billing','insufficient_credit',402)
    strategy.client=Client()
    strategy.build=lambda now:{'as_of':now,'symbols':{'BTC/USD':{}}}
    async def run():
        with pytest.raises(ValueError,match='insufficient_credit'): await strategy.tick()
        clock[0]+=30
        await strategy.tick()
        reopened=Scalper(strategy.e)
        assert reopened.state('credit_blocked') and len(calls)==1
    asyncio.run(run())


@pytest.mark.parametrize('status',[401,402,403,429])
def test_primary_fallback_honors_all_durable_credential_restrictions(tmp_path,monkeypatch,status):
    from ctb.kairos.client import KairosError
    strategy,now=service(tmp_path);clock=[now];calls=[]
    monkeypatch.setattr('ctb.scalper.time.time',lambda:clock[0])
    class Client:
        configured=True
        async def ask(self,*_): calls.append(1);raise KairosError('unavailable','blocked',status)
    strategy.client=Client();strategy.build=lambda now:{'as_of':now,'symbols':{'SPY':{}}}
    async def run():
        with pytest.raises(ValueError): await strategy.tick()
        clock[0]+=30;await strategy.tick();assert len(calls)==1
        reopened=Scalper(strategy.e);reopened.client=Client();reopened.build=strategy.build
        clock[0]+=30;await reopened.tick();assert len(calls)==1
        clock[0]=now+301
        if status==429:
            with pytest.raises(ValueError): await reopened.tick()
            assert len(calls)==2
        else:
            await reopened.tick();assert len(calls)==1
    asyncio.run(run())
