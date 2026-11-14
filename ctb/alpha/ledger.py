"""Additive experiment journal. Source and decision records are append-only."""
import json
import re
import time

SCHEMA = '''
CREATE TABLE IF NOT EXISTS alpha_versions (
 id TEXT PRIMARY KEY, registered_at REAL, config_hash TEXT, config_json TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'disabled', reason TEXT,
 armed_at REAL, allocation REAL NOT NULL DEFAULT 0, peak_nav REAL, next_review REAL
);
CREATE TABLE IF NOT EXISTS alpha_sources (
 id TEXT PRIMARY KEY, provider TEXT, external_id TEXT, entity TEXT, cik TEXT, url TEXT,
 published_at REAL, original_start REAL, original_end REAL, observed_at REAL NOT NULL,
 retrieved_at REAL, processed_at REAL, ingest_kind TEXT NOT NULL, content_hash TEXT NOT NULL,
 raw TEXT NOT NULL, text TEXT NOT NULL, metadata_json TEXT, previous_id TEXT,
 UNIQUE(provider,external_id,content_hash)
);
CREATE INDEX IF NOT EXISTS alpha_sources_entity ON alpha_sources(entity,published_at);
CREATE TABLE IF NOT EXISTS alpha_passages (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, ordinal INTEGER, text TEXT NOT NULL, content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alpha_entities (
 symbol TEXT PRIMARY KEY, cik TEXT, name TEXT, sector TEXT, observed_at REAL, metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS alpha_relationships (
 id TEXT PRIMARY KEY, customer TEXT, supplier TEXT, sector TEXT, source_id TEXT, passage_id TEXT,
 observed_at REAL, effective_at REAL, expires_at REAL, exposure_pct REAL, exposure_floor_pct REAL,
 materiality TEXT, classification_id TEXT, support REAL, state TEXT, metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS alpha_events (
 id TEXT PRIMARY KEY, customer TEXT, period TEXT, kind TEXT, source_id TEXT,
 published_at REAL, original_start REAL, observed_at REAL, processed_at REAL,
 eligible_until REAL, ingest_kind TEXT, state TEXT, reason TEXT, metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS alpha_event_sources (
 event_id TEXT, source_id TEXT, PRIMARY KEY(event_id,source_id)
);
CREATE TABLE IF NOT EXISTS alpha_classifications (
 id TEXT PRIMARY KEY, version TEXT, purpose TEXT, requested_model TEXT, responding_model TEXT,
 prompt_version TEXT, input_hash TEXT, request_json TEXT, response_json TEXT, evidence_json TEXT,
 started_at REAL, completed_at REAL, state TEXT, reserved_usd REAL, actual_usd REAL,
 usage_json TEXT, latency_ms REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS alpha_decisions (
 id TEXT PRIMARY KEY, version TEXT, event_id TEXT, relationship_id TEXT, source_id TEXT,
 classification_id TEXT, cutoff_at REAL, decided_at REAL, action TEXT, state TEXT, reason TEXT,
 features_json TEXT, evidence_json TEXT, UNIQUE(version,event_id,relationship_id,source_id)
);
CREATE TABLE IF NOT EXISTS alpha_groups (
 id TEXT PRIMARY KEY, version TEXT, event_id TEXT, decision_id TEXT, account_id TEXT, state TEXT,
 created_at REAL, entry_window_start REAL, entry_deadline REAL, recovery_deadline REAL,
 exit_deadline REAL, reason TEXT, target_json TEXT, risk_json TEXT, UNIQUE(version,event_id)
);
CREATE TABLE IF NOT EXISTS alpha_orders (
 id TEXT PRIMARY KEY, group_id TEXT, owner TEXT, account_id TEXT, symbol TEXT, role TEXT,
 client_order_id TEXT UNIQUE, request_json TEXT, created_at REAL, deadline REAL,
 attempted_at REAL, status TEXT, broker_json TEXT, error TEXT, accounted_qty TEXT DEFAULT '0',
 accounted_value TEXT DEFAULT '0', intent_effect TEXT
);
CREATE TABLE IF NOT EXISTS alpha_positions (
 id TEXT PRIMARY KEY, version TEXT, group_id TEXT, account_id TEXT, symbol TEXT,
 qty TEXT NOT NULL DEFAULT '0', average REAL, realized REAL DEFAULT 0, exit_deadline REAL,
 state TEXT, metadata_json TEXT, UNIQUE(group_id,symbol)
);
CREATE TABLE IF NOT EXISTS alpha_fills (
 id TEXT PRIMARY KEY, order_id TEXT, group_id TEXT, account_id TEXT, symbol TEXT,
 filled_at REAL, qty TEXT, signed_qty TEXT, price REAL, cash_flow REAL, source_json TEXT
);
CREATE TABLE IF NOT EXISTS alpha_cashflows (
 id TEXT PRIMARY KEY, version TEXT, at REAL, kind TEXT, amount REAL, group_id TEXT,
 symbol TEXT, source_id TEXT, metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS alpha_nav (
 id TEXT PRIMARY KEY, version TEXT, session TEXT, marked_at REAL, gross_nav REAL,
 net_nav REAL, external_flow REAL, gross_exposure REAL, net_exposure REAL, beta REAL,
 costs REAL, status TEXT, marks_json TEXT, assumptions_json TEXT, UNIQUE(version,session)
);
CREATE TABLE IF NOT EXISTS alpha_jobs (
 id TEXT PRIMARY KEY, kind TEXT, state TEXT, params_json TEXT, available_at REAL,
 claimed_at REAL, lease_until REAL, attempts INTEGER DEFAULT 0, completed_at REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS alpha_heartbeats (
 id TEXT PRIMARY KEY, heartbeat REAL, last_success REAL, last_failure REAL, next_run REAL, error TEXT
);
CREATE TABLE IF NOT EXISTS alpha_experiments (
 id TEXT PRIMARY KEY, registered_at REAL, version TEXT, name TEXT, cutoff_at REAL,
 params_json TEXT, state TEXT, result_json TEXT
);
CREATE TABLE IF NOT EXISTS alpha_outcomes (
 id TEXT PRIMARY KEY, decision_id TEXT, variant TEXT, event_id TEXT, entry_at REAL, mature_at REAL,
 state TEXT, features_json TEXT, result_json TEXT, UNIQUE(decision_id,variant)
);
CREATE TABLE IF NOT EXISTS alpha_state (
 id TEXT PRIMARY KEY, value_json TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS alpha_broker_activities (
 id TEXT PRIMARY KEY, external_id TEXT, observed_at REAL, at REAL, kind TEXT, symbol TEXT,
 order_id TEXT, raw_json TEXT, content_hash TEXT
);
CREATE TABLE IF NOT EXISTS alpha_corporate_actions (
 id TEXT PRIMARY KEY, external_id TEXT, observed_at REAL, kind TEXT, symbol TEXT,
 effective_at REAL, raw_json TEXT, content_hash TEXT
);
CREATE TABLE IF NOT EXISTS alpha_nav_revisions (
 id TEXT PRIMARY KEY, session TEXT, revised_at REAL, previous_json TEXT, reason TEXT
);
'''


