"""Owned fills, explicit cash flows and raw-price economic marks."""
from datetime import datetime, timedelta
from decimal import Decimal
import math
import random
import statistics
import time

from ..broker.alpaca import iso, timestamp
from .calendar import NY
from .config import VERSION, digest


def day_open(date):
    return datetime.fromisoformat(date).replace(hour=9,minute=30,tzinfo=NY).timestamp()


def ratios(values, *, minimum=20):
    """Daily excess returns; all observations enter the downside denominator."""
    n = len(values)
    result = {'observations':n,'frequency':'exchange-session daily','cash_benchmark_annual':0,
              'target_annual':0,'annualization':'252 sessions; descriptive, with block-bootstrap uncertainty',
              'sharpe':None,'sortino':None,'mean_daily':None,'expected_shortfall_95':None,'ci_sharpe_95':None,
              'status':'insufficient_data'}
    if n < minimum: return result
    mean, sd = statistics.mean(values), statistics.stdev(values)
    downside = math.sqrt(sum(min(v,0)**2 for v in values)/n)
    result.update(mean_daily=mean,expected_shortfall_95=statistics.mean(sorted(values)[:max(1,math.ceil(.05*n))]))
    if sd <= 1e-12 or sum(v<0 for v in values)<3:
        result['status']='insufficient_variation_or_downside'
        return result
    result.update(sharpe=mean/sd*math.sqrt(252),sortino=mean/downside*math.sqrt(252),status='descriptive_small_sample' if n<126 else 'descriptive')
    rng = random.Random(871)
    samples=[]
    block=10
    for _ in range(400):
        sample=[]
        while len(sample)<n:
            start=rng.randrange(n)
            sample.extend(values[(start+k)%n] for k in range(block))
        sample=sample[:n]
        sigma=statistics.stdev(sample)
        if sigma>1e-12: samples.append(statistics.mean(sample)/sigma*math.sqrt(252))
    if samples:
        samples.sort();result['ci_sharpe_95']=[samples[int(len(samples)*.025)],samples[min(len(samples)-1,int(len(samples)*.975))]]
    return result


def factor_alpha(strategy, market, sector):
    """OLS intercept with SPY and XLK controls, plus Newey-West standard error."""
    n=len(strategy)
    if n<60 or len(market)!=n or len(sector)!=n: return {'status':'insufficient_data','alpha_daily':None,'controls':['SPY','XLK'],'observations':n}
    xm, xs, ym=statistics.mean(market),statistics.mean(sector),statistics.mean(strategy)
    mm=sum((x-xm)**2 for x in market); ss=sum((x-xs)**2 for x in sector)
    ms=sum((x-xm)*(z-xs) for x,z in zip(market,sector))
    my=sum((x-xm)*(y-ym) for x,y in zip(market,strategy));sy=sum((z-xs)*(y-ym) for z,y in zip(sector,strategy))
    det=mm*ss-ms*ms
    if abs(det)<1e-16: return {'status':'singular_controls','alpha_daily':None,'observations':n}
    bm,bs=(my*ss-sy*ms)/det,(sy*mm-my*ms)/det
    intercept=ym-bm*xm-bs*xs
    # Influence of each observation on the intercept; Bartlett HAC lag 10.
    influence=[(1/n-(xm*ss-xs*ms)*(m-xm)/det-(xs*mm-xm*ms)*(s-xs)/det)*(y-intercept-bm*m-bs*s) for y,m,s in zip(strategy,market,sector)]
    variance=sum(v*v for v in influence)
    for lag in range(1,min(10,n-1)+1):
        variance+=2*(1-lag/11)*sum(influence[t]*influence[t-lag] for t in range(lag,n))
    se=math.sqrt(max(0,variance)*n/(n-3))
    return {'status':'descriptive','alpha_daily':intercept,'alpha_annual_arithmetic':252*intercept,
            'ci_daily_95':[intercept-1.96*se,intercept+1.96*se],'market_beta':bm,'sector_beta':bs,
            'controls':['SPY','XLK'],'observations':n,'uncertainty':'Newey-West Bartlett lag 10'}


