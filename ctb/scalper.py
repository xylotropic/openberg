"""Session-switched paper strategy. Closed candles, bounded spend, shared execution."""
from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
import uuid
from datetime import datetime, timezone

from . import indicators as ind
from .alpha.classifier import choice
from .broker.alpaca import timestamp
from .kairos.client import SystemOneClient
from .models import MODELS

VERSION = 'candles-session-v1'
EQUITIES = ['TSLA', 'AAPL', 'GOOGL', 'MSFT', 'NVDA', 'AMD', 'INTC']
CRYPTO = ['BTC/USD','ETH/USD','SOL/USD']
SYMBOLS = [*EQUITIES,*CRYPTO]
MODEL = 'drex-max-finance'
PRICE = .03 / 1_000_000
RESERVE = 262144 * PRICE
CAP = 2.0
SCHEMA = '''
CREATE TABLE IF NOT EXISTS scalper_calls (
 id TEXT PRIMARY KEY, version TEXT NOT NULL, started_at REAL NOT NULL,
 request_json TEXT NOT NULL, response_json TEXT, state TEXT NOT NULL,
 reserved_usd REAL NOT NULL, actual_usd REAL, input_tokens INTEGER,
 completed_at REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS scalper_state(id TEXT PRIMARY KEY,value_json TEXT NOT NULL);
'''


def compact(value):
    if isinstance(value, float):
        if not math.isfinite(value): raise ValueError('Nonfinite market value')
        return round(value, 6)
    if isinstance(value, dict): return {k: compact(v) for k, v in value.items()}
    if isinstance(value, list): return [compact(v) for v in value]
    return value