class Journal:
    def __init__(self, db):
        self.db = db
        with db.lock:
            db.conn.executescript(SCHEMA)
            cols={r[1] for r in db.conn.execute('PRAGMA table_info(alpha_orders)')}
            if 'intent_json' not in cols: db.conn.execute('ALTER TABLE alpha_orders ADD COLUMN intent_json TEXT')
            for table in ('alpha_sources','alpha_passages','alpha_decisions','alpha_fills','alpha_cashflows','alpha_broker_activities','alpha_corporate_actions','alpha_nav_revisions'):
                for action in ('UPDATE','DELETE'):
                    db.conn.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable experiment journal'); END")
            db.conn.execute("CREATE TRIGGER IF NOT EXISTS frozen_alpha_config BEFORE UPDATE OF config_json,config_hash ON alpha_versions BEGIN SELECT RAISE(ABORT,'Register a new strategy version'); END")
            db.conn.execute("CREATE TRIGGER IF NOT EXISTS frozen_alpha_target BEFORE UPDATE OF target_json,risk_json ON alpha_groups BEGIN SELECT RAISE(ABORT,'Approved holdings are immutable'); END")

    @staticmethod
    def encode(values):
        return {k: json.dumps(v, sort_keys=True, allow_nan=False, default=str) if k.endswith('_json') and v is not None else v for k, v in values.items()}

    @staticmethod
    def decode(row):
        if row is None:
            return None
        return {k: json.loads(v) if k.endswith('_json') and v is not None else v for k, v in dict(row).items()}

    def query(self, sql, params=()):
        with self.db.lock:
            rows = self.db.conn.execute(sql, params).fetchall()
        return [self.decode(r) for r in rows]

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table, values):
        assert re.fullmatch(r'alpha_[a-z_]+', table)
        values = self.encode(values)
        with self.db.lock:
            return self.db.conn.execute(f"INSERT OR IGNORE INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values())).rowcount

    def update(self, table, identity, values):
        assert re.fullmatch(r'alpha_[a-z_]+', table)
        if table in ('alpha_sources', 'alpha_passages', 'alpha_decisions', 'alpha_fills'):
            raise ValueError('Evidence and decisions are immutable')
        values = self.encode(values)
        with self.db.lock:
            self.db.conn.execute(f"UPDATE {table} SET {','.join(k+'=?' for k in values)} WHERE id=?", [*values.values(), identity])

    def state(self, key, value=None, *, write=False):
        if write:
            with self.db.lock:
                self.db.conn.execute('INSERT INTO alpha_state VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at',
                                     [key, json.dumps(value, allow_nan=False, default=str), time.time()])
        row = self.one('SELECT * FROM alpha_state WHERE id=?', [key])
        return row['value_json'] if row else None

    def heartbeat(self, name, *, error=None, next_run=None):
        now = time.time()
        with self.db.lock:
            self.db.conn.execute('INSERT INTO alpha_heartbeats(id,heartbeat,last_success,last_failure,next_run,error) VALUES(?,?,?,?,?,?) '
                                 'ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat, '
                                 'last_success=COALESCE(excluded.last_success,alpha_heartbeats.last_success), '
                                 'last_failure=COALESCE(excluded.last_failure,alpha_heartbeats.last_failure),next_run=excluded.next_run,error=excluded.error',
                                 [name, now, None if error else now, now if error else None, next_run, error])