class Accounting:
    def __init__(self, service):
        self.svc,self.j,self.client,self.c=service,service.j,service.client,service.config

    def fills(self, *, symbol=None, until=None, group=None):
        sql='SELECT * FROM alpha_fills WHERE 1=1';args=[]
        for key,value in [('symbol',symbol),('group_id',group)]:
            if value is not None: sql+=' AND '+key+'=?';args.append(value)
        if until is not None: sql+=' AND filled_at<?';args.append(until)
        return self.j.query(sql+' ORDER BY filled_at,id',args)

    def cashflow(self, identity, at, kind, amount, *, group=None, symbol=None, source=None, metadata=None):
        self.j.insert('alpha_cashflows',{'id':identity,'version':VERSION,'at':at,'kind':kind,'amount':amount,
            'group_id':group,'symbol':symbol,'source_id':source,'metadata_json':metadata or {}})

    async def activities(self):
        version=self.svc.version()
        if not version.get('armed_at'): return
        # Replay from activation, not a high-water timestamp: late events and corrections can arrive out of order.
        params={'after':iso(version['armed_at']-1),'direction':'asc','page_size':100}
        all_rows=[];tokens=set()
        for _ in range(100):
            rows=await self.client.request('GET','/v2/account/activities',params=params)
            all_rows.extend(rows)
            if len(rows)<100: break
            token=rows[-1]['id']
            if token in tokens: raise ValueError('Repeated account-activity page token')
            tokens.add(token);params['page_token']=token
        else: raise ValueError('Account-activity pagination bound reached; coverage is incomplete')
        for row in all_rows:
            at=timestamp(row.get('transaction_time')) if row.get('transaction_time') else day_open(row.get('date',datetime.now(NY).date().isoformat()))
            content=digest(row);identity=digest([row['id'],content])
            self.j.insert('alpha_broker_activities',{'id':identity,'external_id':row['id'],'observed_at':time.time(),'at':at,
                'kind':row['activity_type'],'symbol':row.get('symbol'),'order_id':row.get('order_id'),'raw_json':row,'content_hash':content})
            if row['activity_type']=='FILL':
                order=next((o for o in self.j.query('SELECT * FROM alpha_orders') if (o.get('broker_json') or {}).get('id')==row.get('order_id')),None)
                if order: self.apply_fill(order,row,at)
                else:
                    lease=self.svc.coordinator.one('SELECT * FROM execution_leases WHERE account_id=? AND resource=?',[self.svc.coordinator.account_id,(row.get('symbol') or '').replace('/','')])
                    if lease and lease['owner_type']=='alpha':
                        self.svc.coordinator.state('account_quarantine',{'reason':'External fill on a strategy-owned symbol','activity_id':row['id']},write=True)
            elif row.get('symbol') and row['activity_type'] not in ('FILL',):
                historical=self.fills(symbol=row['symbol'],until=at+86400)
                if historical and row.get('net_amount') is not None:
                    # Exact corporate-action reference is needed to replace an accrual without guessing.
                    caid=row.get('corporate_action_id')
                    owned_order=next((o for o in self.j.query('SELECT * FROM alpha_orders') if row.get('order_id') and (o.get('broker_json') or {}).get('id')==row['order_id']),None)
                    if not caid and not owned_order:
                        self.j.state('accounting_gap',{'reason':'Cash activity needs an exact entitlement reference','id':row['id']},write=True)
                        continue
                    previous=self.j.query("SELECT * FROM alpha_cashflows WHERE kind='broker_cash' AND json_extract(metadata_json,'$.external_id')=?",[row['id']])
                    delta=float(row['net_amount'])-sum(p['amount'] for p in previous)
                    self.cashflow('broker:'+identity,at,'broker_cash',delta,symbol=row['symbol'],source=identity,
                        group=owned_order['group_id'] if owned_order else None,
                        metadata={'corporate_action_id':caid,'activity_type':row['activity_type'],'external_id':row['id'],'revision_delta':True})
        self.j.state('activity_coverage',{'from':version['armed_at'],'through':time.time(),'rows':len(all_rows),'complete':True},write=True)

    def apply_fill(self, order, activity, at):
        identity='alpaca:'+activity['id']
        previous=self.j.one('SELECT * FROM alpha_fills WHERE id=?',[identity])
        if previous:
            if previous['source_json'] != activity:
                self.j.state('accounting_gap',{'reason':'Broker revised a booked fill','fill':identity},write=True)
            return
        qty=Decimal(activity['qty']);signed=qty if activity['side']=='buy' else -qty
        if qty<=0 or Decimal(order['request_json']['qty']) < qty: raise ValueError('Invalid owned fill quantity')
        price=float(activity['price'])
        with self.j.db.lock:
            self.j.db.conn.execute('BEGIN IMMEDIATE')
            try:
                self.j.insert('alpha_fills',{'id':identity,'order_id':order['id'],'group_id':order['group_id'],'account_id':order['account_id'],
                    'symbol':order['symbol'],'filled_at':at,'qty':str(qty),'signed_qty':str(signed),'price':price,'cash_flow':-float(signed)*price,'source_json':activity})
                self.rebuild_position(order['group_id'],order['symbol'])
                self.cashflow('latency:'+identity,at,'modeled_latency',-float(qty)*price*self.c['extra_latency_slippage_bp']/10000,
                    group=order['group_id'],symbol=order['symbol'],source=identity,metadata={'assumed_bp':self.c['extra_latency_slippage_bp'],'spread_charged_again':False})
                group=self.j.one('SELECT * FROM alpha_groups WHERE id=?',[order['group_id']])
                first=min(f['filled_at'] for f in self.fills(group=order['group_id']))
                deadline=self.svc.sessions.exit_deadline(first,self.c['holding_sessions'])
                self.j.update('alpha_groups',group['id'],{'exit_deadline':deadline,'recovery_deadline':first+self.c['recovery_s']})
                if group['state'] in ('closed','canceled') and any(Decimal(p['qty']) for p in self.j.query('SELECT qty FROM alpha_positions WHERE group_id=?',[group['id']])):
                    self.j.update('alpha_groups',group['id'],{'state':'overdue','reason':'Late fill reopened a strategy-owned exit obligation'})
                self.j.db.conn.execute('UPDATE alpha_positions SET exit_deadline=? WHERE group_id=?',[deadline,group['id']])
                self.j.db.conn.execute('COMMIT')
            except BaseException:
                self.j.db.conn.execute('ROLLBACK');raise
        restored=self.j.one('SELECT * FROM alpha_groups WHERE id=?',[order['group_id']])
        if restored['state']=='overdue':
            try: self.svc.coordinator.reserve_group(restored['id'],[l['symbol'] for l in restored['target_json']['legs']])
            except ValueError:
                self.svc.coordinator.state('account_quarantine',{'reason':'A late fill conflicts with a newer resource owner','group':restored['id']},write=True)

    def rebuild_position(self, group_id, symbol):
        qty=Decimal(0);basis=0.;realized=0.
        for fill in self.fills(symbol=symbol,group=group_id):
            change=Decimal(fill['signed_qty']);px=fill['price']
            if not qty or (qty>0)==(change>0):
                basis=(abs(float(qty))*basis+abs(float(change))*px)/abs(float(qty+change))
            else:
                if abs(change)>abs(qty): raise ValueError('An owned exit would reverse the position')
                realized+=min(abs(float(qty)),abs(float(change)))*(px-basis)*(1 if qty>0 else -1)
            qty+=change
        group=self.j.one('SELECT * FROM alpha_groups WHERE id=?',[group_id])
        leg=next(x for x in group['target_json']['legs'] if x['symbol']==symbol)
        pid=digest([group_id,symbol])
        row={'id':pid,'version':VERSION,'group_id':group_id,'account_id':group['account_id'],'symbol':symbol,'qty':str(qty),
             'average':basis,'realized':realized,'exit_deadline':group.get('exit_deadline'),'state':'open' if qty else 'flat',
             'metadata_json':{'beta':leg['beta'],'sector':group['target_json']['sector']}}
        self.j.insert('alpha_positions',row);self.j.update('alpha_positions',pid,{k:v for k,v in row.items() if k!='id'})

    async def corporate_actions(self):
        version=self.svc.version();now=time.time()
        symbols=sorted(set(['SWKS','CRUS','XLK','SPY',*(f['symbol'] for f in self.fills())]))
        start=datetime.fromtimestamp(version.get('armed_at') or now,NY).date()-timedelta(days=45)
        params={'symbols':','.join(symbols),'start':str(start),'end':str(datetime.now(NY).date()+timedelta(days=120)),
                'data_quality':'all','limit':1000,'sort':'asc'}
        tokens=set();count=0
        for _ in range(30):
            response=await self.client.request('GET','/v1/corporate-actions',data_api=True,params=params)
            for kind,rows in response.get('corporate_actions',{}).items():
                for row in rows:
                    content=digest(row);effective=day_open(row.get('ex_date') or row.get('process_date'))
                    self.j.insert('alpha_corporate_actions',{'id':digest([row['id'],content]),'external_id':row['id'],'observed_at':now,
                        'kind':kind.rstrip('s'),'symbol':row.get('symbol'),'effective_at':effective,'raw_json':row,'content_hash':content})
                    count+=1
            token=response.get('next_page_token')
            if not token: break
            if token in tokens: raise ValueError('Repeated corporate-action page token')
            tokens.add(token);params['page_token']=token
        else: raise ValueError('Corporate-action coverage is incomplete')
        self.j.state('corporate_action_coverage',{'from':str(start),'through':time.time(),'records':count,'provider':'Alpaca corporate actions','delays_possible':True},write=True)

    def actions(self):
        rows=self.j.query('SELECT * FROM alpha_corporate_actions ORDER BY observed_at DESC')
        seen=set();latest=[]
        for r in rows:
            if r['external_id'] not in seen: latest.append(r);seen.add(r['external_id'])
        return latest

    def economics(self, until, marks):
        fills=self.fills(until=until+1)
        cash=sum(f['cash_flow'] for f in fills)
        raw_qty={s:sum(float(f['signed_qty']) for f in fills if f['symbol']==s) for s in {f['symbol'] for f in fills}}
        raw_value=sum(q*marks[s]['price'] for s,q in raw_qty.items())
        economic_qty=dict(raw_qty);dividends=0.;gaps=[]
        for action in sorted(self.actions(),key=lambda a:a['effective_at']):
            if action['effective_at']>until: continue
            row=action['raw_json'];symbol=action['symbol']
            entitled=sum(float(f['signed_qty']) for f in fills if f['symbol']==symbol and f['filled_at']<action['effective_at'])
            if not entitled: continue
            if action['kind'] in ('cash_dividend','cash_dividend_'):
                if row.get('currency','USD')!='USD' or not row.get('ex_date') or row.get('rate') is None:
                    gaps.append(action['id']);continue
                dividends+=entitled*float(row['rate'])
            elif action['kind'] in ('forward_split','reverse_split'):
                # Preserve broker quantities. Economic share adjustment is separately visible.
                factor=float(row['new_rate'])/float(row['old_rate'])
                economic_qty[symbol]=economic_qty.get(symbol,0)+entitled*(factor-1)
                gaps.append(action['id'])
            else: gaps.append(action['id'])
        actual=sum(r['amount'] for r in self.j.query("SELECT * FROM alpha_cashflows WHERE kind='broker_cash' AND at<=?",[until]))
        # Actual dividend cash replaces the accrual when an exact CA reference exists.
        actual_div=sum(r['amount'] for r in self.j.query("SELECT * FROM alpha_cashflows WHERE kind='broker_cash' AND at<=?",[until])
            if (r.get('metadata_json') or {}).get('corporate_action_id') and r['metadata_json'].get('activity_type','').startswith('DIV'))
        extra_cost=-sum(r['amount'] for r in self.j.query("SELECT * FROM alpha_cashflows WHERE kind='modeled_latency' AND at<=?",[until]))
        borrow=0.;financing=0.
        # Integrate historical holdings over calendar time, including weekends and closed positions.
        by_symbol={s:[f for f in fills if f['symbol']==s] for s in raw_qty}
        for symbol,events in by_symbol.items():
            qty=0.;last=events[0]['filled_at'] if events else until;px=0.
            for f in events+[{'filled_at':until,'signed_qty':'0','price':0}]:
                days=max(0,f['filled_at']-last)/86400
                if qty<0: borrow+=abs(qty)*px*self.c['borrow_annual_assumption']*days/365
                qty+=float(f['signed_qty']);last=f['filled_at'];px=f['price'] or px
        running_cash=self.svc.version()['allocation'];last=fills[0]['filled_at'] if fills else until
        for f in fills+[{'filled_at':until,'cash_flow':0}]:
            financing+=max(0,-running_cash)*self.c['financing_annual_assumption']*max(0,f['filled_at']-last)/(365*86400)
            running_cash+=f['cash_flow'];last=f['filled_at']
        allocation=self.svc.version()['allocation']
        gross=allocation+cash+raw_value+actual
        net=allocation+cash+sum(q*marks[s]['price'] for s,q in economic_qty.items())+actual+dividends-actual_div-extra_cost-borrow-financing
        return {'gross_nav':gross,'net_nav':net,'costs':extra_cost+borrow+financing,'dividend_accrual':dividends,
                'borrow':borrow,'financing':financing,'gaps':gaps,'quantities':raw_qty,'economic_quantities':economic_qty}

    async def mark(self, at=None, session=None):
        now=at or time.time();fills=self.fills(until=now+1)
        symbols=sorted({f['symbol'] for f in fills})
        marks={}
        for symbol in symbols:
            if at is not None:
                result=await self.client.request('GET','/v2/stocks/bars',data_api=True,params={'symbols':symbol,'timeframe':'1Day',
                    'start':iso(now-6*86400),'end':iso(now+1),'feed':'iex','adjustment':'raw','limit':10,'sort':'desc'})
                rows=result.get('bars',{}).get(symbol,[])
                if not rows: raise ValueError('Daily raw mark is missing for '+symbol)
                row=rows[0];price=float(row['c']);source_at=timestamp(row['t'])
                if session and row['t'][:10]!=session: raise ValueError('A daily raw mark is stale for '+symbol)
            else:
                snap=(await self.client.snapshots([symbol],'us_equity')).get(symbol,{})
                trade=snap.get('latestTrade') or {};price=float(trade.get('p') or 0);source_at=timestamp(trade.get('t'))
                if price<=0: raise ValueError('A raw position mark is missing for '+symbol)
                if self.svc.sessions.current(now) and now-source_at>60: raise ValueError('An intraday mark is stale for '+symbol)
            marks[symbol]={'price':price,'source_at':source_at,'feed':'iex','adjustment':'raw'}
        result=self.economics(now,marks)
        if result['gaps']: self.j.state('accounting_gap',{'reason':'A noncash corporate action needs verified broker reconciliation','actions':result['gaps']},write=True)
        # Historical exposure must use holdings at that mark, including groups closed later.
        by_group={}
        for fill in fills:
            key=(fill['group_id'],fill['symbol']);by_group[key]=by_group.get(key,0)+float(fill['signed_qty'])
        gross=sum(abs(qty*marks[symbol]['price']) for (_,symbol),qty in by_group.items())
        net=sum(qty*marks[symbol]['price'] for (_,symbol),qty in by_group.items())
        beta=0.
        for (group_id,symbol),qty in by_group.items():
            group=self.j.one('SELECT target_json FROM alpha_groups WHERE id=?',[group_id])
            factor=next(l['beta'] for l in group['target_json']['legs'] if l['symbol']==symbol)
            beta+=qty*marks[symbol]['price']*factor
        result.update(marked_at=now,marks=marks,gross_exposure=gross,net_exposure=net,beta=beta/max(1,result['net_nav']))
        if session is None: self.j.state('live_nav',result,write=True)
        if session:
            identity=VERSION+':'+session;previous=self.j.one('SELECT * FROM alpha_nav WHERE id=?',[identity])
            values={'id':identity,'version':VERSION,'session':session,'marked_at':now,'gross_nav':result['gross_nav'],
                    'net_nav':result['net_nav'],'external_flow':0,'gross_exposure':gross,'net_exposure':net,'beta':result['beta'],
                    'costs':result['costs'],'status':'gap' if result['gaps'] or self.j.state('accounting_gap') else 'marked','marks_json':marks,
                    'assumptions_json':{'borrow_annual':self.c['borrow_annual_assumption'],'financing_annual':self.c['financing_annual_assumption'],
                    'latency_slippage_bp':self.c['extra_latency_slippage_bp'],'cash_benchmark_annual':0,'corporate_actions_may_arrive_late':True}}
            if previous:
                if digest({k:previous[k] for k in values})!=digest(values):
                    self.j.insert('alpha_nav_revisions',{'id':digest([identity,previous,values]),'session':session,'revised_at':time.time(),'previous_json':previous,'reason':'Late source/fill/corporate-action reconciliation'})
                    self.j.update('alpha_nav',identity,{k:v for k,v in values.items() if k!='id'})
            else: self.j.insert('alpha_nav',values)
        return result

    def metrics(self):
        if self.j.state('accounting_gap'): return {'status':'accounting_gap','observations':0,'sharpe':None,'sortino':None}
        nav=self.j.query('SELECT * FROM alpha_nav WHERE version=? ORDER BY session',[VERSION])
        values=[];prev=self.svc.version()['allocation'];peak=prev;dd=0
        for row in nav:
            if row['status']!='marked' or prev<=0: return {'status':'accounting_gap','observations':len(nav)}
            values.append((row['net_nav']-row['external_flow'])/prev-1)
            peak=max(peak,row['net_nav']);dd=max(dd,1-row['net_nav']/peak);prev=row['net_nav']
        result=ratios(values)
        result.update(max_drawdown=dd,independent_events=self.j.one('SELECT COUNT(DISTINCT event_id) n FROM alpha_groups WHERE exit_deadline IS NOT NULL')['n'],
            turnover_notional=sum(abs(float(f['qty'])*f['price']) for f in self.fills()),
            alpha={'status':'insufficient_factor_history','alpha_daily':None,'controls':['SPY','XLK']})
        return result

    async def measured_metrics(self):
        result=self.metrics()
        if result['status']=='accounting_gap': return result
        nav=self.j.query("SELECT * FROM alpha_nav WHERE version=? AND status='marked' ORDER BY session",[VERSION])
        if len(nav)<60: return result
        series={}
        for symbol in ('SPY','XLK'):
            first=day_open(nav[0]['session']);last=day_open(nav[-1]['session'])+86400
            response=await self.client.request('GET','/v2/stocks/bars',data_api=True,params={
                'symbols':symbol,'timeframe':'1Day','start':iso(first-10*86400),'end':iso(last),
                'feed':'iex','adjustment':'all','sort':'asc','limit':1000})
            if response.get('next_page_token'): return {**result,'alpha':{'status':'incomplete_factor_history','alpha_daily':None}}
            rows=response.get('bars',{}).get(symbol,[])
            series[symbol]={b['t'][:10]:float(b['c'])/float(a['c'])-1 for a,b in zip(rows,rows[1:])}
        strategy=[];market=[];sector=[];previous=self.svc.version()['allocation']
        for row in nav:
            if row['session'] not in series['SPY'] or row['session'] not in series['XLK']:
                return {**result,'alpha':{'status':'missing_aligned_factor_session','alpha_daily':None}}
            strategy.append((row['net_nav']-row['external_flow'])/previous-1);previous=row['net_nav']
            market.append(series['SPY'][row['session']]);sector.append(series['XLK'][row['session']])
        result['alpha']=factor_alpha(strategy,market,sector)
        return result