def packet(st, now):
    """Use elapsed minutes, never silently relabel sparse observed bars as minutes."""
    rows = sorted({b.t: b for b in st.closed_bars('1') if b.t + 60000 <= now * 1000}.values(), key=lambda b: b.t)
    if len(rows) < 130: raise ValueError('Waiting for 130 closed one-minute candles')
    rows = rows[-500:]
    for i in range(len(rows)-1,0,-1):
        if rows[i].t-rows[i-1].t != 60000:
            rows=rows[i:];break
    if len(rows)<130: raise ValueError('Waiting for 130 consecutive closed one-minute candles')
    tail = rows[-130:]
    if any(b.t - a.t != 60000 for a, b in zip(tail, tail[1:])):
        raise ValueError('Gap in the last 130 one-minute candles')
    if now - (rows[-1].t / 1000 + 60) > 90:
        raise ValueError('Closed one-minute candles are stale')
    for b in rows:
        values = (b.o, b.h, b.l, b.c, b.v, b.turnover)
        if not all(math.isfinite(v) for v in values) or min(b.o,b.h,b.l,b.c) <= 0 or b.v < 0 or b.h < max(b.o,b.c,b.l) or b.l > min(b.o,b.c):
            raise ValueError('Invalid OHLCV candle')
    # Derive all windows from the same minute history, excluding incomplete buckets.
    def window(minutes, size):
        groups = {}
        for b in rows:
            at = b.t // (minutes * 60000) * (minutes * 60000)
            groups.setdefault(at, []).append(b)
        complete = []
        for at, group in sorted(groups.items()):
            if len(group) == minutes and group[0].t == at and group[-1].t == at + (minutes-1)*60000 and at + minutes*60000 <= now*1000:
                complete.append([at // 1000, group[0].o, max(b.h for b in group), min(b.l for b in group), group[-1].c, sum(b.v for b in group)])
        if len(complete) < size: raise ValueError(f'Waiting for {size} complete {minutes}-minute candles')
        chosen = complete[-size:]
        if any(b[0]-a[0] != minutes*60 for a,b in zip(chosen,chosen[1:])):
            raise ValueError(f'Gap in the {minutes}-minute window')
        return chosen
    close = [b.c for b in rows]; high = [b.h for b in rows]; low = [b.l for b in rows]
    volume = [b.v for b in rows]; v60 = sum(volume[-60:]); avgvol = statistics.mean(volume[-21:-1])
    macd = ind.macd(close)
    features = {'ema9': ind.ema(close,9), 'ema21': ind.ema(close,21), 'rsi7':ind.rsi(close,7), 'rsi14':ind.rsi(close,14),
        'atr14':ind.atr(high,low,close), 'vwap60':sum(b.turnover for b in rows[-60:])/v60 if v60 else None,
        'bollinger_b_20_2':ind.bollinger_pos(close), 'bollinger_width_pct_20_2':ind.bollinger_width_pct(close),
        'macd_hist_12_26_9':macd[2] if macd else None, 'relative_volume_prior20':volume[-1]/avgvol if avgvol else None,
        **{f'return_{n}m_pct':ind.pct_change(close,n) for n in (1,5,15,60)},
        'realized_vol_60m_pct_per_minute':ind.realized_vol_pct(close,60), 'hour_range_position':ind.range_position(high,low,close[-1],60)}
    if any(v is None for v in features.values()): raise ValueError('Indicator warmup or volume is insufficient')
    return compact({'source':'Alpaca US spot' if '/' in st.symbol else 'IEX only', 'columns':['open_epoch_s','o','h','l','c','volume'],
        'closed_1m':window(1,30), 'closed_5m':window(5,12), 'closed_15m':window(15,8), 'indicators':features,
        'warmup_bars':len(rows), 'last_close_at':rows[-1].t//1000+60})


def questions(symbols):
    actions = {'LONG':'Target a long position; keep an existing long, never add to it.',
        'SHORT':'Target a short share position; requires current shortable and easy-to-borrow status.',
        'HOLD':'Keep the owned position, or stay flat when none is owned.', 'EXIT':'Close only the current strategy-owned position.'}
    return {s:{'type':'choice','instructions':f'Choose the action for {s} over the next 30 minutes after spread and costs. Use only the supplied as-of data. Choose HOLD when evidence is weak. Spot crypto is long-only: bearish means EXIT when long, otherwise HOLD. No guaranteed return is assumed.',
        'criteria':{k:v for k,v in actions.items() if k!='SHORT' or s in EQUITIES}} for s in symbols}


class Spend:
    """Reserve worst-case cost atomically. Failed/unknown calls keep the reservation."""
    def __init__(self, db, hourly):
        self.db, self.hourly = db, hourly
        with db.lock: db.conn.executescript(SCHEMA)

    def bounds(self, now):
        d = datetime.fromtimestamp(now, timezone.utc)
        start = d.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        end = start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1)
        return start.timestamp(), end.timestamp()

    def spent(self, now=None):
        start,_ = self.bounds(now or time.time())
        own = self.db.one('SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0) n FROM scalper_calls WHERE started_at>=?',[start])['n']
        # The retired experiment shares the user's ceiling; its history stays intact.
        alpha = 0
        if self.db.one("SELECT name FROM sqlite_master WHERE name='alpha_classifications'"):
            alpha = self.db.one('SELECT COALESCE(SUM(COALESCE(actual_usd,reserved_usd)),0) n FROM alpha_classifications WHERE started_at>=?',[start])['n']
        return float(own + alpha)

    def reserve(self, state, qs, now=None):
        now = now or time.time(); identity = uuid.uuid4().hex
        payload = json.dumps({'model':MODEL,'state':state,'questions':qs},separators=(',',':'),allow_nan=False)
        with self.db.lock:
            self.db.conn.execute('BEGIN IMMEDIATE')
            try:
                if self.spent(now) + RESERVE > CAP + 1e-12: raise ValueError('Monthly Drex budget exhausted')
                if not self.hourly.try_take(): raise ValueError('Hourly model budget exhausted')
                self.db.conn.execute('INSERT INTO scalper_calls(id,version,started_at,request_json,state,reserved_usd) VALUES(?,?,?,?,?,?)',
                    [identity,VERSION,now,payload,'reserved',RESERVE])
                self.db.conn.execute('COMMIT')
            except BaseException:
                self.db.conn.execute('ROLLBACK'); raise
        return identity

    def settle(self, identity, result):
        tokens = result.usage.get('input_tokens')
        actual = tokens*PRICE if type(tokens) is int and 0<=tokens<=262144 else None
        with self.db.lock:
            self.db.conn.execute('UPDATE scalper_calls SET response_json=?,state=?,actual_usd=?,input_tokens=?,completed_at=? WHERE id=?',
                [json.dumps(result.raw,allow_nan=False),'complete',actual,tokens if actual is not None else None,time.time(),identity])

    def interval(self, now=None):
        now=now or time.time();start,end=self.bounds(now)
        rows=self.db.query('SELECT request_json,actual_usd,reserved_usd FROM scalper_calls ORDER BY started_at DESC LIMIT 20')
        cost=max((float(r['actual_usd'] if r['actual_usd'] is not None else r['reserved_usd'])/max(1,len(r['request_json'].get('state',{}).get('symbols',{}))) for r in rows),default=.0001)
        # Pace a full month, even when the service is enabled part-way through it.
        rth=23*6.5*3600
        ticker_seconds=7*rth+3*max(0,end-start-rth)
        return max(30, math.ceil(cost * ticker_seconds / CAP * 1.15 / 30)*30)


class Scalper:
    def __init__(self, engine):
        self.e, self.db = engine, engine.db
        self.spend=Spend(engine.db,engine.budget)
        self.client=SystemOneClient(MODELS['drex'],engine.session,model=MODEL,timeout_s=25,max_retries=0)
        self.lock=asyncio.Lock(); self.next_call=0; self.last_scan=0; self.last_error=None; self.readiness={}; self.task=None

    def state(self,key,value=None,write=False):
        if write:
            with self.db.lock: self.db.conn.execute('INSERT INTO scalper_state VALUES(?,?) ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json',[key,json.dumps(value)])
        row=self.db.one('SELECT value_json FROM scalper_state WHERE id=?',[key])
        return json.loads(row['value_json']) if row else None

    async def start(self):
        if self.client.url!='https://console.nace.ai/v1/systemone':
            raise ValueError('Session strategy requires the verified Nace endpoint')
        if self.e.run['config_json'].get('qset')!=VERSION:
            from .runs import start
            self.e.run=start(self.db,self.e.s,name='Session strategy · stocks / crypto · 5% allocation')
        row=self.db.one('SELECT started_at FROM scalper_calls ORDER BY started_at DESC LIMIT 1')
        self.next_call=(row['started_at']+self.spend.interval()) if row else 0
        self.task=asyncio.create_task(self.loop(),name='session_strategy')

    async def stop(self):
        if self.task:
            self.task.cancel(); await asyncio.gather(self.task,return_exceptions=True)

    def owned(self,symbol):
        return self.db.query("SELECT t.* FROM trades t JOIN decisions d ON d.id=t.decision_id WHERE t.venue='alpaca' AND t.account_id=? AND t.symbol=? AND t.status IN ('opening','open','closing') AND d.qset_version=?",[self.e.s.alpaca_account_id,symbol,VERSION])

    def quote_ready(self,symbol,now):
        st=self.e.store[symbol]
        bid=float(st.ticker.get('bid1Price') or 0);ask=float(st.ticker.get('ask1Price') or 0)
        if now-st.book_ts>5 or st.book_ts>now+2 or not 0<bid<=ask: raise ValueError('Quote is stale or crossed')
        spread=(ask-bid)/((ask+bid)/2)*10000
        if spread>(20 if symbol in CRYPTO else 5): raise ValueError('Spread exceeds entry cap')
        return bid,ask,spread

    def build(self,now):
        symbols={}; self.readiness={}
        active=EQUITIES if self.e.feed.clock.get('is_open') else CRYPTO
        for symbol in SYMBOLS:
            try:
                if symbol not in self.e.s.symbols: raise ValueError('Not in active strategy universe')
                if symbol not in active: raise ValueError('US market closed' if symbol in EQUITIES else 'Inactive during US stock session')
                data=packet(self.e.store[symbol],now)
                bid,ask,spread=self.quote_ready(symbol,now)
                owned=self.owned(symbol)
                data.update(quote={'bid':bid,'ask':ask,'at':self.e.store[symbol].book_ts,'spread_bp':spread},
                    position={'side':owned[0]['side'],'status':owned[0]['status']} if owned else {'side':'flat'},
                    estimated_additional_roundtrip_cost_bp=50 if symbol in CRYPTO else 2, max_holding_minutes=30)
                symbols[symbol]=data;self.readiness[symbol]='READY'
            except ValueError as exc: self.readiness[symbol]=str(exc)
        return compact({'strategy':VERSION,'as_of':now,'purpose':'Alpaca PAPER decisions; no demonstrated profitability',
            'rules':'LONG/SHORT are target directions, not repeated orders. HOLD maintains the position or stays flat. EXIT reduces only owned inventory. No pyramiding. Opposite targets close first and wait for a later decision before opening. Crypto is long-only spot.',
            'symbols':symbols})

    async def loop(self):
        while True:
            started=time.time()
            try: await self.tick()
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.last_error=str(exc)[:300]
                self.e.emit('event',self.db.event('warn','scalper',self.last_error))
            await asyncio.sleep(max(1,30-(time.time()-started)))

    async def tick(self):
        async with self.lock:
            now=time.time(); self.last_scan=now
            # The broker clock governs holidays and early closes. Never infer a session
            # from local wall-clock hours or from the previous model call.
            clock=await self.e.coordinator.clock()
            self.e.feed.clock=clock
            active=EQUITIES if clock.get('is_open') else CRYPTO
            transitioning=False
            for symbol in SYMBOLS:
                owned=self.owned(symbol)
                if symbol not in active and owned:
                    transitioning=True
                    if len(owned)==1:
                        await self.e.executor.close(symbol,'session_switch',expected_trade_id=owned[0]['id'])
            state=self.build(now)
            if transitioning or not state['symbols'] or not self.e.s.trading_enabled or not self.client.configured or self.state('model_blocked') or now<self.next_call: return
            # A single batch replaces the old per-symbol/mirrored calls.
            qs=questions(state['symbols']);planned_interval=self.spend.interval(now)
            identity=self.spend.reserve(state,qs)
            self.next_call=now+planned_interval
            try:
                result=await self.client.ask(state,qs)
                if result.metadata.get('model')!=MODEL:
                    self.state('model_blocked',{'at':time.time(),'reason':'Unexpected responding model'},write=True)
                    raise ValueError('Unexpected responding model; further calls blocked')
                self.spend.settle(identity,result)
                self.next_call=now+self.spend.interval(now)
                actions={s:choice(result.answers,s,qs[s]['criteria']) for s in qs}
                if time.time()-now>25: raise ValueError('Drex decision expired before execution')
                self.last_error=None
                for symbol,(action,probability) in actions.items():
                    await self.apply(symbol,action,probability,state,result,identity)
            except Exception as exc:
                with self.db.lock:
                    message=str(exc).replace(MODELS['drex'].api_key,'[redacted]') if MODELS['drex'].api_key else str(exc)
                    self.db.conn.execute("UPDATE scalper_calls SET state='failed',error=?,completed_at=? WHERE id=?",[message[:300],time.time(),identity])
                raise ValueError(message[:300]) from None

    async def apply(self,symbol,action,probability,state,result,identity):
        now=time.time(); data=state['symbols'][symbol]; features=data['indicators']; owned=self.owned(symbol)
        sl=max(100 if symbol in CRYPTO else 20,features['atr14']/data['closed_1m'][-1][4]*10000*1.5)
        tp=sl*2;reason='Drex target '+action;actual='hold';ex=self.e.executor;execution=None
        if now-state['as_of']>25:
            reason='Drex signal expired; local exits remain active'
        elif owned and owned[0]['status'] in ('open','opening') and (action=='EXIT' or action in ('LONG','SHORT') and action.lower()!=owned[0]['side']):
            flat=await ex.close(symbol,'drex_exit' if action=='EXIT' else 'drex_opposite',expected_trade_id=owned[0]['id']);actual='exit'
            execution={'close_requested':True,'confirmed_flat':bool(flat)}
        elif not owned and action in ('LONG','SHORT'):
            try:
                if not self.e.s.trading_enabled: raise ValueError('Entries paused')
                if symbol not in SYMBOLS or symbol not in self.e.s.symbols: raise ValueError('Symbol outside active universe')
                if symbol in CRYPTO and action=='SHORT': raise ValueError('Alpaca spot crypto cannot open shorts')
                if time.time()-state['as_of']>25: raise ValueError('Drex signal expired')
                fresh=packet(self.e.store[symbol],now)
                if fresh['last_close_at']!=data['last_close_at']: raise ValueError('Candle changed while Drex was running')
                self.quote_ready(symbol,now)
                if probability<.6: raise ValueError('Action weight below 0.60 gate; uncalibrated score')
                asset=await ex.client.asset(symbol)
                if action=='SHORT' and not (asset.get('shortable') and asset.get('easy_to_borrow')): raise ValueError('Stock is not easy to borrow')
                clock=await self.e.coordinator.clock()
                if symbol in EQUITIES:
                    if not clock.get('is_open') or now+35*60>timestamp(clock.get('next_close')): raise ValueError('Insufficient stock session time for a 30-minute hold')
                elif clock.get('is_open') or now+35*60>timestamp(clock.get('next_open')):
                    raise ValueError('Crypto entries pause before the US stock session')
                cooldown=self.db.one('SELECT MAX(t.closed_ts) at FROM trades t JOIN decisions d ON t.decision_id=d.id WHERE t.symbol=? AND d.qset_version=?',[symbol,VERSION])['at']
                if cooldown and now-cooldown<300: raise ValueError('Five-minute entry cooldown')
                actual=action.lower()
            except ValueError as exc: reason=str(exc)
        decision={'reason':reason,'strategy':VERSION,'model_action':action,'action_weight':probability,'call_id':identity,'signal_at':state['as_of'],
            'cost_bp':(50 if symbol in CRYPTO else 2)+data.get('quote',{}).get('spread_bp',0),
            'meta':{'source':data['source']},'events':['session_candles'],'execution':execution}
        did=self.db.insert('decisions',{'ts':now,'symbol':symbol,'bar_time':data['last_close_at']*1000,'qset_version':VERSION,
            'decision_tf':'1','run_id':self.e.run['id'],'model_provider':'drex','state_json':data,'questions_json':{symbol:questions([symbol])[symbol]},
            'answers_json':{symbol:result.answers[symbol]},'decision_json':decision,'action':actual,'price':data['closed_1m'][-1][4],
            'sl_bp':sl,'tp_bp':tp,'horizon_min':30,'latency_ms':result.latency_ms})
        if actual in ('long','short'):
            response=await ex.open(symbol=symbol,side=actual,sl_bp=sl,tp_bp=tp,decision_id=did,ev_r=0,
                decision_price=data['closed_1m'][-1][4],horizon_min=30,events=['session_candles'])
            decision=self.db.one('SELECT decision_json FROM decisions WHERE id=?',[did])['decision_json']
            decision['execution']=response
            if not response.get('ok'):
                decision['reason']=response.get('reason') or response.get('status')
                actual='pending' if any(t['status']=='opening' for t in self.owned(symbol)) else 'hold'
            self.db.update('decisions',did,{'action':actual,'decision_json':decision})
        self.e.emit('decision',self.e._decision_view(self.db.one('SELECT * FROM decisions WHERE id=?',[did])))

    def status(self):
        return {'version':VERSION,'symbols':SYMBOLS,'paper':True,'enabled':self.e.s.trading_enabled,
            'active_symbols':EQUITIES if self.e.feed.clock.get('is_open') else CRYPTO,
            'session':'US STOCKS' if self.e.feed.clock.get('is_open') else 'CRYPTO',
            'scan_seconds':30,'drex_interval_seconds':self.spend.interval(),'next_call_at':self.next_call,'last_scan_at':self.last_scan,
            'monthly_cap_usd':CAP,'month_spent_usd':self.spend.spent(),'error':self.last_error,'readiness':self.readiness,
            'risk':{'allocation_pct_per_position':5,'max_holding_minutes':30,'entry_cooldown_minutes':5},
            'decisions':self.db.query('SELECT id,ts,symbol,action,decision_json,latency_ms FROM decisions WHERE qset_version=? ORDER BY id DESC LIMIT 30',[VERSION]),
            'last_call':self.db.one('SELECT id,started_at,state,input_tokens,actual_usd,error FROM scalper_calls WHERE version=? ORDER BY started_at DESC LIMIT 1',[VERSION])}
