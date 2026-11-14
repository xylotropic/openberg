"""Synthetic engineering cases. These databases never reach the production journal."""
import asyncio
import math
import statistics
import time
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ctb.alpha.accounting import Accounting, factor_alpha, ratios
from ctb.alpha.calendar import NY, Sessions
from ctb.alpha.classifier import Classifier, choice, gates, noul, MECHANISMS, EFFECTS
from ctb.alpha.config import VERSION, digest, frozen
from ctb.alpha.evidence import Evidence, FilingText, customer_passages, reporting_period
from ctb.alpha.execution import Execution
from ctb.alpha.ledger import Journal
from ctb.alpha.research import purged_split, ridge_fit
from ctb.alpha.portfolio import Portfolio
from ctb.alpha.service import AlphaService
from ctb.alpha.evidence import numeric_comparison
from ctb.broker.alpaca import AlpacaError, TERMINAL, iso
from ctb.broker.coordinator import BrokerCoordinator
from ctb.broker.alpaca_executor import AlpacaExecutor
from ctb.config import Settings
from ctb.db import DB
from ctb.kairos.client import RateBudget, KairosResult
from ctb.market.alpaca import AlpacaFeed
from ctb.market.store import MarketStore


class Paper:
    def __init__(self):
        self.pos=[];self.open=[];self.sent=[];self.cancelled=[];self.by_id={};self.activities=[]
        self.account_id='synthetic-paper';self.timeout=False;self.hidden=False;self.market_open=True
        self.price=100;self.quote_age=0
    async def account(self): return {'id':self.account_id,'equity':'100000','cash':'100000','buying_power':'100000','options_buying_power':'100000','options_trading_level':3}
    async def positions(self): return self.pos
    async def orders(self): return self.open
    async def clock(self): return {'is_open':self.market_open,'next_close':iso(time.time()+3600)}
    async def asset(self,symbol): return {'tradable':True,'shortable':True,'easy_to_borrow':True,'size':'100','min_trade_increment':'.00000001'}
    async def snapshots(self,symbols,kind): return {s:{'latestQuote':{'bp':self.price,'ap':self.price,'t':iso(time.time()-self.quote_age)},'latestTrade':{'p':self.price,'t':iso(time.time())}} for s in symbols}
    async def submit(self,body):
        self.sent.append(body)
        order={'id':'broker-'+str(len(self.sent)),**body,'status':'new','filled_qty':'0'}
        self.by_id[body['client_order_id']]=order
        if self.timeout: raise asyncio.TimeoutError()
        return order
    async def order(self,link):
        if self.hidden or link not in self.by_id: raise AlpacaError(404,'not found')
        return self.by_id[link]
    async def cancel(self,order):
        self.cancelled.append(order['id']);self.by_id[order['client_order_id']]['status']='canceled'
    async def request(self,method,path,**kwargs):
        assert method=='GET'
        if path=='/v2/account/activities': return self.activities
        if path=='/v1/corporate-actions': return {'corporate_actions':{},'next_page_token':None}
        raise AssertionError(path)


def holding(symbol,qty):
    return {'symbol':symbol,'qty':str(qty),'qty_available':str(abs(qty)),'side':'long' if qty>0 else 'short',
            'market_value':str(qty*100),'avg_entry_price':'100','current_price':'100','unrealized_pl':'0'}


def setup(tmp_path):
    settings=Settings();settings.venue='alpaca';settings.alpaca_account_id='synthetic-paper';settings.trading_enabled=True
    settings.symbols=['SPY','BTC/USD'];settings.kill_file=tmp_path/'KILL'
    db=DB(tmp_path/'synthetic.db');j=Journal(db);broker=Paper()
    co=BrokerCoordinator(settings,db,broker,lock_dir=tmp_path/'writer-locks');co.acquire()
    ex=AlpacaExecutor(settings,db,co.read_client,lambda *_:None,AlpacaFeed(MarketStore(settings.symbols),broker))
    ex.coordinator=co;ex.manual.client=co.read_client
    config=frozen(settings)
    j.insert('alpha_versions',{'id':VERSION,'config_json':config,'config_hash':digest(config),'enabled':1,'state':'ARMED_WAITING_EVENT',
        'armed_at':time.time()-600,'allocation':10000,'peak_nav':10000})
    svc=SimpleNamespace(j=j,client=co.read_client,coordinator=co,config=config,settings=settings,engine=SimpleNamespace(executor=ex))
    svc.version=lambda:j.one('SELECT * FROM alpha_versions WHERE id=?',[VERSION])
    svc.sessions=Sessions(co.read_client,j)
    base=datetime.now(NY).date()-timedelta(days=20)
    svc.sessions.rows=[Sessions.parse({'date':str(base+timedelta(days=i)),'open':'09:30','close':'16:00'}) for i in range(70) if (base+timedelta(days=i)).weekday()<5]
    svc.accounting=Accounting(svc);svc.execution=Execution(svc)
    return svc,co,broker,db,ex


def group(svc, *, identity='synthetic-group',state='entering'):
    now=time.time()
    target={'sector':'XLK','legs':[{'symbol':'SWKS','qty':2,'side':'buy','price':100,'beta':1},
                                 {'symbol':'XLK','qty':2,'side':'sell','price':100,'beta':1}]}
    svc.j.insert('alpha_groups',{'id':identity,'version':VERSION,'event_id':'synthetic-event-'+identity,'account_id':svc.coordinator.account_id,
        'state':state,'created_at':now,'entry_window_start':now-10,'entry_deadline':now+300,'target_json':target,'risk_json':{}})
    svc.coordinator.reserve_group(identity,['SWKS','XLK'])
    return svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[identity])


