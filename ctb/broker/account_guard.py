"""Account equity-change entry circuit breaker. Exits never depend on this guard."""
from __future__ import annotations

import hashlib
import math
import time
from decimal import Decimal, InvalidOperation


def amount(value):
    if value is None or isinstance(value, bool):
        raise ValueError('Missing account amount')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Invalid account amount') from None
    if not number.is_finite() or not math.isfinite(float(number)):
        raise ValueError('Nonfinite account amount')
    return number


def failure_file(settings, account_id):
    suffix = hashlib.sha256(account_id.encode()).hexdigest()[:16]
    return settings.kill_file.with_name(settings.kill_file.name + '.guard-' + suffix)


def observe(db, settings, account, *, now=None, record_snapshot=True):
    """Commit the immutable reference and latch before reporting entry readiness."""
    now = time.time() if now is None else now
    observed_at = float(account.get('_observed_at', now))
    if not math.isfinite(observed_at) or not 0 < observed_at <= now:
        raise ValueError('Invalid broker observation time')
    account_id = account.get('id')
    if not account_id or account_id != settings.alpaca_account_id:
        raise ValueError('Account loss guard pin mismatch')
    equity, cash = amount(account.get('equity')), amount(account.get('cash'))
    previous = amount(account.get('last_equity', 0))
    if previous < 0:
        raise ValueError('Invalid prior-close equity')
    limit = amount(settings.daily_loss_limit_pct)
    if not 0 < limit < 100 or not math.isfinite(now) or now <= 0:
        raise ValueError('Invalid account guard limit or observation time')
    marker = failure_file(settings, account_id)
    with db.lock:
        db.conn.execute('BEGIN IMMEDIATE')
        try:
            row = db.conn.execute('SELECT * FROM account_loss_guards WHERE account_id=?', [account_id]).fetchone()
            if row is None:
                anchor = previous if previous > 0 else equity
                if anchor <= 0:
                    raise ValueError('No positive account loss reference')
                db.conn.execute('INSERT INTO account_loss_guards(account_id,anchor_equity,captured_at,source) VALUES(?,?,?,?)',
                    [account_id, str(anchor), observed_at, 'prior_close' if previous > 0 else 'first_observation'])
                row = db.conn.execute('SELECT * FROM account_loss_guards WHERE account_id=?', [account_id]).fetchone()
            anchor = amount(row['anchor_equity'])
            if anchor <= 0 or row['source'] not in ('prior_close', 'first_observation') or not 0 < row['captured_at'] <= now:
                raise ValueError('Stored account loss reference is invalid')
            reference = max(anchor, previous)
            threshold = reference * (1 - limit / 100)
            if equity <= threshold and row['breached_at'] is None:
                db.conn.execute('UPDATE account_loss_guards SET breached_at=?,breach_reference=?,breach_equity=?,breach_limit_pct=? WHERE account_id=? AND breached_at IS NULL',
                    [now, str(reference), str(equity), str(limit), account_id])
                row = db.conn.execute('SELECT * FROM account_loss_guards WHERE account_id=?', [account_id]).fetchone()
            if record_snapshot:
                db.conn.execute('INSERT INTO account_equity_snapshots VALUES(?,?,?,?,?) ON CONFLICT(account_id,minute) DO UPDATE SET ts=excluded.ts,equity=excluded.equity,cash=excluded.cash WHERE excluded.ts>=account_equity_snapshots.ts',
                    [account_id, int(observed_at // 60), observed_at, float(equity), float(cash)])
            db.conn.execute('COMMIT')
        except BaseException:
            db.conn.execute('ROLLBACK')
            raise
    blocked = row['breached_at'] is not None or marker.exists()
    return {'ready': True, 'blocked': blocked, 'account_id': account_id, 'anchor_equity': float(anchor),
        'captured_at': row['captured_at'], 'source': row['source'], 'equity_change': float(equity - anchor),
        'reference_equity': float(reference), 'entry_floor': float(threshold), 'limit_pct': float(limit),
        'breached_at': row['breached_at'], 'breach_reference': row['breach_reference'],
        'breach_equity': row['breach_equity'], 'breach_limit_pct': row['breach_limit_pct'],
        'reason': 'Account loss guard latched; operator review required' if blocked else None}


def update(db, settings, account, *, now=None, record_snapshot=True):
    """A guard storage error blocks entries but must not abort broker reconciliation."""
    try:
        return observe(db, settings, account, now=now, record_snapshot=record_snapshot)
    except Exception as exc:
        account_id = account.get('id') or settings.alpaca_account_id
        reason = 'Account loss guard unavailable: ' + type(exc).__name__
        if account_id == settings.alpaca_account_id:
            marker = failure_file(settings, account_id)
            try:
                marker.touch(mode=0o600, exist_ok=True)
                marker.chmod(0o600)
            except OSError:
                # A failure that cannot be committed is never described as durable.
                reason += '; entry-block marker could not be saved'
            settings.trading_enabled = False
        return {'ready': False, 'blocked': True, 'account_id': account_id, 'reason': reason}
