"""Read-only display marks and broker-confirmed chart fills."""
from __future__ import annotations

import math

from .alpaca import asset_class, timestamp


def mark_positions(positions, store, now):
    """Use the fresh liquidation side of a quote; never change execution state."""
    marked=[]
    for original in positions:
        p=dict(original)
        p.update(broker_mark=p['mark'],broker_upnl=p['upnl'],mark_source='broker',mark_at=None,mark_stale=True)
        st=store.state.get(p['symbol'])
        if st and asset_class(p['symbol'])!='option':
            try:
                bid=float(st.ticker.get('bid1Price') or 0);ask=float(st.ticker.get('ask1Price') or 0)
            except (TypeError,ValueError):
                marked.append(p);continue
            if all(math.isfinite(v) for v in (bid,ask,st.book_ts)) and 0<bid<=ask and -2<=now-st.book_ts<=5:
                long=p['side']=='long';price=bid if long else ask
                p.update(mark=price,upnl=(price-p['entry'])*p['size']*(1 if long else -1),
                    value=price*p['size'],mark_source='live_bid' if long else 'live_ask',mark_at=st.book_ts,mark_stale=False)
        marked.append(p)
    return marked


def mark_key(executor, store, now):
    """Refresh a browser on quote, inventory, or freshness changes."""
    rows=[]
    for p in executor.positions:
        st=store.state.get(p['symbol'])
        rows.append((p['symbol'],p['side'],p['size'],st.book_ts if st else None,
            st.ticker.get('bid1Price') if st else None,st.ticker.get('ask1Price') if st else None,
            bool(st and -2<=now-st.book_ts<=5)))
    return executor.last_sync,rows


def mark_basis(positions):
    if not positions: return 'flat'
    live=sum(p['mark_source'] in ('live_bid','live_ask') for p in positions)
    return 'live' if live==len(positions) else 'mixed' if live else 'broker'


def chart_fills(db,account_id,symbol=None):
    """Only exact canonical orders with nonzero broker fills can create fill arrows."""
    if not db.one("SELECT name FROM sqlite_master WHERE name='execution_intents'"): return []
    where="i.account_id=? AND i.owner_type='legacy' AND t.venue='alpaca' AND t.account_id=i.account_id"
    params=[account_id]
    if symbol: where+=' AND t.symbol=?';params.append(symbol)
    rows=db.query('SELECT i.broker_json,i.effect,t.id trade_id,t.symbol,t.side position_side FROM execution_intents i '
        'JOIN trades t ON i.owner_record=CAST(t.id AS TEXT) WHERE '+where,params)
    result={}
    for row in rows:
        order=row['broker_json'] or {}
        try:
            qty=float(order.get('filled_qty') or 0);price=float(order.get('filled_avg_price') or 0)
            at=timestamp(order.get('filled_at'));identity=order.get('id')
            if not identity or min(qty,price,at)<=0 or not all(math.isfinite(v) for v in (qty,price,at)): continue
        except (TypeError,ValueError): continue
        result[identity]={'id':identity,'trade_id':row['trade_id'],'symbol':row['symbol'],'at':at,
            'position_side':row['position_side'],'effect':'entry' if row['effect']=='increase' else 'exit','qty':qty,'price':price}
    return sorted(result.values(),key=lambda f:(f['at'],f['id']))