def alpha_order(svc,g,symbol='SWKS',qty='2',side='buy',role='entry',effect='increase',expected=0):
    leg=next(l for l in g['target_json']['legs'] if l['symbol']==symbol)
    return svc.execution.make_order(g,leg,role,{'symbol':symbol,'qty':qty,'side':side,'type':'market','time_in_force':'day'},
        deadline=g['entry_deadline'] if role=='entry' else None,effect=effect,expected=expected,maximum=qty)


def fill(svc,order, *, qty='2',price='100',at=None,identity='synthetic-fill'):
    at=at or time.time()
    activity={'id':identity,'activity_type':'FILL','order_id':(order.get('broker_json') or {}).get('id','broker'),
        'symbol':order['symbol'],'side':order['request_json']['side'],'qty':qty,'price':price,'transaction_time':iso(at)}
    svc.accounting.apply_fill(order,activity,at)


def test_customer_revenue_does_not_use_receivables_or_ten_largest_customer_percentage():
    text='Our ten largest customers represented 96 percent of total sales. Apple represented 90 percent of total net sales.\nApple represented more than ten percent of total net sales. Receivables were 75 percent.'
    rows=customer_passages(text)
    assert rows[0]['exposure_pct']==90 and rows[1]['exposure_pct'] is None and rows[1]['exposure_floor_pct']==10
    assert not customer_passages('One unnamed customer represented 80 percent of net sales.')


def test_ambiguous_historical_percentages_remain_unknown():
    assert not customer_passages('Apple represented 60%, 70%, and 80% of total net sales in 2024, 2025 and 2026.',fiscal_year='2026')
    rows=customer_passages('During fiscal 2026, fiscal 2025 and fiscal 2024, Apple represented 61%, 67%, and 69% of total net sales, respectively.',fiscal_year='2026')
    assert rows[0]['exposure_pct']==61


def test_drex_uses_selected_probability_not_confidence_and_rejects_bool():
    assert choice({'x':{'type':'choice','choice':'a','probabilities':{'a':.2,'b':.8},'confidence':.99}},'x',{'a':1,'b':1})==('a',.2)
    with pytest.raises(ValueError): noul({'x':{'type':'noul','noul':True}},'x')
    with pytest.raises(ValueError): choice({'x':{'type':'choice','choice':'a','probabilities':{'a':.9,'b':.9}}},'x',{'a':1,'b':1})


def test_unsupported_mechanism_and_noncomparable_periods_abstain():
    c=frozen(Settings())
    answers={k:{'type':'noul','noul':.99} for k in ('relationship_supported','materiality','comparison_valid','persistence')}
    answers['material_contradiction']={'type':'noul','noul':.01}
    answers['mechanism']={'type':'choice','choice':'price_only','probabilities':{k:(.99 if k=='price_only' else .01/(len(MECHANISMS)-1)) for k in MECHANISMS}}
    answers['target_effect']={'type':'choice','choice':'positive','probabilities':{k:(.99 if k=='positive' else .01/(len(EFFECTS)-1)) for k in EFFECTS}}
    assert not gates(answers,c)['eligible']
    answers['comparison_valid']['noul']=.1
    assert 'noncomparable_evidence' in gates(answers,c)['reasons']


