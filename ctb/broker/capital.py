"""Conservative uncommitted capital for sequential paper-strategy entries."""
from decimal import Decimal, InvalidOperation
import math


FINAL = {'filled','canceled','expired','rejected','replaced'}


def amount(value):
    try:
        result=Decimal(str(value))
        if isinstance(value,bool) or not result.is_finite() or not math.isfinite(float(result)):
            raise ValueError()
        return result
    except (InvalidOperation,ValueError,TypeError,OverflowError):
        raise ValueError('Remaining capital requires finite broker and reservation amounts') from None


def remaining_capital(snapshot, intents, allocations, *, crypto=False, exclude_id=None):
    """Reserve gross exposure dollar-for-dollar, including short and unseen fills.

    Buying power only restricts this cash/equity pool; it never multiplies it.
    Exits release capital through fresh broker balances and inventory, not signals.
    """
    account=snapshot['account']
    equity=amount(account.get('equity'));cash=amount(account.get('cash'))
    signed=sum((amount(p.get('market_value')) for p in snapshot['positions']),Decimal(0))
    if abs(equity-cash-signed)>Decimal('.05'):
        raise ValueError('Broker cash, equity and inventory snapshots disagree; wait for reconciliation')
    gross=sum((abs(amount(p.get('market_value'))) for p in snapshot['positions']),Decimal(0))
    short_basis=Decimal(0)
    for position in snapshot['positions']:
        if position.get('side')=='short':
            # Cash contains both the proceeds and the original capital backing a short.
            # Keep both unavailable until close, including when the short has a profit.
            basis=position.get('cost_basis')
            if basis is None:
                from .alpaca import asset_class
                multiplier=100 if asset_class(position.get('symbol',''))=='option' else 1
                basis=amount(position.get('qty'))*amount(position.get('avg_entry_price'))*multiplier
            short_basis+=abs(amount(basis))
    other=sum((amount(a['amount']) for a in allocations),Decimal(0))
    if any(amount(a['amount'])<0 for a in allocations):
        raise ValueError('Remaining capital cannot use a negative owner reservation')
    by_link={i['client_order_id']:i for i in intents}
    visible={o.get('client_order_id') for o in snapshot['orders']}
    if any(link not in by_link for link in visible):
        raise ValueError('Unowned open paper orders block remaining-capital sizing')
    pending=Decimal(0);native_pending=Decimal(0)
    for intent in intents:
        if intent['id']==exclude_id or intent['effect'] not in ('increase','repair'):
            continue
        broker=intent.get('broker_json') or {}
        if intent['status'] in FINAL and intent.get('attempted_at') is not None:
            if not (intent['status']=='rejected' and not broker) and 'filled_qty' not in broker:
                raise ValueError('Terminal entry lacks cumulative fill evidence')
        requested=amount(intent['request_json']['qty'])
        filled=amount(broker.get('filled_qty') or 0)
        seen=amount(intent.get('inventory_seen_qty') or 0)
        notional=amount(intent.get('notional'))
        if requested<=0 or not 0<=seen<=filled<=requested or notional<0:
            raise ValueError('Remaining capital has an inconsistent entry reservation')
        unfilled=Decimal(0) if intent['status'] in FINAL else requested-filled
        unseen=filled-seen
        price=notional/requested
        if unseen:
            from .alpaca import asset_class
            multiplier=100 if asset_class(intent.get('symbol',''))=='option' else 1
            fill_price=amount(broker.get('filled_avg_price'))*multiplier
            if fill_price<=0:raise ValueError('Unseen entry fills require a valid execution price')
        else:fill_price=price
        reserved=unfilled*price+unseen*max(price,fill_price)
        pending+=reserved
        native_pending+=reserved*(Decimal('1.03') if intent['request_json'].get('side')=='sell' else 1)
    # Account capacity may lag a visible order or fill. Reserve both until inventory
    # is proved; temporary underallocation is preferable to reusing those funds.
    power='non_marginable_buying_power' if crypto else 'buying_power'
    capacity=max(Decimal(0),amount(account.get(power))-other-native_pending)
    free=max(Decimal(0),min(equity-gross-pending-other,cash-2*short_basis,capacity))
    return {'equity':float(equity),'gross_exposure':float(gross),'pending_entries':float(pending),
            'other_allocations':float(other),'available_capital':float(free)}
