"""One cooperating local writer per pinned paper account, with explicit durable intents."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import time
from decimal import Decimal
from pathlib import Path

import aiohttp

from .alpaca import AlpacaError, asset_class

FINAL = {'filled', 'canceled', 'expired', 'rejected', 'replaced'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS execution_intents (
 id TEXT PRIMARY KEY, business_key TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL,
 owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, owner_record TEXT NOT NULL,
 client_order_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL, effect TEXT NOT NULL,
 request_json TEXT NOT NULL, resources_json TEXT NOT NULL, expected_qty TEXT NOT NULL,
 max_qty TEXT NOT NULL, notional REAL, deadline REAL, cancel_after REAL, policy_json TEXT,
 created_at REAL NOT NULL, attempted_at REAL, status TEXT NOT NULL, broker_json TEXT,
 cancel_requested_at REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS execution_leases (
 account_id TEXT NOT NULL, resource TEXT NOT NULL, owner_type TEXT NOT NULL,
 owner_id TEXT NOT NULL, flat_since REAL, quarantine TEXT,
 PRIMARY KEY(account_id,resource)
);
CREATE TABLE IF NOT EXISTS execution_allocations (
 account_id TEXT NOT NULL, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL,
 amount REAL NOT NULL, created_at REAL, PRIMARY KEY(account_id,owner_type,owner_id)
);
CREATE TABLE IF NOT EXISTS execution_lease_members (
 account_id TEXT, resource TEXT, owner_type TEXT, owner_id TEXT, position_symbol TEXT,
 PRIMARY KEY(account_id,resource,owner_type,owner_id,position_symbol)
);
CREATE TABLE IF NOT EXISTS execution_state (id TEXT PRIMARY KEY,value_json TEXT,updated_at REAL);
'''


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def resource(symbol):
    return symbol.replace('/', '').upper()


def signed_position(position):
    if not position:
        return Decimal(0)
    qty = Decimal(str(position['qty']))
    return -abs(qty) if position.get('side') == 'short' else qty


class ReadClient:
    """Existing adapters can read through this client, but writes need an intent ID."""
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def __getattr__(self, key):
        if key in ('submit', 'cancel'):
            raise ValueError('Broker writes require a persisted coordinator intent')
        return getattr(self.coordinator._transport, key)

    async def request(self, method, *args, **kwargs):
        if method != 'GET':
            raise ValueError('Broker writes require a persisted coordinator intent')
        return await self.coordinator._transport.request(method, *args, **kwargs)

    async def account(self): return (await self.coordinator.refresh())['account']
    async def positions(self): return (await self.coordinator.refresh())['positions']
    async def orders(self, **params):
        if params: return await self.coordinator._transport.orders(**params)
        return (await self.coordinator.refresh())['orders']
    async def clock(self): return await self.coordinator.clock()


class BrokerCoordinator:
    def __init__(self, settings, db, transport, *, lock_dir=None):
        self.s, self.db, self._transport = settings, db, transport
        self.account_id = settings.alpaca_account_id
        self.lock = asyncio.Lock()
        self._snapshot_lock=asyncio.Lock()
        self._clock_lock=asyncio.Lock()
        self._clock=None
        self._file = None
        self.lock_dir = Path(lock_dir) if lock_dir else Path.home() / '.local/state/openberg/paper-writers'
        self.snapshot = {'account': {}, 'positions': [], 'orders': [], 'at': 0}
        self.read_client = ReadClient(self)
        with db.lock:
            db.conn.executescript(SCHEMA)
            cols={r[1] for r in db.conn.execute('PRAGMA table_info(execution_intents)')}
            if 'inventory_seen_qty' not in cols: db.conn.execute("ALTER TABLE execution_intents ADD COLUMN inventory_seen_qty TEXT NOT NULL DEFAULT '0'")
            if 'inventory_seen_at' not in cols: db.conn.execute('ALTER TABLE execution_intents ADD COLUMN inventory_seen_at REAL')

    def acquire(self):
        if self._file is not None:
            return
        if not self.account_id:
            raise ValueError('A verified paper account pin is required')
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.chmod(0o700)
        path = self.lock_dir / (hashlib.sha256(self.account_id.encode()).hexdigest() + '.lock')
        file = path.open('a+')
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.close()
            raise ValueError('Another local process owns this paper account writer') from None
        file.seek(0); file.truncate(); file.write(str(os.getpid())); file.flush()
        self._file = file

    def close(self):
        if self._file:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    @staticmethod
    def decode(row):
        if row is None: return None
        return {k: json.loads(v) if k.endswith('_json') and v is not None else v for k, v in dict(row).items()}

    def query(self, sql, params=()):
        with self.db.lock:
            return [self.decode(r) for r in self.db.conn.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def update(self, identity, values):
        encoded = {k: canonical(v) if k.endswith('_json') and v is not None else v for k, v in values.items()}
        with self.db.lock:
            self.db.conn.execute('UPDATE execution_intents SET ' + ','.join(k + '=?' for k in encoded) + ' WHERE id=?', [*encoded.values(), identity])

    def state(self, key, value=None, *, write=False):
        if write:
            with self.db.lock:
                self.db.conn.execute('INSERT INTO execution_state VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at',
                                     [key, canonical(value), time.time()])
        row = self.one('SELECT * FROM execution_state WHERE id=?', [key])
        return row['value_json'] if row else None

    async def refresh(self, *, force=False):
        async with self._snapshot_lock:
            if not force and time.time()-self.snapshot['at']<1:
                if self.s.alpaca_account_id!=self.account_id: raise ValueError('Paper account pin changed')
                return self.snapshot
            positions, orders = await asyncio.gather(self._transport.positions(), self._transport.orders())
            # Read capacity after the open-order snapshot, so visible commitments have been acknowledged.
            account = await self._transport.account()
            if account.get('id') != self.account_id or self.s.alpaca_account_id != self.account_id:
                raise ValueError('Paper account pin mismatch; no broker write is permitted')
            self.snapshot = {'account': account, 'positions': positions, 'orders': orders, 'at': time.time()}
            self.observe_inventory()
            return self.snapshot

    async def clock(self):
        async with self._clock_lock:
            if not self._clock or time.time()-self._clock['at']>1:
                self._clock={'value':await self._transport.clock(),'at':time.time()}
            return self._clock['value']

    def conflict(self, resources, owner_type, owner_id):
        for symbol in resources:
            lease = self.one('SELECT * FROM execution_leases WHERE account_id=? AND resource=?', [self.account_id, resource(symbol)])
            if lease and (lease['quarantine'] or lease['owner_type'] != owner_type or lease['owner_id'] != str(owner_id)):
                return lease
        return None

    def allocate(self, owner_type, owner_id, amount):
        if amount < 0:
            raise ValueError('Capital allocation cannot be negative')
        with self.db.lock:
            self.db.conn.execute('INSERT INTO execution_allocations VALUES(?,?,?,?,?) ON CONFLICT(account_id,owner_type,owner_id) DO UPDATE SET amount=excluded.amount',
                                 [self.account_id, owner_type, str(owner_id), amount, time.time()])

    def available(self, owner_type, *, power='buying_power', exclude_id=None):
        account = self.snapshot['account']
        base = max(0, float(account.get(power) or 0))
        other = sum(r['amount'] for r in self.query('SELECT * FROM execution_allocations WHERE account_id=? AND owner_type<>?', [self.account_id, owner_type]))
        visible = {o.get('client_order_id') for o in self.snapshot['orders']}
        unknown = sum(self.pending_notional(i) for i in self.query("SELECT * FROM execution_intents WHERE account_id=? AND effect IN ('increase','repair') AND status NOT IN ('filled','canceled','expired','rejected','replaced')", [self.account_id])
                      if i['client_order_id'] not in visible and i['id'] != exclude_id)
        return max(0, base - other - unknown)

    @staticmethod
    def pending_notional(intent):
        requested=Decimal(intent['request_json']['qty'])
        filled=Decimal(str((intent.get('broker_json') or {}).get('filled_qty') or 0))
        visible=Decimal(intent.get('inventory_seen_qty') or '0')
        outstanding=(filled if intent['status'] in FINAL else requested)-visible
        return max(0,float(outstanding/requested)*float(intent.get('notional') or 0))

    def commitments(self, *, exclude_id=None):
        return [i for i in self.query("SELECT * FROM execution_intents WHERE account_id=? AND effect IN ('increase','repair')",[self.account_id])
                if i['id']!=exclude_id and self.pending_notional(i)>0]

    @staticmethod
    def validate_snapshot(order, intent):
        if order.get('client_order_id')!=intent['client_order_id']:
            raise ValueError('Broker order identity differs from the canonical intent')
        quantity=Decimal(str(order.get('filled_qty') or 0));requested=Decimal(intent['request_json']['qty'])
        if not quantity.is_finite() or quantity<0 or quantity>requested or (order.get('status')=='filled' and quantity!=requested):
            raise ValueError('Broker cumulative fill quantity is missing or inconsistent')

    def observe_inventory(self):
        """Release filled exposure reservations only after the corresponding inventory is proved visible."""
        positions={resource(p['symbol']):signed_position(p) for p in self.snapshot['positions']}
        intents=self.query("SELECT * FROM execution_intents WHERE account_id=? AND effect IN ('increase','repair')",[self.account_id])
        witnesses={}
        for i in intents:
            filled=Decimal(str((i.get('broker_json') or {}).get('filled_qty') or 0))
            if filled>Decimal(i.get('inventory_seen_qty') or '0'):
                key=(resource(i['symbol']),Decimal(i['expected_qty'])+(1 if i['request_json']['side']=='buy' else -1)*filled)
                witnesses[key]=witnesses.get(key,0)+1
        for intent in intents:
            filled=Decimal(str((intent.get('broker_json') or {}).get('filled_qty') or 0))
            if filled<=Decimal(intent.get('inventory_seen_qty') or '0'): continue
            sign=1 if intent['request_json']['side']=='buy' else -1
            expected=Decimal(intent['expected_qty'])+sign*filled
            if witnesses.get((resource(intent['symbol']),expected),0)>1:
                self.state('account_quarantine',{'reason':'Multiple unacknowledged fills share an ambiguous inventory witness','symbol':intent['symbol']},write=True)
                continue
            seen=positions.get(resource(intent['symbol']),Decimal(0))==expected
            if intent['owner_type']=='manual':
                row=self.db.one('SELECT inventory_seen_ts FROM manual_orders WHERE id=? AND account_id=?',[intent['owner_record'],self.account_id])
                seen=seen or bool(row and row['inventory_seen_ts'] and intent['status'] in FINAL)
            elif intent['owner_type']=='legacy':
                row=self.db.one('SELECT * FROM trades WHERE id=? AND account_id=?',[intent['owner_record'],self.account_id])
                if row and row['status']=='closed' and not positions.get(resource(intent['symbol'])): seen=True
                if row and row['status']=='open' and asset_class(intent['symbol'])=='crypto':
                    expected_credit=Decimal(str(row.get('qty') or 0))*(1 if row['side']=='long' else -1)
                    seen=seen or positions.get(resource(intent['symbol']),Decimal(0))==expected_credit
            elif intent['owner_type']=='alpha':
                with self.db.lock:
                    row=self.db.conn.execute('SELECT state FROM alpha_groups WHERE id=?',[intent['owner_id']]).fetchone()
                seen=seen or bool(row and row['state'] in ('closed','canceled') and not positions.get(resource(intent['symbol'])))
            if seen: self.update(intent['id'],{'inventory_seen_qty':str(filled),'inventory_seen_at':time.time()})

    def prepare(self, *, owner_type, owner_id, owner_record, business_key, body, effect,
                resources, expected_qty, max_qty, notional=0, deadline=None, policy=None):
        if owner_type not in ('manual', 'legacy', 'alpha') or effect not in ('increase', 'reduce', 'protect', 'repair'):
            raise ValueError('Explicit owner and order effect are required')
        if not body.get('client_order_id', '').startswith('ctba-') or len(body['client_order_id']) > 48:
            raise ValueError('A stable Openberg client order ID is required')
        qty = Decimal(str(body.get('qty', 0)))
        if not qty.is_finite() or qty <= 0 or body.get('side') not in ('buy', 'sell'):
            raise ValueError('Order quantity and side are invalid')
        if owner_type == 'alpha' and (asset_class(body['symbol']) != 'us_equity' or body.get('time_in_force') != 'day' or body.get('extended_hours')):
            raise ValueError('The economic-link strategy permits regular-session equity DAY orders only')
        payload = canonical(body)
        identity = hashlib.sha256((self.account_id + ':' + business_key).encode()).hexdigest()
        resources = sorted({resource(s) for s in resources})
        if resource(body['symbol']) not in resources:
            raise ValueError('The primary position symbol must have an ownership lease')
        values = {'id': identity, 'business_key': business_key, 'account_id': self.account_id,
                  'owner_type': owner_type, 'owner_id': str(owner_id), 'owner_record': str(owner_record),
                  'client_order_id': body['client_order_id'], 'symbol': body['symbol'], 'effect': effect,
                  'request_json': payload, 'resources_json': canonical(resources), 'expected_qty': str(expected_qty),
                  'max_qty': str(max_qty), 'notional': float(notional), 'deadline': deadline,
                  'policy_json': canonical(policy or {}), 'created_at': time.time(), 'status': 'created'}
        with self.db.lock:
            nested=self.db.conn.in_transaction
            if not nested: self.db.conn.execute('BEGIN IMMEDIATE')
            try:
                existing = self.db.conn.execute('SELECT * FROM execution_intents WHERE business_key=?', [business_key]).fetchone()
                if existing:
                    if (existing['request_json'] != payload or existing['owner_type'] != owner_type or existing['owner_record'] != str(owner_record)
                            or existing['account_id']!=self.account_id or existing['owner_id']!=str(owner_id) or existing['effect']!=effect):
                        raise ValueError('An approved business action cannot change its canonical order')
                else:
                    if effect in ('increase','repair'):
                        predecessors=self.db.conn.execute("SELECT * FROM execution_intents WHERE account_id=? AND symbol=? AND effect IN ('increase','repair')",[self.account_id,body['symbol']]).fetchall()
                        if any(r['status'] not in FINAL or self.pending_notional(self.decode(r))>0 for r in predecessors):
                            raise ValueError('A previous opening action on this symbol is unresolved')
                    for symbol in resources:
                        lease = self.db.conn.execute('SELECT * FROM execution_leases WHERE account_id=? AND resource=?', [self.account_id, symbol]).fetchone()
                        if lease and (lease['quarantine'] or lease['owner_type'] != owner_type or lease['owner_id'] != str(owner_id)):
                            raise ValueError('Another owner reserves ' + symbol)
                    self.db.conn.execute(f"INSERT INTO execution_intents ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))
                    for symbol in resources:
                        self.db.conn.execute('INSERT INTO execution_leases VALUES(?,?,?,?,NULL,NULL) ON CONFLICT(account_id,resource) DO UPDATE SET flat_since=NULL',
                                             [self.account_id, symbol, owner_type, str(owner_id)])
                        self.db.conn.execute('INSERT OR IGNORE INTO execution_lease_members VALUES(?,?,?,?,?)',
                                             [self.account_id, symbol, owner_type, str(owner_id), resource(body['symbol'])])
                if not nested: self.db.conn.execute('COMMIT')
            except BaseException:
                if not nested: self.db.conn.execute('ROLLBACK')
                raise
        return identity

    def reserve_group(self, owner_id, resources):
        """Acquire the complete stock/hedge set atomically before any leg is dispatched."""
        with self.db.lock:
            self.db.conn.execute('BEGIN IMMEDIATE')
            try:
                for symbol in sorted({resource(s) for s in resources}):
                    row = self.db.conn.execute('SELECT * FROM execution_leases WHERE account_id=? AND resource=?', [self.account_id, symbol]).fetchone()
                    if row and (row['quarantine'] or row['owner_type'] != 'alpha' or row['owner_id'] != str(owner_id)):
                        raise ValueError('The complete hedge group cannot reserve ' + symbol)
                    self.db.conn.execute('INSERT INTO execution_leases VALUES(?,?,?,?,NULL,NULL) ON CONFLICT(account_id,resource) DO UPDATE SET flat_since=NULL',
                                         [self.account_id, symbol, 'alpha', str(owner_id)])
                self.db.conn.execute('COMMIT')
            except BaseException:
                self.db.conn.execute('ROLLBACK')
                raise

    async def migrate(self):
        """Import exact originating records without submitting historical orders."""
        await self.refresh()
        for row in self.db.query("SELECT * FROM manual_orders WHERE account_id=? AND attempted_ts IS NOT NULL AND "
                "(status NOT IN ('filled','canceled','expired','rejected','replaced') OR inventory_seen_ts IS NULL OR "
                "REPLACE(symbol,'/','') IN (SELECT position_symbol FROM manual_ownership WHERE account_id=?))", [self.account_id,self.account_id]):
            body = row['request_json']
            review = row['review_json']
            identity = self.prepare(owner_type='manual', owner_id='manual', owner_record=row['id'],
                business_key='manual:' + row['id'], body=body,
                effect='reduce' if review['reducing'] else 'increase', resources=review['resources'],
                expected_qty=review['position_qty'], max_qty=body['qty'], notional=review['estimated_notional'])
            if not self.one('SELECT attempted_at FROM execution_intents WHERE id=?', [identity])['attempted_at']:
                self.update(identity, {'attempted_at': row['attempted_ts'], 'status': row['status'], 'broker_json': row.get('broker_json')})
        for trade in self.db.query("SELECT * FROM trades WHERE venue='alpaca' AND account_id=? AND status IN ('opening','open','closing')", [self.account_id]):
            links = [trade['entry_link_id'], *(trade.get('exit_links_json') or []), trade.get('exit_link_id'), trade['entry_link_id'] + '-stop']
            for link in dict.fromkeys(l for l in links if l):
                try:
                    order = await self._transport.order(link)
                except AlpacaError as exc:
                    if exc.status != 404: raise
                    if link != trade['entry_link_id'] or trade['status'] != 'opening':
                        continue
                    # An old ambiguous entry may never have reached Alpaca. It is never re-POSTed.
                    order = {'symbol': trade['symbol'], 'qty': str(trade['requested_qty']),
                             'side': 'buy' if trade['side']=='long' else 'sell', 'type':'limit',
                             'limit_price':str(trade['cap_price']), 'time_in_force':'gtc' if asset_class(trade['symbol'])=='crypto' else 'day',
                             'client_order_id':link, 'status':'unknown'}
                body = {k:order[k] for k in ('symbol','qty','side','type','limit_price','stop_price','time_in_force','client_order_id','position_intent') if order.get(k) is not None}
                effect = 'increase' if link == trade['entry_link_id'] else 'protect' if link.endswith('-stop') else 'reduce'
                resources = [trade['symbol']]
                if asset_class(trade['symbol']) == 'option': resources.append(trade['symbol'][:-15])
                identity = self.prepare(owner_type='legacy', owner_id=trade['id'], owner_record=trade['id'],
                    business_key='legacy:' + link, body=body, effect=effect, resources=resources,
                    expected_qty=0, max_qty=body['qty'], notional=float(trade.get('notional') or 0))
                if not self.one('SELECT attempted_at FROM execution_intents WHERE id=?',[identity])['attempted_at']:
                    self.update(identity, {'attempted_at':trade['opened_ts'], 'status':order['status'], 'broker_json':order})
        # Foreign inventory is recorded as foreign. It never becomes strategy inventory.
        for item in [*self.snapshot['positions'], *self.snapshot['orders']]:
            symbol = resource(item['symbol'])
            if not self.one('SELECT * FROM execution_leases WHERE account_id=? AND resource=?',[self.account_id,symbol]):
                with self.db.lock:
                    self.db.conn.execute('INSERT OR IGNORE INTO execution_leases VALUES(?,?,?,?,NULL,?)',
                        [self.account_id,symbol,'external','external','Unowned broker inventory/order'])
        self.state('migration', {'at':time.time(),'account_id':self.account_id}, write=True)

    async def legacy_submit(self, trade, body, effect):
        if not trade:
            raise ValueError('An originating trade record is required')
        position = next((p for p in (await self.refresh())['positions'] if resource(p['symbol'])==resource(body['symbol'])),None)
        expected = signed_position(position)
        resources = [body['symbol']]
        if asset_class(body['symbol']) == 'option': resources.append(body['symbol'][:-15])
        maximum = Decimal(str(trade.get('qty') or trade.get('requested_qty') or 0))
        if effect in ('reduce','protect') and abs(expected) > maximum:
            raise ValueError('Broker quantity exceeds the originating trade inventory')
        identity = self.prepare(owner_type='legacy', owner_id=trade['id'], owner_record=trade['id'],
            business_key='legacy:' + body['client_order_id'], body=body, effect=effect, resources=resources,
            expected_qty=expected, max_qty=maximum,
            notional=float(body['qty']) * float(body.get('limit_price') or trade.get('ref_price') or 0) * float(trade.get('multiplier') or 1))
        try:
            return await self.dispatch(identity)
        except Exception:
            row=self.one('SELECT * FROM execution_intents WHERE id=?',[identity])
            if not row['attempted_at']: self.update(identity,{'status':'rejected','error':'Originating legacy action failed its dispatch preflight'})
            raise

    async def legacy_cancel(self, trade, order):
        intent = self.one('SELECT * FROM execution_intents WHERE account_id=? AND owner_type=? AND owner_record=? AND client_order_id=?',
            [self.account_id,'legacy',str(trade['id']),order['client_order_id']])
        if not intent:
            raise ValueError('The originating legacy order has no execution record')
        return await self.cancel(intent['id'])

    def authority(self, intent, *, dispatch=False):
        """Exact persisted originating records authorize dispatch; symbols/prefixes do not."""
        body = intent['request_json']
        if intent['owner_type'] == 'manual':
            row = self.db.one('SELECT * FROM manual_orders WHERE id=? AND account_id=?', [intent['owner_record'], self.account_id])
            if not row or row['request_json'] != body:
                raise ValueError('Manual intent is not linked to the immutable reviewed request')
            if not intent['attempted_at'] and row['review_json']['expires_at'] < time.time():
                raise ValueError('Manual order review expired')
        elif intent['owner_type'] == 'legacy':
            row = self.db.one('SELECT * FROM trades WHERE id=? AND venue=? AND account_id=?', [intent['owner_record'], 'alpaca', self.account_id])
            links = [row.get('entry_link_id'), row.get('exit_link_id'), *(row.get('exit_links_json') or []), str(row.get('entry_link_id')) + '-stop'] if row else []
            if not row or body['client_order_id'] not in links or resource(row['symbol']) != resource(body['symbol']):
                raise ValueError('Legacy intent lacks an exact trade/action link')
            if str(row['id'])!=intent['owner_id']: raise ValueError('Legacy owner differs from its originating trade')
            if dispatch and not intent['attempted_at'] and intent['effect'] == 'increase' and not self.s.trading_enabled and row.get('mode') != 'manual_option':
                raise ValueError('Legacy automatic entries are disabled')
        else:
            with self.db.lock:
                order = self.db.conn.execute('SELECT * FROM alpha_orders WHERE id=? AND account_id=?', [intent['owner_record'], self.account_id]).fetchone()
                group = self.db.conn.execute('SELECT * FROM alpha_groups WHERE id=?', [intent['owner_id']]).fetchone()
                version = self.db.conn.execute('SELECT * FROM alpha_versions WHERE id=?', [group['version']]).fetchone() if group else None
            if (not order or not group or not version or json.loads(order['request_json']) != body
                    or order['group_id']!=group['id'] or group['account_id']!=self.account_id
                    or order['owner']!=group['version'] or order['intent_effect']!=intent['effect']):
                raise ValueError('Alpha intent is not linked to a persisted group leg')
            targets = json.loads(group['target_json'])
            if body['symbol'] not in {leg['symbol'] for leg in targets['legs']}:
                raise ValueError('Alpha symbol is outside the approved group')
            if dispatch and not intent['attempted_at'] and intent['effect'] in ('reduce','protect'):
                with self.db.lock:
                    holding=self.db.conn.execute('SELECT qty FROM alpha_positions WHERE group_id=? AND symbol=? AND account_id=?',
                        [group['id'],body['symbol'],self.account_id]).fetchone()
                owned=Decimal(holding['qty']) if holding else Decimal(0)
                if not owned or (owned>0)!=(body['side']=='sell') or Decimal(body['qty'])>abs(owned) or Decimal(intent['max_qty'])>abs(owned):
                    raise ValueError('Alpha reduction exceeds its independently booked owned quantity')
            if dispatch and not intent['attempted_at'] and intent['effect'] in ('increase','repair'):
                leg = next(leg for leg in targets['legs'] if leg['symbol'] == body['symbol'])
                if body['side'] != leg['side'] or Decimal(body['qty']) > Decimal(str(leg['qty'])):
                    raise ValueError('Alpha order exceeds its approved leg')
                other=self.query("SELECT * FROM execution_intents WHERE owner_type='alpha' AND owner_id=? AND symbol=? AND effect IN ('increase','repair') AND id<>?",[group['id'],body['symbol'],intent['id']])
                committed=Decimal(0)
                for previous in other:
                    committed+=Decimal(str((previous.get('broker_json') or {}).get('filled_qty') or 0)) if previous['status'] in FINAL else Decimal(previous['request_json']['qty'])
                if committed+Decimal(body['qty'])>Decimal(str(leg['qty'])):
                    raise ValueError('Cumulative alpha entry commitments exceed the approved target')
                if group['entry_deadline'] <= time.time() or group['state'] not in ('entering','recovering'):
                    raise ValueError('The approved entry window has ended')
            if dispatch and not intent['attempted_at'] and intent['effect'] == 'increase' and not version['enabled']:
                raise ValueError('The economic-link entry switch is paused')

    async def dispatch(self, identity):
        async with self.lock:
            if self._file is None:
                raise ValueError('The paper writer process lock is not held')
            intent = self.one('SELECT * FROM execution_intents WHERE id=? AND account_id=?', [identity, self.account_id])
            if not intent:
                raise ValueError('Execution intent was not found')
            await self.refresh(force=True)
            if intent['attempted_at']:
                return await self.recover(identity)
            self.authority(intent, dispatch=True)
            now = time.time()
            if intent['deadline'] and now >= intent['deadline']:
                self.update(identity, {'status': 'expired', 'error': 'Dispatch deadline passed before submission'})
                raise ValueError('Order dispatch deadline passed')
            if self.conflict(intent['resources_json'], intent['owner_type'], intent['owner_id']):
                raise ValueError('The execution resource lease is unavailable')
            for symbol in intent['resources_json']:
                if not self.one('SELECT resource FROM execution_leases WHERE account_id=? AND resource=?',[self.account_id,symbol]):
                    raise ValueError('The canonical order lost its required resource lease')
            body = intent['request_json']
            position = next((p for p in self.snapshot['positions'] if resource(p['symbol']) == resource(body['symbol'])), None)
            actual = signed_position(position)
            expected = Decimal(intent['expected_qty'])
            qty = Decimal(body['qty'])
            if actual != expected:
                raise ValueError('Broker inventory changed; the recorded action requires reconciliation')
            if intent['effect'] in ('reduce', 'protect'):
                if not position or (actual > 0) != (body['side'] == 'sell') or qty > abs(actual) or qty > Decimal(intent['max_qty']):
                    raise ValueError('Reduction would exceed proven owned inventory or reverse its side')
                available = abs(Decimal(str(position.get('qty_available', position['qty']))))
                if qty > available:
                    raise ValueError('Owned inventory is committed to another order; cancellation must finish first')
            elif intent['effect'] in ('increase', 'repair'):
                if self.state('account_quarantine'):
                    raise ValueError('Unexplained external activity blocks new account exposure')
                kind = asset_class(body['symbol'])
                power = 'non_marginable_buying_power' if kind == 'crypto' and 'non_marginable_buying_power' in self.snapshot['account'] else 'cash' if kind == 'crypto' else 'options_buying_power' if kind == 'option' else 'buying_power'
                # This intent's reservation is already included in the unknown commitments.
                if intent['notional'] > self.available(intent['owner_type'], power=power, exclude_id=identity):
                    raise ValueError('Shared paper buying power is reserved by another owner')
                account = self.snapshot['account']
                gross = sum(abs(float(p.get('market_value') or 0)) for p in self.snapshot['positions'])
                commitments=self.commitments(exclude_id=identity)
                committed_gross=sum(self.pending_notional(i) for i in commitments)
                if gross + committed_gross + intent['notional'] > float(account['equity']) * self.s.max_leverage:
                    raise ValueError('The existing whole-account leverage limit binds')
                names={resource(p['symbol']) for p in self.snapshot['positions']}|{resource(i['symbol']) for i in commitments}|{resource(body['symbol'])}
                if len(names) > self.s.max_positions:
                    raise ValueError('The existing whole-account position cap binds')
            if intent['owner_type'] == 'alpha':
                clock = await self._transport.clock()
                if not clock.get('is_open'):
                    raise ValueError('Alpha equity execution requires an open US session')
                if intent['effect'] in ('increase', 'repair'):
                    quote = (await self._transport.snapshots([body['symbol']], 'us_equity')).get(body['symbol'], {}).get('latestQuote') or {}
                    now=time.time()
                    from .alpaca import timestamp
                    bid, ask = float(quote.get('bp') or 0), float(quote.get('ap') or 0)
                    if not 0 < bid <= ask or not -1 <= now - timestamp(quote.get('t')) <= 5 or 10000 * (ask - bid) / ((ask + bid) / 2) > 15:
                        raise ValueError('Alpha quote freshness or displayed-spread gate failed at dispatch')
                    with self.db.lock:
                        target=json.loads(self.db.conn.execute('SELECT target_json FROM alpha_groups WHERE id=?',[intent['owner_id']]).fetchone()[0])
                    reference=next(leg['price'] for leg in target['legs'] if leg['symbol']==body['symbol'])
                    if abs((bid+ask)/2/reference-1)*10000 > self.s.max_entry_slip_bp:
                        raise ValueError('The approved group price moved beyond the existing entry slippage bound')
            elif asset_class(body['symbol'])!='crypto' and intent['effect']!='protect':
                clock=await self.clock()
                from .alpaca import timestamp
                if not clock.get('is_open') or timestamp(clock.get('next_close'))<=time.time():
                    raise ValueError('Regular US session is closed')
            now=time.time()
            if asset_class(body['symbol'])!='crypto' and intent['effect']!='protect':
                from .alpaca import timestamp
                if not clock.get('is_open') or timestamp(clock.get('next_close'))<=now:
                    raise ValueError('Regular US session ended during broker preflight')
            if self.s.alpaca_account_id!=self.account_id:
                raise ValueError('The paper account pin changed during preflight')
            if intent['deadline'] and now>=intent['deadline']:
                raise ValueError('The dispatch deadline passed during broker preflight')
            # Pause, kill and group controls can run during network awaits. Check again before the atomic attempt.
            self.authority(intent,dispatch=True)
            if intent['effect'] in ('increase','repair') and self.state('account_quarantine'):
                raise ValueError('Ownership quarantine was raised during broker preflight')
            if intent['effect'] in ('increase','repair') and self.s.kill_file.exists():
                raise ValueError('The application kill file blocks new exposure')
            with self.db.lock:
                self.db.conn.execute('BEGIN IMMEDIATE')
                try:
                    claimed = self.db.conn.execute("UPDATE execution_intents SET attempted_at=?,status='submitting',cancel_after=? WHERE id=? AND attempted_at IS NULL AND status='created'",
                                                   [now, min(now + 60, intent['deadline']) if intent['owner_type'] == 'alpha' and intent['effect'] != 'reduce' and intent['deadline'] else None, identity]).rowcount
                    if claimed and intent['owner_type'] == 'manual':
                        self.db.conn.execute("UPDATE manual_orders SET attempted_ts=?,status='submitting' WHERE id=? AND account_id=?", [now, intent['owner_record'], self.account_id])
                        for symbol in intent['resources_json']:
                            self.db.conn.execute('INSERT INTO manual_ownership(account_id,resource,position_symbol,flat_since) VALUES(?,?,?,NULL) '
                                                 'ON CONFLICT(account_id,resource,position_symbol) DO UPDATE SET flat_since=NULL',
                                                 [self.account_id, symbol, resource(body['symbol'])])
                    if claimed and intent['owner_type'] == 'alpha':
                        self.db.conn.execute("UPDATE alpha_orders SET attempted_at=?,status='submitting' WHERE id=? AND account_id=?", [now, intent['owner_record'], self.account_id])
                    self.db.conn.execute('COMMIT')
                except BaseException:
                    self.db.conn.execute('ROLLBACK')
                    raise
            if not claimed:
                return await self.recover(identity)
            try:
                order = await self._transport.submit(body)
                self.snapshot['at']=0
                self.validate_snapshot(order,intent)
                self.update(identity, {'status': order['status'], 'broker_json': order, 'error': None})
                return order
            except AlpacaError as exc:
                ambiguous = exc.status >= 500 or exc.status in (408, 429)
                self.update(identity, {'status': 'unknown' if ambiguous else 'rejected', 'error': str(exc)[:400]})
                raise
            except Exception:
                self.update(identity, {'status': 'unknown', 'error': 'Submission outcome is unresolved; the same client ID will be reconciled'})
                raise AlpacaError(503, 'Submission outcome is unresolved; no new order ID will be used') from None

    async def recover(self, identity):
        intent = self.one('SELECT * FROM execution_intents WHERE id=?', [identity])
        if intent['status']=='rejected' and not intent.get('broker_json'):
            raise AlpacaError(422,intent.get('error') or 'The original attempt was definitively rejected')
        try:
            order = await self._transport.order(intent['client_order_id'])
        except AlpacaError as exc:
            if exc.status == 404:
                self.update(identity, {'status': 'unknown', 'error': 'The original order is not yet resolved; no resubmission is permitted'})
            raise
        self.validate_snapshot(order,intent)
        if Decimal(str(order.get('filled_qty') or 0))!=Decimal(str((intent.get('broker_json') or {}).get('filled_qty') or 0)):
            self.snapshot['at']=0
        self.update(identity, {'status': order['status'], 'broker_json': order, 'error': None})
        return order

    async def cancel(self, identity):
        async with self.lock:
            if self._file is None:
                raise ValueError('The paper writer process lock is not held')
            await self.refresh(force=True)
            intent = self.one('SELECT * FROM execution_intents WHERE id=? AND account_id=?', [identity, self.account_id])
            if not intent:
                raise ValueError('Cancellation requires an exact account/intent link')
            self.authority(intent)
            if intent['status']=='rejected' and not intent.get('broker_json'):
                return {'status':'rejected','client_order_id':intent['client_order_id']}
            order = await self.recover(identity)
            if order['status'] in FINAL:
                return order
            self.update(identity, {'cancel_requested_at': time.time(), 'status': 'pending_cancel'})
            await self._transport.cancel(order)
            self.snapshot['at']=0
            return {'status': 'pending_cancel', **{k: order[k] for k in ('id', 'client_order_id') if k in order}}

    async def reconcile(self):
        async with self.lock:
            await self.refresh()
            for item in [*self.snapshot['positions'],*self.snapshot['orders']]:
                symbol=resource(item['symbol'])
                if not self.one('SELECT * FROM execution_leases WHERE account_id=? AND resource=?',[self.account_id,symbol]):
                    with self.db.lock:
                        self.db.conn.execute('INSERT OR IGNORE INTO execution_leases VALUES(?,?,?,?,NULL,?)',
                            [self.account_id,symbol,'external','external','Unexplained broker inventory/order'])
                    self.state('account_quarantine',{'reason':'Unexplained external broker inventory/order','symbol':symbol,'at':time.time()},write=True)
            with self.db.lock:
                self.db.conn.execute("UPDATE execution_intents SET status='expired',error='Unsubmitted review/window expired' WHERE attempted_at IS NULL AND status='created' AND deadline IS NOT NULL AND deadline<=?",[time.time()])
            for intent in self.query("SELECT * FROM execution_intents WHERE account_id=? AND attempted_at IS NOT NULL AND status NOT IN ('filled','canceled','expired','rejected','replaced')", [self.account_id]):
                try:
                    await self.recover(intent['id'])
                except AlpacaError as exc:
                    if exc.status != 404: raise
            # Leases survive unresolved orders, positions, and delayed inventory propagation.
            for lease in self.query('SELECT * FROM execution_leases WHERE account_id=?', [self.account_id]):
                group = self.query('SELECT * FROM execution_leases WHERE account_id=? AND owner_type=? AND owner_id=?', [self.account_id, lease['owner_type'], lease['owner_id']])
                resources = {r['resource'] for r in group}
                if lease['owner_type'] == 'manual':
                    members = self.query('SELECT position_symbol FROM execution_lease_members WHERE account_id=? AND resource=? AND owner_type=? AND owner_id=?',
                                         [self.account_id, lease['resource'], 'manual', lease['owner_id']])
                    resources = {lease['resource']}
                    for member in members:
                        resources.update(r['resource'] for r in self.query('SELECT resource FROM execution_lease_members WHERE account_id=? AND position_symbol=? AND owner_type=? AND owner_id=?',
                                                                            [self.account_id, member['position_symbol'], 'manual', lease['owner_id']]))
                held = any(resource(p['symbol']) in resources for p in self.snapshot['positions'])
                active = any(resources.intersection(i['resources_json']) for i in self.query("SELECT * FROM execution_intents WHERE account_id=? AND owner_type=? AND owner_id=? AND status NOT IN ('filled','canceled','expired','rejected','replaced')", [self.account_id, lease['owner_type'], lease['owner_id']]))
                if lease['owner_type'] == 'alpha':
                    with self.db.lock:
                        obligation = self.db.conn.execute("SELECT state FROM alpha_groups WHERE id=? AND state NOT IN ('closed','canceled','abstained')", [lease['owner_id']]).fetchone()
                    active = active or bool(obligation)
                elif lease['owner_type']=='legacy':
                    active=active or bool(self.db.one("SELECT id FROM trades WHERE id=? AND account_id=? AND status IN ('opening','open','closing')",[lease['owner_id'],self.account_id]))
                elif lease['owner_type']=='manual':
                    unseen=self.db.query('SELECT * FROM manual_orders WHERE account_id=? AND attempted_ts IS NOT NULL AND inventory_seen_ts IS NULL',[self.account_id])
                    active=active or any(Decimal(str((m.get('broker_json') or {}).get('filled_qty') or 0))>0 and resources.intersection(m['review_json']['resources']) for m in unseen)
                if held or active or lease['quarantine']:
                    with self.db.lock:
                        self.db.conn.execute('UPDATE execution_leases SET flat_since=NULL WHERE account_id=? AND resource=?', [self.account_id, lease['resource']])
                elif lease['flat_since'] and time.time() - lease['flat_since'] >= 10:
                    with self.db.lock:
                        self.db.conn.execute('DELETE FROM execution_leases WHERE account_id=? AND resource=?', [self.account_id, lease['resource']])
                else:
                    with self.db.lock:
                        self.db.conn.execute('UPDATE execution_leases SET flat_since=COALESCE(flat_since,?) WHERE account_id=? AND resource=?', [time.time(), self.account_id, lease['resource']])