def test_bootstrap_and_old_republication_never_become_fresh_events(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    e=Evidence(None,None,svc.j,svc.sessions,svc.config)
    now=time.time()
    first=e.store(provider='sec',external_id='synthetic-1',entity='AAPL',raw='<p>fiscal 2026 fourth quarter</p>',url='https://www.sec.gov/x',published_at=now-86400,kind='bootstrap',metadata={'form':'8-K'})
    event=e.event(first)
    assert event['state']=='abstained' and event['reason']=='bootstrap_or_backfill'
    revised=e.store(provider='sec',external_id='synthetic-2',entity='AAPL',raw='<p>fiscal 2026 fourth quarter revised</p>',url='https://www.sec.gov/y',published_at=now,kind='live',metadata={'form':'8-K'})
    assert e.event(revised)['id']==event['id'] and e.event(revised)['state']=='abstained'
    assert svc.j.one('SELECT COUNT(*) n FROM alpha_event_sources')['n']==2
    with pytest.raises(ValueError): svc.j.update('alpha_sources',first['id'],{'raw':'changed'})
    co.close()


def test_calendar_handles_early_close_holiday_dst_and_ten_subsequent_sessions(tmp_path):
    svc,co,*_=setup(tmp_path)
    cal=svc.sessions
    dates=['2026-11-02','2026-11-03','2026-11-04','2026-11-05','2026-11-06','2026-11-09','2026-11-10','2026-11-11','2026-11-12','2026-11-13','2026-11-16']
    cal.rows=[Sessions.parse({'date':d,'open':'09:30','close':'13:00' if d==dates[-1] else '16:00'}) for d in dates]
    assert cal.exit_deadline(cal.rows[0]['open']+2000)==cal.rows[-1]['close']-1800
    assert cal.window(cal.rows[-1]['close']-1750)['start']==cal.rows[-1]['close']-1800
    assert cal.current(datetime(2026,11,7,12,tzinfo=NY).timestamp()) is None
    before=Sessions.parse({'date':'2026-10-30','open':'09:30','close':'16:00'})
    assert datetime.fromtimestamp(before['open'],NY).hour==9
    assert datetime.fromtimestamp(cal.rows[0]['open'],NY).hour==9
    assert cal.rows[0]['open']-before['open']==3*86400+3600
    co.close()


def test_process_lock_account_pin_and_read_client_cannot_be_bypassed(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    other=BrokerCoordinator(ex.s,db,b,lock_dir=co.lock_dir)
    with pytest.raises(ValueError): other.acquire()
    with pytest.raises(ValueError): getattr(co.read_client,'submit')
    async def run():
        with pytest.raises(ValueError): await co.read_client.request('POST','/v2/orders')
        b.account_id='wrong-account'
        with pytest.raises(ValueError): await co.refresh()
    asyncio.run(run());co.close();other.acquire();other.close()


def test_atomic_shared_hedge_and_manual_underlying_reservations(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc)
    with pytest.raises(ValueError): co.reserve_group('second',['AAPL','XLK'])
    assert co.one('SELECT * FROM execution_leases WHERE resource=?',['AAPL']) is None
    async def run():
        with pytest.raises(ValueError): await ex.manual.preview({'symbol':'SWKS','side':'buy','qty':'1','type':'market'})
    asyncio.run(run());co.close()


def test_manual_claim_is_same_transaction_as_execution_and_send_is_once(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    async def run():
        ticket=await ex.manual.preview({'symbol':'SPY','side':'buy','qty':'1','type':'market'})
        a,c=await asyncio.gather(ex.manual.send(ticket['id']),ex.manual.send(ticket['id']))
        assert a['status']==c['status']=='new' and len(b.sent)==1
        intent=co.one('SELECT * FROM execution_intents WHERE owner_record=?',[ticket['id']])
        row=db.one('SELECT * FROM manual_orders WHERE id=?',[ticket['id']])
        assert intent['attempted_at']==row['attempted_ts']
        assert db.one('SELECT * FROM manual_ownership WHERE resource=?',['SPY'])
    asyncio.run(run());co.close()


def test_ambiguous_timeout_and_404_never_repost_even_after_new_instance(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc);o=alpha_order(svc,g)
    async def run():
        b.timeout=True
        with pytest.raises(AlpacaError): await svc.execution.send(o)
        assert len(b.sent)==1
        b.hidden=True
        with pytest.raises(AlpacaError): await svc.execution.send(o)
        assert len(b.sent)==1
        with pytest.raises(ValueError): alpha_order(svc,g)
        b.hidden=False;b.timeout=False
        same=co.one('SELECT * FROM execution_intents WHERE owner_record=?',[o['id']])
        assert (await co.dispatch(same['id']))['status']=='new' and len(b.sent)==1
    asyncio.run(run());co.close()


def test_shared_capital_and_pending_orders_count_toward_capacity(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    async def run():
        await co.refresh();co.allocate('alpha',VERSION,10000)
        assert co.available('manual')==90000 and co.available('alpha')==100000
        ticket=await ex.manual.preview({'symbol':'SPY','side':'buy','qty':'1','type':'market'})
        await ex.manual.send(ticket['id'])
        assert co.available('manual')==89900
        ex.s.max_positions=1
        second=await ex.manual.preview({'symbol':'BTC/USD','side':'buy','qty':'.2','type':'market'})
        with pytest.raises(ValueError): await ex.manual.send(second['id'])
        assert len(b.sent)==1
    asyncio.run(run());co.close()


def test_pause_preserves_reduction_and_prevents_new_alpha_exposure(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc);entry=alpha_order(svc,g)
    svc.j.update('alpha_versions',VERSION,{'enabled':0})
    async def run():
        with pytest.raises(ValueError): await svc.execution.send(entry)
        fill(svc,entry);b.pos=[holding('SWKS',2)]
        exit_order=alpha_order(svc,g,qty='2',side='sell',role='exit',effect='reduce',expected=2)
        assert (await svc.execution.send(exit_order))['status']=='new'
        assert len(b.sent)==1 and b.sent[0]['side']=='sell'
    asyncio.run(run());co.close()


def test_owned_close_never_flips_or_adopts_external_inventory(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc)
    b.pos=[holding('SWKS',3)]
    async def run():
        o=alpha_order(svc,g,qty='2',side='sell',role='exit',effect='reduce',expected=2)
        with pytest.raises(ValueError): await svc.execution.send(o)
        assert not b.sent
    asyncio.run(run());co.close()


def test_partial_pair_recovery_unwinds_owned_quantity_and_waits_for_cancel_final(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc)
    first=alpha_order(svc,g);second=alpha_order(svc,g,'XLK',side='sell')
    async def run():
        a=await svc.execution.send(first);z=await svc.execution.send(second)
        a.update(status='canceled',filled_qty='1',filled_avg_price='100')
        z.update(status='canceled');b.pos=[holding('SWKS',1)]
        svc.j.update('alpha_orders',first['id'],{'status':'canceled','broker_json':a})
        svc.j.update('alpha_orders',second['id'],{'status':'canceled','broker_json':z})
        fill(svc,svc.j.one('SELECT * FROM alpha_orders WHERE id=?',[first['id']]),qty='1',at=time.time()-130)
        current=svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[g['id']])
        await co.refresh();await svc.execution.group_tick(current,{'is_open':True},time.time())
        assert b.sent[-1]['side']=='sell' and b.sent[-1]['qty']=='1'
        assert svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[g['id']])['state']=='unwinding'
        # A request is not a fill or a flat account.
        assert svc.j.one('SELECT * FROM alpha_positions WHERE symbol=?',['SWKS'])['qty']=='1'
    asyncio.run(run());co.close()


def test_cancel_fill_race_remains_owned_and_exit_deadline_anchors_actual_first_fill(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc);o=alpha_order(svc,g)
    async def run():
        result=await svc.execution.send(o)
        await svc.execution.cancel(o)
        result.update(status='filled',filled_qty='2',filled_avg_price='100')
        svc.j.update('alpha_orders',o['id'],{'broker_json':result,'status':'filled'})
        at=time.time()-15
        fill(svc,svc.j.one('SELECT * FROM alpha_orders WHERE id=?',[o['id']]),at=at)
        current=svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[g['id']])
        assert current['recovery_deadline']==at+120
        assert current['exit_deadline']==svc.sessions.exit_deadline(at)
        assert svc.j.one('SELECT * FROM alpha_positions WHERE symbol=?',['SWKS'])['qty']=='2'
    asyncio.run(run());co.close()


def test_overdue_exit_retains_obligation_when_market_closed(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc,state='closing');o=alpha_order(svc,g)
    fill(svc,o);b.pos=[holding('SWKS',2)]
    svc.j.update('alpha_orders',o['id'],{'status':'filled','broker_json':{'filled_qty':'2'}})
    async def run():
        await co.refresh();await svc.execution.group_tick(svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[g['id']]),{'is_open':False},time.time())
        assert svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[g['id']])['state']=='overdue'
        assert not b.sent
    asyncio.run(run());co.close()


