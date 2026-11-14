"""Synthetic capital accounting and real canonical-dispatch regressions."""
import asyncio
import copy
import time

import pytest

from ctb.broker.capital import remaining_capital
from ctb.runs import config_snapshot
from test_session_execution import candle
from test_alpha import holding


def snapshot(cash=100000, equity=100000, positions=None, power=300000):
    return {'account':{'equity':str(equity),'cash':str(cash),'buying_power':str(power)},
            'positions':positions or [],'orders':[]}


def intent(*, identity='entry', status='new', qty=500, filled=0, seen=0, notional=50000, effect='increase'):
    return {'id':identity,'client_order_id':identity,'request_json':{'qty':str(qty)},
            'effect':effect,'status':status,'broker_json':{'filled_qty':str(filled),'filled_avg_price':'100'},
            'inventory_seen_qty':str(seen),'notional':notional}


def test_sequential_halves_use_remaining_cash_without_margin():
    positions=[];cash=100000;entries=[]
    for symbol in ['SPY','TSLA','AAPL']:
        free=remaining_capital(snapshot(cash=cash,positions=positions),[],[])['available_capital']
        entries.append(free*.5)
        positions.append(holding(symbol,free*.5/100));cash-=free*.5
    assert entries==[50000,25000,12500] and cash==12500


def test_long_mark_changes_do_not_spend_unrealized_profit_and_close_recycles_actual_cash():
    pos=holding('SPY',500);pos['market_value']='55000'
    assert remaining_capital(snapshot(cash=50000,equity=105000,positions=[pos]),[],[])['available_capital']==50000
    # The broker has booked a profitable exit; its cash now includes the proceeds.
    assert remaining_capital(snapshot(cash=104950,equity=104950),[],[])['available_capital']*.5==52475


@pytest.mark.parametrize('market,equity,expected',[(50000,100000,50000),(45000,105000,50000),(55000,95000,40000)])
def test_short_proceeds_and_unrealized_profit_never_create_free_cash(market,equity,expected):
    pos=holding('SPY',-500);pos['market_value']=str(-market)
    assert remaining_capital(snapshot(cash=150000,equity=equity,positions=[pos]),[],[])['available_capital']==expected


@pytest.mark.parametrize('visible',[False,True])
def test_pending_entry_is_reserved_exactly_once_in_capital_pool(visible):
    s=snapshot();i=intent()
    if visible:s['orders']=[{'client_order_id':i['client_order_id']}]
    result=remaining_capital(s,[i],[])
    assert result['pending_entries']==50000 and result['available_capital']==50000
    assert remaining_capital(s,[i],[],exclude_id=i['id'])['available_capital']==100000


@pytest.mark.parametrize('status,filled,seen,expected',[
    ('partially_filled',100,100,50000),('partially_filled',100,0,40000),
    ('filled',500,0,40000),('canceled',100,0,80000),('canceled',100,100,90000),
    ('rejected',0,0,90000),('expired',0,0,90000)])
def test_partial_and_terminal_fills_keep_only_unseen_capital_reserved(status,filled,seen,expected):
    s=snapshot(cash=90000,positions=[holding('SPY',100)])
    r=remaining_capital(s,[intent(status=status,filled=filled,seen=seen)],[])
    assert r['available_capital']==expected


def test_crypto_waits_for_settlement_and_other_owners_keep_their_capital():
    s=snapshot(cash=100000);s['account']['non_marginable_buying_power']='12000'
    assert remaining_capital(s,[],[],crypto=True)['available_capital']==12000
    assert remaining_capital(s,[],[{'amount':10000}],crypto=True)['available_capital']==2000
    s['account']['non_marginable_buying_power']='0'
    assert remaining_capital(s,[],[],crypto=True)['available_capital']==0


def test_missing_crypto_capacity_cannot_fall_back_to_cash():
    with pytest.raises(ValueError):remaining_capital(snapshot(),[],[],crypto=True)


def test_visible_order_with_lagging_capacity_remains_reserved():
    s=snapshot(power=20000);i=intent(qty=150,notional=15000)
    s['orders']=[{'client_order_id':i['client_order_id']}]
    assert remaining_capital(s,[i],[])['available_capital']==5000


def test_unseen_short_fill_above_sizing_price_reserves_actual_execution_value():
    i=intent(status='filled',filled=500);i['broker_json']['filled_avg_price']='105'
    assert remaining_capital(snapshot(),[i],[])['pending_entries']==52500


def test_protection_does_not_reserve_entry_capital_and_foreign_orders_block():
    s=snapshot(cash=50000,positions=[holding('SPY',500)]);i=intent(effect='protect')
    s['orders']=[{'client_order_id':i['client_order_id']}]
    assert remaining_capital(s,[i],[])['available_capital']==50000
    s['orders'].append({'client_order_id':'foreign'})
    with pytest.raises(ValueError,match='Unowned'):remaining_capital(s,[i],[])


@pytest.mark.parametrize('bad',[None,'bad','NaN','Infinity',True,'1e999'])
def test_invalid_broker_amounts_fail_closed(bad):
    s=snapshot();s['account']['cash']=bad
    with pytest.raises(ValueError):remaining_capital(s,[],[])


@pytest.mark.parametrize('field,bad',[('notional',float('nan')),('inventory_seen_qty','501')])
def test_invalid_reservations_fail_closed(field,bad):
    i=intent();i[field]=bad
    with pytest.raises(ValueError):remaining_capital(snapshot(),[i],[])


