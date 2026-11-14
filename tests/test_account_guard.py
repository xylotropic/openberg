import asyncio
import time

import pytest

from ctb.broker.account_guard import observe, update, failure_file
from test_alpaca import executor, Broker
from test_session_execution import candle


def account(equity='100000', previous='0'):
    return {'id':'paper-test','equity':equity,'cash':equity,'last_equity':previous}


def test_zero_prior_close_bootstraps_once_and_exact_threshold_latches_across_restart(tmp_path):
    ex,b,db=executor(tmp_path)
    first=observe(db,ex.s,account(),now=100)
    assert first['anchor_equity']==100000 and first['source']=='first_observation' and not first['blocked']
    assert not observe(db,ex.s,account('97050'),now=101)['blocked']
    breach=observe(db,ex.s,account('97000'),now=102)
    assert breach['blocked'] and breach['breached_at']==102 and breach['breach_reference']=='100000'
    recovered=observe(db,ex.s,account('150000','120000'),now=103)
    assert recovered['blocked'] and recovered['anchor_equity']==100000 and recovered['breached_at']==102
    ex2,_,db2=executor(tmp_path)
    assert observe(db2,ex2.s,account('160000'),now=104)['blocked']


def test_valid_prior_close_keeps_protection_after_accumulated_profits(tmp_path):
    ex,b,db=executor(tmp_path);observe(db,ex.s,account(),now=100)
    current=observe(db,ex.s,account('115000','120000'),now=101)
    assert current['blocked'] and current['breach_reference']=='120000'
    assert current['breach_equity']=='115000' and current['breach_limit_pct']=='3.0'


@pytest.mark.parametrize('equity',['96000','0','-5'])
def test_prior_close_can_bootstrap_and_breach_on_first_observation(tmp_path,equity):
    ex,b,db=executor(tmp_path)
    result=observe(db,ex.s,account(equity,'100000'),now=100)
    assert result['blocked'] and result['source']=='prior_close'


@pytest.mark.parametrize('bad',[None,'bad','NaN','Infinity','-Infinity',True,'1e999'])
def test_invalid_amounts_fail_closed_without_inventing_a_reference(tmp_path,bad):
    ex,b,db=executor(tmp_path)
    result=update(db,ex.s,account(bad),now=100)
    assert result['blocked'] and not result['ready']
    assert db.one('SELECT * FROM account_loss_guards') is None
    assert failure_file(ex.s,'paper-test').exists()


def test_malformed_prior_close_is_not_treated_as_a_missing_zero(tmp_path):
    ex,b,db=executor(tmp_path)
    assert update(db,ex.s,account(previous='NaN'),now=100)['blocked']
    assert db.one('SELECT * FROM account_loss_guards') is None


def test_breach_write_failure_stays_blocked_after_recovery_and_restart(tmp_path):
    ex,b,db=executor(tmp_path);observe(db,ex.s,account(),now=100)
    original=db.conn
    class Broken:
        def execute(self,sql,*args):
            if sql.startswith('UPDATE account_loss_guards'):raise RuntimeError('simulated guard storage failure')
            return original.execute(sql,*args)
    db.conn=Broken()
    result=update(db,ex.s,account('96000'),now=101)
    assert result['blocked'] and not ex.s.trading_enabled
    db.conn=original
    assert observe(db,ex.s,account('120000'),now=102)['blocked']
    ex2,_,db2=executor(tmp_path)
    assert observe(db2,ex2.s,account('130000'),now=103)['blocked']


def test_account_scope_and_partial_broker_sync_cannot_create_foreign_reference(tmp_path):
    ex,b,db=executor(tmp_path)
    with pytest.raises(ValueError):observe(db,ex.s,{**account(),'id':'foreign'},now=100)
    assert db.one('SELECT * FROM account_loss_guards') is None
    b.pos=[{'symbol':'BTCUSD','qty':'bad','side':'long','avg_entry_price':'100','current_price':'100','unrealized_pl':'0','market_value':'100'}]
    asyncio.run(ex.sync())
    assert ex.sync_error and db.one('SELECT * FROM account_loss_guards') is None
    assert ex.account=={} and ex.wallet.get('equity') is None