def test_owned_fill_idempotence_open_losses_and_idle_marks(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc);o=alpha_order(svc,g)
    fill(svc,o,qty='2',price='100',identity='same');fill(svc,o,qty='2',price='100',identity='same')
    assert svc.j.one('SELECT COUNT(*) n FROM alpha_fills')['n']==1
    mark=svc.accounting.economics(time.time()+10,{'SWKS':{'price':90}})
    assert mark['gross_nav']==9980 and mark['net_nav']<9980
    assert mark['costs']==pytest.approx(.04)
    assert svc.accounting.metrics()['observations']==0
    co.close()


def test_cash_dividend_short_liability_and_split_gap_do_not_double_count_spread(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc);o=alpha_order(svc,g,'XLK',side='sell')
    at=time.time()-2*86400;fill(svc,o,qty='2',price='100',at=at)
    svc.j.insert('alpha_corporate_actions',{'id':'ca1','external_id':'ca1','observed_at':time.time(),'kind':'cash_dividend','symbol':'XLK',
        'effective_at':at+86400,'raw_json':{'rate':1,'ex_date':datetime.now(NY).date().isoformat()},'content_hash':'x'})
    mark=svc.accounting.economics(time.time(),{'XLK':{'price':99}})
    assert mark['gross_nav']==10002 and mark['dividend_accrual']==-2
    assert mark['net_nav']==pytest.approx(10000-.04-200*.03*2/365,abs=.001)
    svc.j.insert('alpha_corporate_actions',{'id':'split','external_id':'split','observed_at':time.time(),'kind':'forward_split','symbol':'XLK',
        'effective_at':at+86400,'raw_json':{'new_rate':2,'old_rate':1},'content_hash':'y'})
    assert 'split' in svc.accounting.economics(time.time(),{'XLK':{'price':49.5}})['gaps']
    co.close()