@pytest.mark.parametrize('cash',[50000,150000])
def test_a_later_missing_inventory_snapshot_cannot_reuse_previously_seen_fill(cash):
    s=snapshot(cash=cash)
    with pytest.raises(ValueError,match='snapshots disagree'):
        remaining_capital(s,[intent(status='filled',filled=500,seen=500)],[])


def test_terminal_without_fill_evidence_blocks_but_unsent_rejection_releases():
    i=intent(status='canceled');i['attempted_at']=1;i['broker_json']={}
    with pytest.raises(ValueError,match='fill evidence'):remaining_capital(snapshot(),[i],[])
    i['status']='rejected';i['broker_json']=None
    assert remaining_capital(snapshot(),[i],[])['available_capital']==100000


@pytest.mark.parametrize('new_filled',[None,'0','99'])
def test_broker_snapshot_cannot_drop_cumulative_fill_proof(new_filled):
    from ctb.broker.coordinator import BrokerCoordinator
    i=intent(filled=100)
    order={'client_order_id':i['client_order_id'],'status':'canceled'}
    if new_filled is not None:order['filled_qty']=new_filled
    with pytest.raises(ValueError):BrokerCoordinator.validate_snapshot(order,i)


def configure(co,b,ex,*,cash=50000):
    ex.s.scalper_allocation_pct=50;ex.s.scalper_allocation_basis='remaining_capital'
    b.pos=[holding('NVDA',500)]
    async def account():
        return {'id':b.account_id,'equity':'100000','cash':str(cash),'buying_power':'300000','options_buying_power':'100000'}
    b.account=account


def test_remaining_capital_reaches_real_executor_and_canonical_cap(tmp_path):
    co,b,db,ex,did=candle(tmp_path);configure(co,b,ex)
    try:
        asyncio.run(ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0,horizon_min=30))
        assert len(b.sent)==1
        n=float(b.sent[0]['qty'])*float(b.sent[0]['limit_price'])
        assert 24000<n<=25000
        sizing=db.one('SELECT sizing_json FROM trades')['sizing_json']
        assert sizing['available_capital']==50000 and sizing['target_notional']==25000
        cfg=config_snapshot(ex.s)
        assert cfg['allocation_basis']=='remaining_capital' and cfg['allocation_pct_per_position']==50
    finally:co.close()


def test_capital_shrink_after_sizing_cannot_reach_submit(tmp_path):
    co,b,db,ex,did=candle(tmp_path);configure(co,b,ex)
    original=co.dispatch
    async def shrink(identity):
        b.pos=[holding('NVDA',900)]
        return await original(identity)
    co.dispatch=shrink
    try:
        result=asyncio.run(ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0))
        assert result['never_submitted'] and not b.sent
        assert co.one('SELECT * FROM execution_intents')['attempted_at'] is None
        assert co.one('SELECT * FROM execution_intents')['status']=='rejected'
    finally:co.close()


def test_owned_inventory_cannot_disappear_even_if_the_account_snapshot_is_also_stale(tmp_path):
    from test_alpaca import add_trade
    co,b,db,ex,did=candle(tmp_path)
    ex.s.scalper_allocation_basis='remaining_capital'
    add_trade(db,account_id=co.account_id,symbol='TSLA',qty=500,side='short')
    try:
        asyncio.run(co.refresh(force=True))
        with pytest.raises(ValueError,match='owned position'):co.session_allocation('SPY')
    finally:co.close()


def test_invalid_basis_blocks_submit_and_snapshot(tmp_path):
    co,b,db,ex,did=candle(tmp_path);ex.s.scalper_allocation_basis='margin'
    try:
        result=asyncio.run(ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0))
        assert not result['ok'] and not b.sent
        with pytest.raises(ValueError):config_snapshot(ex.s)
    finally:co.close()


def test_concurrent_signals_reserve_an_ambiguous_first_entry_before_sizing_second(tmp_path):
    from test_scalper import history
    from ctb.scalper import packet
    co,b,db,ex,first=candle(tmp_path)
    ex.s.scalper_allocation_pct=50;ex.s.scalper_allocation_basis='remaining_capital'
    ex.s.symbols=['SPY','TSLA'];b.timeout=True
    store,now=history('TSLA',end=time.time());ex.feed.store.state['TSLA']=store['TSLA']
    original=db.one('SELECT * FROM decisions WHERE id=?',[first])
    dj=copy.deepcopy(original['decision_json']);answer=copy.deepcopy(original['answers_json']['SPY'])
    call=db.one('SELECT * FROM scalper_calls WHERE id=?',[dj['call_id']])
    call['response_json']['answers']['TSLA']=answer
    db.conn.execute('UPDATE scalper_calls SET response_json=? WHERE id=?',
        [__import__('json').dumps(call['response_json']),call['id']])
    second=db.insert('decisions',{'ts':now,'symbol':'TSLA','qset_version':original['qset_version'],
        'action':'long','state_json':packet(store['TSLA'],now),'answers_json':{'TSLA':answer},'decision_json':dj})
    async def run():
        await asyncio.gather(*(ex.open(symbol=symbol,side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0)
                              for symbol,did in [('SPY',first),('TSLA',second)]))
    try:
        asyncio.run(run())
        assert len(b.sent)==2
        sizes=[float(body['qty'])*float(body['limit_price']) for body in b.sent]
        assert 49000<sizes[0]<=50000 and 24000<sizes[1]<=25100
        records=db.query('SELECT sizing_json FROM trades ORDER BY id')
        assert records[0]['sizing_json']['available_capital']==100000
        assert records[1]['sizing_json']['pending_entries']==pytest.approx(sizes[0])
        assert records[1]['sizing_json']['target_notional']==pytest.approx((100000-sizes[0])/2)
    finally:co.close()
