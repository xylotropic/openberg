"""Exercise the real candle-to-coordinator contract, including persisted JSON."""
import asyncio
import time
import pytest

from test_alpha import setup
from test_scalper import history,result
from ctb.scalper import Spend,MODEL,VERSION,packet,questions
from ctb.kairos.client import RateBudget,KairosResult
from ctb.broker.alpaca import AlpacaError


def candle(tmp_path):
    svc,co,b,db,ex=setup(tmp_path)
    ex.s.scalper_enabled=True;ex.s.symbols=['SPY'];ex.s.maker_wait_s=0
    store,now=history('SPY',end=time.time());ex.feed.store=store
    data=packet(store['SPY'],now)
    spend=Spend(db,RateBudget(200));state={'strategy':VERSION,'as_of':now,'symbols':{'SPY':data}}
    answer={'type':'choice','choice':'LONG','probabilities':{'LONG':.99,'SHORT':.003,'HOLD':.004,'EXIT':.003}}
    response={'model':MODEL,'answers':{'SPY':answer},'usage':{'input_tokens':5000}}
    identity=spend.reserve(state,questions(['SPY']));spend.settle(identity,KairosResult(response['answers'],response['usage'],{'model':MODEL},10,1,100,response))
    did=db.insert('decisions',{'ts':now,'symbol':'SPY','qset_version':VERSION,'state_json':data,'action':'long','answers_json':{'SPY':answer},
        'decision_json':{'model_action':'LONG','call_id':identity,'signal_at':now}})
    return co,b,db,ex,did


def test_real_coordinator_decodes_saved_response_and_dispatches_once(tmp_path):
    co,b,db,ex,did=candle(tmp_path)
    async def run():
        r=await ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0,horizon_min=30)
        assert r['status']=='canceled' and len(b.sent)==1
        call=db.one('SELECT response_json FROM scalper_calls')
        assert call['response_json']['answers']['SPY']['choice']=='LONG'
        intent=co.one('SELECT * FROM execution_intents WHERE effect=?',['increase'])
        assert intent['attempted_at'] and float(b.sent[0]['qty'])*float(b.sent[0]['limit_price'])<=5000
        await co.dispatch(intent['id']);assert len(b.sent)==1
    try: asyncio.run(run())
    finally: co.close()


def test_failed_preflight_is_terminal_and_migration_never_invents_attempt(tmp_path):
    co,b,db,ex,did=candle(tmp_path)
    dj=db.one('SELECT decision_json FROM decisions WHERE id=?',[did])['decision_json'];dj['model_action']='SHORT';db.update('decisions',did,{'decision_json':dj})
    async def run():
        r=await ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0,horizon_min=30)
        assert r['never_submitted'] and not b.sent
        trade=db.one('SELECT * FROM trades WHERE decision_id=?',[did]);assert trade['status']=='error'
        intent=co.one('SELECT * FROM execution_intents');assert intent['attempted_at'] is None and intent['status']=='rejected'
        # Reproduce the old production orphan at restart. The ledger must retain proof.
        db.update('trades',trade['id'],{'status':'opening'})
        await co.migrate()
        assert co.one('SELECT * FROM execution_intents')['attempted_at'] is None
        assert ex.resolve_unattempted_entry(db.one('SELECT * FROM trades WHERE id=?',[trade['id']]))
        assert db.one('SELECT * FROM decisions WHERE id=?',[did])['action']=='hold'
    try: asyncio.run(run())
    finally: co.close()


def test_ambiguous_attempt_and_broker_404_stay_owned_without_repost(tmp_path):
    co,b,db,ex,did=candle(tmp_path);b.timeout=True
    async def run():
        r=await ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0,horizon_min=30)
        assert not r['ok'] and len(b.sent)==1
        trade=db.one('SELECT * FROM trades WHERE decision_id=?',[did]);b.hidden=True
        assert not ex.resolve_unattempted_entry(trade)
        await co.migrate()
        assert co.one('SELECT * FROM execution_intents')['attempted_at']
        assert db.one('SELECT status FROM trades WHERE id=?',[trade['id']])['status']=='opening'
        assert len(b.sent)==1
    try: asyncio.run(run())
    finally: co.close()


@pytest.mark.parametrize('status',[401,404,408,429,503])
def test_post_attempt_lookup_error_through_open_never_drops_ownership(tmp_path,status):
    co,b,db,ex,did=candle(tmp_path)
    async def missing(_): raise AlpacaError(status,'temporary lookup failure')
    b.order=missing
    async def run():
        r=await ex.open(symbol='SPY',side='long',sl_bp=100,tp_bp=200,decision_id=did,ev_r=0,horizon_min=30)
        assert not r['ok'] and len(b.sent)==1
        trade=db.one('SELECT * FROM trades WHERE decision_id=?',[did])
        assert trade['status']=='opening' and ex.owned('SPY')[0]['id']==trade['id']
        intent=co.one('SELECT * FROM execution_intents');assert intent['attempted_at'] is not None
        with pytest.raises(AlpacaError): await co.dispatch(intent['id'])
        assert len(b.sent)==1
    try: asyncio.run(run())
    finally: co.close()