def test_guard_storage_failure_does_not_abort_broker_start_or_hide_positions(tmp_path):
    pos={'symbol':'BTCUSD','qty':'1','side':'long','avg_entry_price':'100','current_price':'100','unrealized_pl':'0','market_value':'100'}
    ex,b,db=executor(tmp_path,Broker([pos]));db.conn.execute('DROP TABLE account_loss_guards')
    asyncio.run(ex.start())
    assert ex.sync_error is None and len(ex.positions)==1 and ex.loss_guard['blocked']
    assert ex.account_view()['loss_guard']['blocked']


def test_sync_latches_while_paused_and_account_equity_survives_restart(tmp_path):
    ex,b,db=executor(tmp_path);asyncio.run(ex.sync());ex.s.trading_enabled=False
    async def lower():return {**account('96000'),'buying_power':'100000'}
    b.account=lower;asyncio.run(ex.sync())
    assert ex.loss_guard['blocked'] and db.one('SELECT * FROM account_equity_snapshots')['equity']==96000
    ex.s.trading_enabled=True
    assert ex.check_guards('BTC/USD') and ex.loss_guard['breached_at']


def test_final_dispatch_rechecks_a_breach_committed_during_entry_preparation(tmp_path):
    co,b,db,ex,did=candle(tmp_path)
    original_clock=co.clock
    calls=0
    async def clock():
        nonlocal calls
        calls+=1
        result=await original_clock()
        if calls>1:
            observe(db,ex.s,{'id':ex.s.alpaca_account_id,'equity':'96000','cash':'96000'},now=time.time())
        return result
    co.clock=clock
    try:
        result=asyncio.run(ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0,horizon_min=30))
        assert result.get('never_submitted') and not b.sent
        assert co.one('SELECT * FROM execution_intents')['attempted_at'] is None
    finally:co.close()


def test_twenty_percent_allocation_reaches_the_canonical_dispatch_and_run_snapshot(tmp_path):
    from ctb.runs import config_snapshot
    co,b,db,ex,did=candle(tmp_path);ex.s.scalper_allocation_pct=20
    try:
        asyncio.run(ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0,horizon_min=30))
        assert len(b.sent)==1
        notional=float(b.sent[0]['qty'])*float(b.sent[0]['limit_price'])
        assert 19000 < notional <= 20000
        assert config_snapshot(ex.s)['allocation_pct_per_position']==20
    finally:co.close()


@pytest.mark.parametrize('invalid',[0,-1,101,float('nan'),float('inf')])
def test_invalid_allocation_cannot_reach_a_broker_submit(tmp_path,invalid):
    co,b,db,ex,did=candle(tmp_path);ex.s.scalper_allocation_pct=invalid
    try:
        result=asyncio.run(ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0))
        assert not result['ok'] and not b.sent
    finally:co.close()


def test_a_latched_account_guard_does_not_block_an_owned_exit(tmp_path):
    from test_alpaca import add_trade
    pos={'symbol':'BTCUSD','qty':'1','side':'long','avg_entry_price':'60000','current_price':'60000','unrealized_pl':'0','market_value':'60000','qty_available':'1'}
    ex,b,db=executor(tmp_path,Broker([pos]));asyncio.run(ex.sync());add_trade(db)
    ex.loss_guard=observe(db,ex.s,account('96000'),now=time.time())
    assert ex.loss_guard['blocked']
    asyncio.run(ex.close('BTC/USD','time_stop'))
    assert any(x['type']=='market' and x['side']=='sell' for x in b.submitted)


def test_cached_dispatch_check_cannot_retimestamp_or_overwrite_equity_history(tmp_path):
    ex,b,db=executor(tmp_path)
    observe(db,ex.s,{**account(),'_observed_at':100},now=100)
    observe(db,ex.s,{**account('101000'),'_observed_at':110},now=110)
    observe(db,ex.s,{**account('99500'),'_observed_at':100},now=115,record_snapshot=False)
    assert db.one('SELECT * FROM account_equity_snapshots')['equity']==101000
    assert db.one('SELECT * FROM account_equity_snapshots')['ts']==110
    observe(db,ex.s,{**account('99500'),'_observed_at':100},now=120)
    assert db.one('SELECT * FROM account_equity_snapshots')['equity']==101000