def test_monthly_budget_cache_and_failures_reserve_before_model_call(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    class Model:
        def __init__(self): self.calls=0
        async def ask(self,state,questions):
            self.calls+=1
            return KairosResult({'x':{'type':'noul','noul':.9}},{'input_tokens':100},{'model':'drex-max-finance'},10,1,100,{'model':'drex-max-finance','answers':{'x':{'type':'noul','noul':.9}}})
    model=Model();budget=RateBudget(2);c=Classifier(None,svc.j,svc.config,budget,client=model)
    async def run():
        q={'x':{'type':'noul','instructions':'Synthetic evidence question'}}
        a,z=await asyncio.gather(c.ask('test','same',q,['synthetic']),c.ask('test','same',q,['synthetic']))
        assert a['id']==z['id'] and model.calls==1 and budget.used()==1
        c.config={**svc.config,'monthly_model_usd':.000001}
        with pytest.raises(ValueError): await c.ask('test','different',q,[])
        assert model.calls==1 and budget.used()==1
    asyncio.run(run());co.close()


def test_daily_metrics_include_zero_periods_and_report_cold_start():
    assert ratios([0]*30)['sharpe'] is None
    assert ratios([.01,-.01])['status']=='insufficient_data'
    values=[.01,-.01,0,-.02,0]*5
    r=ratios(values)
    assert r['sortino']==pytest.approx(statistics.mean(values)/math.sqrt(sum(min(x,0)**2 for x in values)/len(values))*math.sqrt(252))
    assert r['ci_sharpe_95'] and r['observations']==25


def test_factor_alpha_and_purged_event_grouping():
    market=[math.sin(i)*.01 for i in range(100)];sector=[math.cos(i*1.3)*.012 for i in range(100)]
    strategy=[.0002+.4*m-.3*s for m,s in zip(market,sector)]
    result=factor_alpha(strategy,market,sector)
    assert result['alpha_daily']==pytest.approx(.0002) and result['market_beta']==pytest.approx(.4)
    rows=[{'event_id':'a','entry_at':1,'mature_at':12},{'event_id':'b','entry_at':4,'mature_at':8},{'event_id':'c','entry_at':11,'mature_at':22}]
    train,test=purged_split(rows,10)
    assert [r['event_id'] for r in train]==['b'] and [r['event_id'] for r in test]==['c']
    assert ridge_fit([])['status']=='insufficient_independent_events'
    assert 'done_for_day' not in TERMINAL and 'suspended' not in TERMINAL


def test_cumulative_target_and_origin_owned_reduction_are_enforced(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc)
    async def run():
        first=alpha_order(svc,g)
        result=await svc.execution.send(first)
        result.update(status='filled',filled_qty='2')
        intent=co.one('SELECT * FROM execution_intents WHERE owner_record=?',[first['id']])
        co.update(intent['id'],{'status':'filled','broker_json':result})
        b.pos=[holding('SWKS',2)]
        await co.refresh(force=True)
        second=alpha_order(svc,g,qty='1',expected=2)
        with pytest.raises(ValueError,match='Cumulative'): await svc.execution.send(second)
        # Matching broker inventory is insufficient if this group has not booked its own fill.
        close=alpha_order(svc,g,qty='2',side='sell',role='exit',effect='reduce',expected=2)
        with pytest.raises(ValueError,match='independently booked'): await svc.execution.send(close)
        assert len(b.sent)==1
    asyncio.run(run());co.close()


def test_pause_during_network_preflight_blocks_the_atomic_attempt(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc);o=alpha_order(svc,g)
    original=b.snapshots
    async def during(*args):
        svc.j.update('alpha_versions',VERSION,{'enabled':0})
        return await original(*args)
    b.snapshots=during
    async def run():
        with pytest.raises(ValueError,match='paused'): await svc.execution.send(o)
        assert not b.sent
        assert co.one('SELECT * FROM execution_intents WHERE owner_record=?',[o['id']])['attempted_at'] is None
    asyncio.run(run());co.close()


def test_current_manifest_does_not_rewrite_immutable_source_history(tmp_path):
    svc,co,*_=setup(tmp_path)
    evidence=Evidence(None,None,svc.j,svc.sessions,svc.config)
    a=evidence.store(provider='sec',external_id='same',entity='AAPL',raw='<p>original</p>',url='https://www.sec.gov/a',published_at=time.time()-10,kind='bootstrap')
    b=evidence.store(provider='sec',external_id='same',entity='AAPL',raw='<p>revised</p>',url='https://www.sec.gov/a',published_at=time.time()-5,kind='live')
    assert b['previous_id']==a['id'] and a['content_hash']!=b['content_hash']
    with pytest.raises(Exception): svc.j.db.conn.execute('UPDATE alpha_sources SET text=? WHERE id=?',['tampered',a['id']])
    co.close()


def test_numeric_revenue_comparison_uses_consolidated_dated_usd_facts():
    raw='''<xbrli:context id="total"><xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
    <ix:nonFraction name="us-gaap:Revenues" contextRef="total" unitRef="USD" scale="6">120</ix:nonFraction>'''
    parsed=FilingText(raw)
    current={'metadata_json':{'numeric_facts':parsed.numeric_facts}}
    prior={'metadata_json':{'numeric_facts':[{**parsed.numeric_facts[0],'value':100000000,'startdate':'2025-01-01','enddate':'2025-03-31'}]}}
    result=numeric_comparison(prior,current,'Q1')
    assert result['revenue_change']==pytest.approx(.2) and result['revenue_difference_usd']==20000000
    assert numeric_comparison(prior,current,'FY')['revenue_change'] is None


def test_startup_migrates_existing_manual_order_without_post(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    async def run():
        ticket=await ex.manual.preview({'symbol':'SPY','side':'buy','qty':'1','type':'market'})
        body=ticket['request'];b.by_id[body['client_order_id']]={'id':'existing',**body,'status':'new','filled_qty':'0'}
        db.update('manual_orders',ticket['id'],{'attempted_ts':time.time()-30,'status':'unknown'})
        await co.migrate()
        assert not b.sent and co.one('SELECT * FROM execution_intents WHERE owner_record=?',[ticket['id']])['attempted_at']
        await ex.manual.send(ticket['id'])
        assert not b.sent
    asyncio.run(run());co.close()


def test_legacy_orders_and_cancellation_use_exact_origin_even_when_paused(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    tid=db.insert('trades',{'venue':'alpaca','account_id':co.account_id,'symbol':'SPY','side':'long','status':'opening',
        'entry_link_id':'ctba-synthetic-legacy','requested_qty':1,'opened_ts':time.time(),'cap_price':100,'ref_price':100,'mode':'strict'})
    trade=db.one('SELECT * FROM trades WHERE id=?',[tid])
    body={'symbol':'SPY','qty':'1','side':'buy','type':'limit','limit_price':'100','time_in_force':'day','client_order_id':trade['entry_link_id']}
    async def run():
        result=await ex.submit_order(trade,body,'increase')
        ex.s.trading_enabled=False
        await ex.cancel_order(trade,result)
        assert len(b.sent)==1 and b.cancelled==[result['id']]
        with pytest.raises(ValueError): await co.legacy_cancel({**trade,'id':999},result)
    asyncio.run(run());co.close()


def test_realized_trade_and_late_fee_revision_replay_remain_idempotent(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc);o=alpha_order(svc,g)
    svc.j.update('alpha_orders',o['id'],{'broker_json':{'id':'entry-id'}})
    o=svc.j.one('SELECT * FROM alpha_orders WHERE id=?',[o['id']])
    fill(svc,o,at=time.time()-50,identity='first')
    exit_order=alpha_order(svc,g,side='sell',role='exit',effect='reduce',expected=2)
    fill(svc,exit_order,price='105',at=time.time()-20,identity='second')
    assert svc.j.one('SELECT * FROM alpha_positions WHERE symbol=?',['SWKS'])['realized']==10
    fee={'id':'late-fee','activity_type':'FEE','symbol':'SWKS','order_id':'entry-id','date':datetime.now(NY).date().isoformat(),'net_amount':'-.20'}
    b.activities=[fee]
    async def run():
        await svc.accounting.activities();await svc.accounting.activities()
        assert svc.j.one("SELECT SUM(amount) n FROM alpha_cashflows WHERE kind='broker_cash'")['n']==pytest.approx(-.2)
        fee['net_amount']='-.25'
        await svc.accounting.activities();await svc.accounting.activities()
        assert svc.j.one("SELECT SUM(amount) n FROM alpha_cashflows WHERE kind='broker_cash'")['n']==pytest.approx(-.25)
    asyncio.run(run());co.close()


def test_service_arm_and_restart_are_separate_from_legacy_switch(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    engine=SimpleNamespace(s=ex.s,alpha_journal=svc.j,coordinator=co,session=None,budget=RateBudget(200),executor=ex)
    service=AlphaService(engine)
    svc.j.update('alpha_versions',VERSION,{'enabled':0,'armed_at':None,'allocation':0})
    service.j.state('bootstrap_complete',{'at':time.time()},write=True)
    service.j.insert('alpha_relationships',{'id':'verified','customer':'AAPL','supplier':'SWKS','state':'verified'})
    async def preflight(): service.ready=True
    service.preflight=preflight
    async def run():
        before=ex.s.trading_enabled
        result=await service.control('arm')
        assert result['version']['enabled']==1 and result['version']['allocation']==10000
        assert ex.s.trading_enabled==before
        await service.control('pause')
        restored=AlphaService(engine)
        assert restored.version()['enabled']==0 and restored.version()['armed_at']
        assert ex.s.trading_enabled==before
        svc.j.state('review_required:'+VERSION,{'drawdown':.06},write=True)
        await service.control('pause')
        with pytest.raises(ValueError,match='new version'): await service.control('resume')
    asyncio.run(run());co.close()


def test_portfolio_uses_lagged_risk_whole_share_targets_and_tighter_caps(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    base=datetime.now(NY).date()-timedelta(days=160)
    svc.sessions.rows=[Sessions.parse({'date':str(base+timedelta(days=i)),'open':'09:30','close':'16:00'}) for i in range(300) if (base+timedelta(days=i)).weekday()<5]
    next_session=next(r for r in svc.sessions.rows if r['open']>time.time())
    dates=[r['date'] for r in svc.sessions.rows if r['close']<next_session['open']][-61:]
    async def request(method,path,**kwargs):
        assert method=='GET' and path=='/v2/stocks/bars'
        params=kwargs['params'];symbol=params['symbols'];price=100.;rows=[]
        for i,date in enumerate(dates):
            change=math.sin(i*1.7)*.008*(1.1 if symbol=='SWKS' else 1)
            price*=1+change
            rows.append({'t':date+'T05:00:00Z','c':price,'v':100000})
        assert params['adjustment']=='all' and params['feed']=='iex'
        return {'bars':{symbol:rows},'next_page_token':None}
    b.request=request
    svc.j.state('corporate_action_coverage',{'through':time.time()},write=True)
    portfolio=Portfolio(svc)
    async def run():
        target,risk=await portfolio.construct({'action':'long'},{'supplier':'SWKS','sector':'XLK'}, {'session':next_session})
        assert len(target['legs'])==2 and risk['gross']<=1000
        assert risk['lookback_dates']==dates[1:] and risk['history_cutoff']<next_session['open']
        assert target['legs'][0]['qty']*100<=250 and abs(risk['beta_dollars'])/10000<=.15
        ex.s.max_notional_usdt=50
        with pytest.raises(ValueError,match='whole-share'): await portfolio.construct({'action':'long'},{'supplier':'SWKS','sector':'XLK'},{'session':next_session})
    asyncio.run(run());co.close()


def test_historical_daily_marks_preserve_the_live_accounting_clock(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    async def run():
        live=await svc.accounting.mark()
        session=next(r for r in reversed(svc.sessions.rows) if r['close']<time.time())
        await svc.accounting.mark(at=session['close'],session=session['date'])
        assert svc.j.state('live_nav')['marked_at']==live['marked_at']
        assert svc.j.one('SELECT * FROM alpha_nav')['session']==session['date']
    asyncio.run(run());co.close()


def test_cancel_recovery_terminal_fill_updates_alpha_mirror_and_cannot_strand_exit(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc)
    async def run():
        o=alpha_order(svc,g);result=await svc.execution.send(o)
        # Alpha saw one share. Broker now reports both shares during cancellation recovery.
        svc.j.update('alpha_orders',o['id'],{'broker_json':{**result,'filled_qty':'1'},'status':'partially_filled'})
        result.update(status='filled',filled_qty='2',filled_avg_price='100')
        await svc.execution.cancel(svc.j.one('SELECT * FROM alpha_orders WHERE id=?',[o['id']]))
        row=svc.j.one('SELECT * FROM alpha_orders WHERE id=?',[o['id']])
        assert row['broker_json']['filled_qty']=='2' and row['status']=='filled'
        fill(svc,row,qty='2');b.pos=[holding('SWKS',2)]
        svc.j.update('alpha_groups',g['id'],{'state':'unwinding'})
        await co.refresh(force=True)
        await svc.execution.group_tick(svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[g['id']]),{'is_open':True},time.time())
        assert b.sent[-1]['side']=='sell' and b.sent[-1]['qty']=='2'
    asyncio.run(run());co.close()


@pytest.mark.parametrize('orphan',[False,True])
def test_restart_resumes_original_provably_unattempted_exit_once(tmp_path,orphan):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc,state='closing');entry=alpha_order(svc,g)
    fill(svc,entry);b.pos=[holding('SWKS',2)]
    o=alpha_order(svc,g,side='sell',role='exit',effect='reduce',expected=2)
    if orphan:
        with db.lock: db.conn.execute('DELETE FROM execution_intents WHERE owner_record=?',[o['id']])
    async def run():
        # Simulate a new Execution object after process death before an attempt claim.
        restarted=Execution(svc)
        await restarted.refresh_orders();await restarted.refresh_orders()
        assert len(b.sent)==1 and b.sent[0]['client_order_id']==o['client_order_id']
    asyncio.run(run());co.close()


def test_terminal_fill_keeps_gross_capacity_until_broker_inventory_is_visible(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    ex.s.max_notional_usdt=100000;ex.s.max_leverage=1
    original=b.submit
    async def filled(body):
        o=await original(body);o.update(status='filled',filled_qty=body['qty'],filled_avg_price='100');return o
    b.submit=filled
    async def run():
        a=await ex.manual.preview({'symbol':'SPY','side':'buy','qty':'800','type':'market'})
        await ex.manual.send(a['id'])
        assert sum(co.pending_notional(i) for i in co.commitments())==80000
        z=await ex.manual.preview({'symbol':'QQQ','side':'buy','qty':'800','type':'market'})
        with pytest.raises(ValueError,match='leverage'): await ex.manual.send(z['id'])
        assert len(b.sent)==1
        b.pos=[holding('SPY',800)]
        await co.refresh(force=True)
        first=co.one('SELECT * FROM execution_intents WHERE owner_record=?',[a['id']])
        assert first['inventory_seen_qty']=='800' and co.pending_notional(first)==0
        assert sum(abs(float(p['market_value'])) for p in co.snapshot['positions'])==80000
    asyncio.run(run());co.close()


def test_operator_close_waits_for_tick_lock_and_cannot_be_overwritten(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc)
    async def run():
        entered=asyncio.Event();release=asyncio.Event()
        async def stale_tick():
            async with svc.execution.lock:
                snapshot=svc.execution.groups()
                entered.set();await release.wait()
                svc.j.update('alpha_groups',snapshot[0]['id'],{'state':'open'})
        task=asyncio.create_task(stale_tick());await entered.wait()
        close=asyncio.create_task(svc.execution.close_owned());await asyncio.sleep(0)
        assert not close.done()
        release.set();await task;await close
        assert svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[g['id']])['state']=='closing'
    asyncio.run(run());co.close()


def test_entry_deadline_is_rechecked_after_awaited_quote_read(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc);o=alpha_order(svc,g)
    original=b.snapshots
    async def expired(*args):
        svc.j.update('alpha_groups',g['id'],{'entry_deadline':time.time()-1})
        return await original(*args)
    b.snapshots=expired
    async def run():
        with pytest.raises(ValueError,match='window'): await svc.execution.send(o)
        assert not b.sent
    asyncio.run(run());co.close()


def test_exit_worker_continues_when_activity_and_model_services_are_unavailable(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc)
    async def run():
        entry=alpha_order(svc,g);result=await svc.execution.send(entry)
        result.update(status='filled',filled_qty='2',filled_avg_price='100')
        intent=co.one('SELECT * FROM execution_intents WHERE owner_record=?',[entry['id']])
        co.update(intent['id'],{'status':'filled','broker_json':result})
        svc.j.update('alpha_orders',entry['id'],{'status':'filled','broker_json':result})
        fill(svc,svc.j.one('SELECT * FROM alpha_orders WHERE id=?',[entry['id']]))
        b.pos=[holding('SWKS',2)]
        svc.j.update('alpha_groups',g['id'],{'state':'closing','exit_deadline':time.time()-1})
        async def unavailable(*args,**kwargs): raise AlpacaError(503,'Synthetic activity outage')
        b.request=unavailable
        await svc.execution.tick()
        assert b.sent[-1]['side']=='sell' and b.sent[-1]['qty']=='2'
        assert svc.j.one('SELECT * FROM alpha_heartbeats WHERE id=?',['broker_activities'])['error']
        assert svc.j.one('SELECT COUNT(*) n FROM alpha_classifications')['n']==0
    asyncio.run(run());co.close()


def test_fill_discovered_inside_group_cancellation_never_terminalizes_before_next_tick(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc)
    async def run():
        a=alpha_order(svc,g);z=alpha_order(svc,g,'XLK',side='sell')
        result=await svc.execution.send(a)
        svc.j.update('alpha_orders',z['id'],{'status':'rejected','error':'Synthetic sibling rejection'})
        zintent=co.one('SELECT * FROM execution_intents WHERE owner_record=?',[z['id']]);co.update(zintent['id'],{'status':'rejected'})
        original=b.order
        async def fill_at_cancel(link):
            if link==a['client_order_id']:
                result.update(status='filled',filled_qty='2',filled_avg_price='100')
            return await original(link)
        b.order=fill_at_cancel
        await co.refresh(force=True)
        await svc.execution.group_tick(g,{'is_open':True},time.time())
        current=svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[g['id']])
        assert current['state']=='unwinding' and not svc.j.query('SELECT * FROM alpha_fills')
        assert svc.j.one('SELECT * FROM alpha_orders WHERE id=?',[a['id']])['broker_json']['filled_qty']=='2'
        # On the next tick, the real fill activity and broker inventory become visible.
        fill(svc,svc.j.one('SELECT * FROM alpha_orders WHERE id=?',[a['id']]))
        b.pos=[holding('SWKS',2)];await co.refresh(force=True)
        await svc.execution.group_tick(svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[g['id']]),{'is_open':True},time.time())
        assert b.sent[-1]['side']=='sell' and b.sent[-1]['qty']=='2'
    asyncio.run(run());co.close()


def test_definitive_rejection_mirrors_without_broker_payload_after_restart(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc);o=alpha_order(svc,g)
    intent=co.one('SELECT * FROM execution_intents WHERE owner_record=?',[o['id']])
    co.update(intent['id'],{'attempted_at':time.time(),'status':'rejected','error':'Synthetic HTTP422'})
    svc.j.update('alpha_orders',o['id'],{'attempted_at':time.time(),'status':'submitting'})
    async def run():
        await Execution(svc).refresh_orders()
        assert svc.j.one('SELECT * FROM alpha_orders WHERE id=?',[o['id']])['status']=='rejected'
        await svc.execution.cancel(o)
        assert not b.sent and not b.cancelled
        assert co.one('SELECT * FROM execution_intents WHERE id=?',[intent['id']])['status']=='rejected'
    asyncio.run(run());co.close()


def test_second_same_symbol_opening_waits_for_prior_filled_inventory_acknowledgment(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    ex.s.max_notional_usdt=100000;ex.s.max_leverage=1
    original=b.submit
    async def filled(body):
        o=await original(body);o.update(status='filled',filled_qty=body['qty'],filled_avg_price='100');return o
    b.submit=filled
    async def run():
        a=await ex.manual.preview({'symbol':'SPY','side':'buy','qty':'400','type':'market'})
        # Both immutable reviews existed before the first fill.
        z=await ex.manual.preview({'symbol':'SPY','side':'buy','qty':'400','type':'market'})
        await ex.manual.send(a['id'])
        with pytest.raises(ValueError,match='unresolved'):
            co.prepare(owner_type='manual',owner_id='manual',owner_record=z['id'],business_key='manual:'+z['id'],body=z['request'],
                effect='increase',resources=['SPY'],expected_qty='0',max_qty='400',notional=40000)
        assert len(b.sent)==1 and sum(co.pending_notional(i) for i in co.commitments())==40000
        b.pos=[holding('SPY',400)];await co.refresh(force=True)
        # The stale review is rejected by the exact starting-inventory guard.
        with pytest.raises(ValueError): await ex.manual.send(z['id'])
        assert len(b.sent)==1 and not co.commitments()
    asyncio.run(run());co.close()


def test_late_fill_reactivates_a_terminal_group_instead_of_losing_owned_exit(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);g=group(svc,state='canceled');o=alpha_order(svc,g)
    fill(svc,o)
    assert svc.j.one('SELECT * FROM alpha_groups WHERE id=?',[g['id']])['state']=='overdue'
    assert svc.execution.groups()[0]['id']==g['id']
    co.close()


def test_legacy_exit_retries_new_id_only_after_definitive_rejection(tmp_path):
    svc,co,b,db,ex=setup(tmp_path);b.pos=[holding('SPY',1)]
    tid=db.insert('trades',{'venue':'alpaca','account_id':co.account_id,'symbol':'SPY','side':'long','status':'closing',
        'entry_link_id':'ctba-legacy-held','qty':1,'entry_price':100,'opened_ts':time.time()-100,'risk_usd':1})
    original=b.submit;attempts=[]
    async def reject_once(body):
        attempts.append(body['client_order_id'])
        if len(attempts)==1: raise AlpacaError(422,'Synthetic definitive rejection')
        return await original(body)
    b.submit=reject_once
    async def run():
        await ex._submit_exit(db.one('SELECT * FROM trades WHERE id=?',[tid]),1)
        db.update('trades',tid,{'exit_attempt_ts':time.time()-20})
        await ex.continue_close(db.one('SELECT * FROM trades WHERE id=?',[tid]))
        assert len(attempts)==2 and attempts[0]!=attempts[1] and len(b.sent)==1
    asyncio.run(run());co.close()


def test_shared_hourly_model_budget_survives_restart_and_joins_atomic_reservation(tmp_path):
    db=DB(tmp_path/'budget.db')
    a=RateBudget(2);a.calls.append(time.time()-30);a.bind(db,'primary')
    assert a.used()==1
    with db.lock:
        db.conn.execute('BEGIN IMMEDIATE');assert a.try_take();db.conn.execute('ROLLBACK')
    assert a.used()==1 and a.try_take() and not a.try_take()
    restored=RateBudget(2);restored.calls.extend([time.time()]*10);restored.bind(db,'primary')
    assert restored.used()==2 and not restored.try_take()
