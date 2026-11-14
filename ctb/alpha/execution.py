"""Durable stock/hedge groups and exits, independent of semantic classification."""
import asyncio
import time
from decimal import Decimal

from ..broker.alpaca import AlpacaError
from ..broker.coordinator import FINAL, resource, signed_position
from .config import VERSION, digest


class Execution:
    def __init__(self, service):
        self.svc,self.j,self.co,self.client=service,service.j,service.coordinator,service.client
        self.lock=asyncio.Lock()

    def groups(self):
        return self.j.query("SELECT * FROM alpha_groups WHERE state NOT IN ('closed','canceled','abstained') ORDER BY created_at")

    def orders(self, group):
        return self.j.query('SELECT * FROM alpha_orders WHERE group_id=? ORDER BY created_at',[group['id']])

    def positions(self, group):
        return self.j.query('SELECT * FROM alpha_positions WHERE group_id=?',[group['id']])

    async def plan(self, decision, relationship, window):
        async with self.lock:
            if self.j.one('SELECT * FROM alpha_groups WHERE version=? AND event_id=?',[VERSION,decision['event_id']]):
                raise ValueError('This customer event already has an execution group')
            target,risk=await self.svc.portfolio.construct(decision,relationship,window)
            event=self.j.one('SELECT * FROM alpha_events WHERE id=?',[decision['event_id']])
            deadline=min(window['end'],event['eligible_until'],window['session']['close'])
            if not self.svc.version()['enabled'] or time.time()>=deadline:
                raise ValueError('New entries were paused or expired during target construction')
            identity=digest([VERSION,decision['event_id']])
            self.j.insert('alpha_groups',{'id':identity,'version':VERSION,'event_id':decision['event_id'],'decision_id':decision['id'],
                'account_id':self.co.account_id,'state':'planned','created_at':time.time(),'entry_window_start':window['start'],
                'entry_deadline':deadline,'target_json':target,'risk_json':risk})
            try:
                self.co.reserve_group(identity,[leg['symbol'] for leg in target['legs']])
            except Exception as exc:
                self.j.update('alpha_groups',identity,{'state':'abstained','reason':str(exc)[:300]});raise
            # Complete desired holdings are durable before either order exists.
            self.j.update('alpha_groups',identity,{'state':'entering'})
            group=self.j.one('SELECT * FROM alpha_groups WHERE id=?',[identity])
            for leg in target['legs']:
                body={'symbol':leg['symbol'],'qty':str(leg['qty']),'side':leg['side'],'type':'limit',
                      'time_in_force':'day','extended_hours':False,
                      'limit_price':str(round(target['quotes'][leg['symbol']]['ask' if leg['side']=='buy' else 'bid'],2))}
                self.make_order(group,leg,'entry',body,deadline=deadline,effect='increase',expected=0,maximum=leg['qty'])
            # Both legs remain covered by the same ownership reservation through any failure.
            for order in self.orders(group):
                try: await self.send(order)
                except Exception as exc:
                    self.j.update('alpha_groups',identity,{'state':'recovering','reason':str(exc)[:300]})
                    break
            return identity

    def make_order(self, group, leg, role, body, *, deadline=None, effect, expected, maximum):
        count=self.j.one('SELECT COUNT(*) n FROM alpha_orders WHERE group_id=? AND symbol=? AND role=?',[group['id'],leg['symbol'],role])['n']
        identity=digest([group['id'],leg['symbol'],role,count])
        body={**body,'client_order_id':'ctba-a-'+identity[:32]}
        intent=dict(owner_type='alpha',owner_id=group['id'],owner_record=identity,business_key='alpha:'+identity,
            body=body,effect=effect,resources=[leg['symbol']],expected_qty=expected,max_qty=maximum,
            notional=float(body['qty'])*float(leg['price']),deadline=deadline,
            policy={'version':VERSION,'event_id':group['event_id'],'group_id':group['id'],'role':role})
        intent['expected_qty']=str(intent['expected_qty']);intent['max_qty']=str(intent['max_qty'])
        with self.j.db.lock:
            self.j.db.conn.execute('BEGIN IMMEDIATE')
            try:
                self.j.insert('alpha_orders',{'id':identity,'group_id':group['id'],'owner':VERSION,'account_id':self.co.account_id,
                    'symbol':leg['symbol'],'role':role,'client_order_id':body['client_order_id'],'request_json':body,
                    'created_at':time.time(),'deadline':deadline,'status':'created','intent_effect':effect,'intent_json':intent})
                self.co.prepare(**intent)
                self.j.db.conn.execute('COMMIT')
            except BaseException:
                self.j.db.conn.execute('ROLLBACK');raise
        return self.j.one('SELECT * FROM alpha_orders WHERE id=?',[identity])

    async def send(self, order):
        intent=self.co.one('SELECT * FROM execution_intents WHERE owner_type=? AND owner_record=? AND account_id=?',
            ['alpha',order['id'],self.co.account_id])
        try:
            result=await self.co.dispatch(intent['id'])
            self.j.update('alpha_orders',order['id'],{'status':result['status'],'broker_json':result,'error':None})
            return result
        except Exception as exc:
            current=self.co.one('SELECT * FROM execution_intents WHERE id=?',[intent['id']])
            if current['attempted_at']:
                self.j.update('alpha_orders',order['id'],{'status':current['status'],'error':str(exc)[:400]})
            else:
                self.co.update(intent['id'],{'status':'rejected','error':str(exc)[:400]})
                self.j.update('alpha_orders',order['id'],{'status':'rejected','error':str(exc)[:400]})
            raise

    async def cancel(self, order):
        intent=self.co.one('SELECT * FROM execution_intents WHERE owner_type=? AND owner_record=?',['alpha',order['id']])
        if not intent: raise ValueError('Owned order has no canonical intent')
        if not intent['attempted_at']:
            self.co.update(intent['id'],{'status':'canceled'})
            self.j.update('alpha_orders',order['id'],{'status':'canceled'})
            return
        result=await self.co.cancel(intent['id'])
        canonical=self.co.one('SELECT * FROM execution_intents WHERE id=?',[intent['id']])
        self.j.update('alpha_orders',order['id'],{'status':result['status'],'broker_json':canonical.get('broker_json') or order.get('broker_json')})

    async def refresh_orders(self):
        for order in self.j.query("SELECT * FROM alpha_orders ORDER BY created_at"):
            intent=self.co.one('SELECT * FROM execution_intents WHERE owner_type=? AND owner_record=?',['alpha',order['id']])
            if not intent and order.get('attempted_at') is None and order.get('intent_json'):
                self.co.prepare(**order['intent_json'])
                intent=self.co.one('SELECT * FROM execution_intents WHERE owner_type=? AND owner_record=?',['alpha',order['id']])
            if not intent:
                self.j.update('alpha_orders',order['id'],{'error':'Missing canonical execution intent requires reconciliation'});continue
            if not intent['attempted_at']:
                if order['role']=='exit' and order['status']=='created':
                    group=self.j.one('SELECT * FROM alpha_groups WHERE id=?',[order['group_id']])
                    if group['state'] in ('closing','overdue','unwinding') and (await self.client.clock()).get('is_open'):
                        try: await self.send(order)
                        except Exception as exc: self.j.update('alpha_orders',order['id'],{'error':str(exc)[:300]})
                continue
            # Repair a mirror even if the canonical state became terminal during a crash/cancel race.
            broker=intent.get('broker_json')
            if order['status']!=intent['status'] or order.get('error')!=intent.get('error') or (broker and order.get('broker_json')!=broker):
                values={'status':intent['status'],'error':intent.get('error')}
                if broker is not None: values['broker_json']=broker
                self.j.update('alpha_orders',order['id'],values)
            ledger=sum(Decimal(f['qty']) for f in self.j.query('SELECT qty FROM alpha_fills WHERE order_id=?',[order['id']]))
            known=Decimal(str((broker or {}).get('filled_qty') or 0))
            if intent['status'] in FINAL and ledger==known: continue
            try:
                result=await self.co.recover(intent['id'])
                self.j.update('alpha_orders',order['id'],{'status':result['status'],'broker_json':result,'error':None})
            except AlpacaError as exc:
                self.j.update('alpha_orders',order['id'],{'error':str(exc)[:300]})
            current=self.co.one('SELECT * FROM execution_intents WHERE id=?',[intent['id']])
            deadline=current.get('cancel_after') or order.get('deadline')
            if deadline and deadline<=time.time() and current['status'] not in FINAL:
                try: await self.cancel(order)
                except Exception as exc: self.j.update('alpha_orders',order['id'],{'error':'Cancellation unresolved: '+str(exc)[:250]})

    async def cancel_entries(self):
        # A filled group owns its exposure. Canceling it invokes deterministic unwind.
        async with self.lock:
            for group in self.groups():
                if group['state'] in ('planned','entering','recovering'):
                    self.j.update('alpha_groups',group['id'],{'state':'unwinding','reason':'Owned entry cancellation requested'})
                    for order in self.orders(group):
                        if order['role']=='entry' and order['status'] not in FINAL:
                            await self.cancel(order)

    async def close_owned(self, reason='Operator close'):
        async with self.lock:
            for group in self.groups():
                self.j.update('alpha_groups',group['id'],{'state':'closing','reason':reason})

    async def tick(self):
        async with self.lock:
            await self.co.refresh()
            await self.refresh_orders()
            try:
                await self.svc.accounting.activities()
                self.j.heartbeat('broker_activities',next_run=time.time()+5)
            except Exception as exc:
                self.j.heartbeat('broker_activities',error=str(exc)[:300],next_run=time.time()+5)
            await self.co.refresh()
            clock=await self.client.clock();now=time.time()
            for group in self.groups():
                try: await self.group_tick(group,clock,now)
                except Exception as exc:
                    self.j.update('alpha_groups',group['id'],{'reason':str(exc)[:400]})
                    self.j.heartbeat('execution:'+group['id'],error=str(exc)[:250],next_run=now+5)

    async def group_tick(self, group, clock, now):
        orders=self.orders(group);positions=self.positions(group)
        entry_orders=[o for o in orders if o['role']=='entry']
        active=[o for o in orders if o['status'] not in FINAL]
        filled_any=any(Decimal(str((o.get('broker_json') or {}).get('filled_qty') or 0))>0 for o in entry_orders)
        reconciled=True
        for order in orders:
            ledger=sum(Decimal(f['qty']) for f in self.j.query('SELECT qty FROM alpha_fills WHERE order_id=?',[order['id']]))
            broker=Decimal(str((order.get('broker_json') or {}).get('filled_qty') or 0))
            if ledger != broker: reconciled=False
        if not reconciled:
            # Late fills still belong to the group. Do not release, replace, or guess an exit quantity.
            self.j.update('alpha_groups',group['id'],{'reason':'Fill activities and broker cumulative quantities are reconciling'})
            return
        desired={leg['symbol']:Decimal(str(leg['qty']))*(1 if leg['side']=='buy' else -1) for leg in group['target_json']['legs']}
        actual={p['symbol']:Decimal(p['qty']) for p in positions}
        complete=all(actual.get(s,Decimal(0))==q for s,q in desired.items())
        if group['state'] in ('planned','entering','recovering'):
            if complete and not active:
                self.j.update('alpha_groups',group['id'],{'state':'open','reason':None});group['state']='open'
            elif now>=group['entry_deadline'] or (group.get('recovery_deadline') and now>=group['recovery_deadline']):
                self.j.update('alpha_groups',group['id'],{'state':'unwinding','reason':'Incomplete pair reached its frozen recovery/entry deadline'})
                group['state']='unwinding'
            elif any(o['status']=='rejected' for o in entry_orders):
                self.j.update('alpha_groups',group['id'],{'state':'unwinding','reason':'An entry leg was rejected'})
                group['state']='unwinding'
            elif filled_any:
                self.j.update('alpha_groups',group['id'],{'state':'recovering','reason':'Waiting for the bounded hedge group to complete'})
        if group.get('exit_deadline') and now>=group['exit_deadline']:
            self.j.update('alpha_groups',group['id'],{'state':'closing','reason':'Ten-session time exit'})
            group['state']='closing'
        if group['state'] not in ('unwinding','closing','overdue'): return
        canceled_any=False
        for order in active:
            if order['role']=='entry':
                await self.cancel(order);canceled_any=True
        if canceled_any:
            # Cancellation can discover a fill after this tick's position/activity snapshot.
            # A fresh tick must repeat the complete fill/position barrier before any exit or terminal state.
            self.j.update('alpha_groups',group['id'],{'reason':'Entry cancellation requires a fresh fill and inventory reconciliation'})
            return
        # A cancel response is not final. Never size closes until all entry quantities settle.
        if any(o['role']=='entry' and o['status'] not in FINAL for o in self.orders(group)): return
        for p in positions:
            qty=Decimal(p['qty'])
            if not qty: continue
            broker=next((x for x in self.co.snapshot['positions'] if resource(x['symbol'])==resource(p['symbol'])),None)
            if signed_position(broker)!=qty:
                self.co.state('account_quarantine',{'reason':'External position change','group':group['id'],'symbol':p['symbol']},write=True)
                self.j.update('alpha_groups',group['id'],{'state':'overdue','reason':'Owned inventory differs from broker inventory; reduction is quarantined'})
                continue
            if not clock.get('is_open'):
                self.j.update('alpha_groups',group['id'],{'state':'overdue','reason':'Required exit awaits an open US session'});continue
            exits=[o for o in self.orders(group) if o['symbol']==p['symbol'] and o['role']=='exit']
            if any(o['status'] not in FINAL for o in exits): continue
            if exits and now-exits[-1]['created_at']<30: continue
            leg=next(l for l in group['target_json']['legs'] if l['symbol']==p['symbol'])
            body={'symbol':p['symbol'],'qty':format(abs(qty),'f'),'side':'sell' if qty>0 else 'buy','type':'market',
                  'time_in_force':'day','extended_hours':False}
            order=self.make_order(group,leg,'exit',body,effect='reduce',expected=qty,maximum=abs(qty))
            await self.send(order)
        orders=self.orders(group)
        held=any(Decimal(p['qty']) for p in self.positions(group))
        broker_held=any(resource(p['symbol']) in {resource(s) for s in desired} for p in self.co.snapshot['positions'])
        if not held and not broker_held and all(o['status'] in FINAL for o in orders):
            self.j.update('alpha_groups',group['id'],{'state':'closed' if filled_any else 'canceled','reason':'Owned fills and flat broker inventory reconciled'})
