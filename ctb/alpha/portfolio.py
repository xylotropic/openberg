"""Lagged, bounded equity targets. This module never submits orders."""
import math
import statistics
import time
from datetime import datetime, timezone

from ..broker.alpaca import iso, timestamp
from .config import VERSION


def covariance(x, y):
    return sum((a-statistics.mean(x))*(b-statistics.mean(y)) for a,b in zip(x,y))/(len(x)-1)


def beta(x, y):
    variance = statistics.variance(y)
    if variance <= 1e-14: raise ValueError('Risk benchmark has no usable variation')
    return covariance(x,y)/variance


def returns(rows):
    output = {}
    for before, after in zip(rows, rows[1:]):
        if before['c'] <= 0 or after['c'] <= 0: raise ValueError('Invalid historical close')
        output[after['t'][:10]] = after['c']/before['c']-1
    return output


class Portfolio:
    def __init__(self, service):
        self.service = service
        self.client, self.j, self.config = service.client, service.j, service.config

    async def history(self, symbol, cutoff):
        cache = self.j.state('risk:' + symbol)
        if cache and cache['cutoff'] == cutoff:
            return cache['rows']
        result = await self.client.request('GET','/v2/stocks/bars',data_api=True,params={
            'symbols':symbol,'timeframe':'1Day','start':iso(cutoff-180*86400),'end':iso(cutoff),
            'feed':'iex','adjustment':'all','sort':'desc','limit':1000})
        rows = sorted(result.get('bars',{}).get(symbol,[]),key=lambda r:r['t'])[-61:]
        if len(rows) < 41: raise ValueError('At least 41 closed daily prices are required')
        if result.get('next_page_token'): raise ValueError('Risk history is unexpectedly incomplete')
        self.j.state('risk:' + symbol,{'cutoff':cutoff,'rows':rows,'retrieved_at':time.time(),'adjustment':'all'},write=True)
        return rows

    async def construct(self, decision, relationship, window):
        svc, c = self.service, self.config
        if svc.j.state('accounting_gap'): raise ValueError('Unresolved corporate-action or accounting coverage gap')
        supplier, hedge = relationship['supplier'], relationship['sector']
        coverage=self.j.state('corporate_action_coverage') or {}
        if time.time()-coverage.get('through',0)>7200: raise ValueError('Corporate-action coverage is stale')
        horizon=svc.sessions.exit_deadline(time.time(),c['holding_sessions'])
        for action in svc.accounting.actions():
            if action['symbol'] in (supplier,hedge) and time.time()<=action['effective_at']<=horizon and action['kind']!='cash_dividend':
                raise ValueError('A pending noncash corporate action requires a separate reviewed implementation')
        co = svc.coordinator
        snapshot = await co.refresh()
        if co.state('account_quarantine'): raise ValueError('Unexplained broker activity requires reconciliation')
        if co.conflict([supplier,hedge],'alpha','unassigned'):
            raise ValueError('Supplier or hedge is reserved by another owner')
        if any(p['symbol'] in (supplier,hedge) for p in snapshot['positions']):
            raise ValueError('Existing broker inventory cannot be adopted')
        assets = {s:await self.client.asset(s) for s in (supplier,hedge)}
        for a in assets.values():
            if not a.get('tradable') or a.get('status','active')!='active': raise ValueError('An asset is not currently tradable')
        side = decision['action']
        stock_sign = 1 if side=='long' else -1
        snaps = await self.client.snapshots([supplier,hedge],'us_equity')
        quotes, prices = {}, {}
        now = time.time()
        for symbol in (supplier,hedge):
            q = snaps.get(symbol,{}).get('latestQuote',{})
            bid, ask = float(q.get('bp') or 0), float(q.get('ap') or 0)
            age = now - timestamp(q.get('t'))
            if not 0 < bid <= ask or not -1 <= age <= c['quote_age_s']:
                raise ValueError('Missing, crossed, future or stale IEX quote: ' + symbol)
            spread = 10000*(ask-bid)/((ask+bid)/2)
            if spread > c['spread_bp']: raise ValueError('Displayed IEX spread exceeds 15 bp: ' + symbol)
            quotes[symbol] = {'bid':bid,'ask':ask,'timestamp':q['t'],'spread_bp':spread,'feed':'iex'}
            prices[symbol] = (bid+ask)/2
            if prices[symbol] < c['minimum_price']: raise ValueError('Price is below the frozen liquidity floor')
        last_close = max(r['close'] for r in svc.sessions.rows if r['close'] < window['session']['open'])
        history = {s:await self.history(s,last_close+1) for s in (supplier,hedge,'SPY')}
        series = {s:returns(rows) for s,rows in history.items()}
        dates = sorted(set.intersection(*(set(r) for r in series.values())))[-60:]
        if len(dates) < c['minimum_risk_sessions']: raise ValueError('Insufficient aligned lagged daily returns')
        # Stale and missing sessions are not zero returns.
        expected_dates = [r['date'] for r in svc.sessions.rows if r['close'] <= last_close][-len(dates):]
        if dates != expected_dates: raise ValueError('Risk history has missing or stale exchange sessions')
        x, h, m = [[series[s][d] for d in dates] for s in (supplier,hedge,'SPY')]
        stock_beta, hedge_beta = beta(x,m), beta(h,m)
        volatility = statistics.stdev(x)*math.sqrt(252)
        if not math.isfinite(volatility) or volatility <= 0 or hedge_beta <= .1 or stock_beta <= 0:
            raise ValueError('The lagged hedge estimate is unusable')
        dollar_volume = statistics.mean(float(r.get('v') or 0)*r['c'] for r in history[supplier][-20:])
        if dollar_volume < c['minimum_iex_daily_dollar_volume']: raise ValueError('IEX daily dollar volume is too low')
        allocation = svc.version()['allocation']
        current = svc.settings
        whole_equity = float(snapshot['account']['equity'])
        # Full stock notional is the conservative per-trade risk charge, not an assumed stop loss.
        cap = min(allocation*c['company_pct']/100, current.max_notional_usdt,c['existing_max_notional'],
                  whole_equity*min(current.risk_per_trade_pct,c['existing_risk_per_trade_pct'])/100)
        stock_dollars = cap * min(1,c['annual_volatility_sizing_target']/volatility)
        stock_qty = math.floor(stock_dollars/prices[supplier])
        hedge_qty = round(stock_qty*prices[supplier]*stock_beta/hedge_beta/prices[hedge])
        if stock_qty < 1 or hedge_qty < 1: raise ValueError('A whole-share stock and hedge pair does not fit the risk cap')
        stock_value, hedge_value = stock_qty*prices[supplier], hedge_qty*prices[hedge]
        legs = [{'symbol':supplier,'qty':stock_qty,'side':'buy' if stock_sign>0 else 'sell','price':prices[supplier], 'beta':stock_beta,'kind':'supplier'},
                {'symbol':hedge,'qty':hedge_qty,'side':'sell' if stock_sign>0 else 'buy','price':prices[hedge], 'beta':hedge_beta,'kind':'sector_hedge'}]
        for leg in legs:
            if leg['side']=='sell' and not (assets[leg['symbol']].get('shortable') and assets[leg['symbol']].get('easy_to_borrow')):
                raise ValueError('Current shortability and easy-to-borrow eligibility are required')
        gross = stock_value+hedge_value
        net = stock_sign*(stock_value-hedge_value)
        beta_dollars = stock_sign*(stock_value*stock_beta-hedge_value*hedge_beta)
        owned = self.j.query("SELECT * FROM alpha_positions WHERE qty NOT IN ('0','0.0')")
        existing_gross, existing_net, existing_beta, sector_gross = 0.,0.,0.,0.
        for p in owned:
            marks = snaps.get(p['symbol'])
            if not marks:
                marks = (await self.client.snapshots([p['symbol']],'us_equity')).get(p['symbol'],{})
            px = float((marks.get('latestTrade') or {}).get('p') or p.get('average') or 0)
            value = float(p['qty'])*px
            existing_gross += abs(value); existing_net += value
            existing_beta += value*p['metadata_json'].get('beta',1)
            if p['metadata_json'].get('sector') == hedge: sector_gross += abs(value)
        pending = self.j.one("SELECT COUNT(*) n FROM alpha_groups WHERE state IN ('planned','entering','recovering','unwinding','closing','overdue')")['n']
        if pending: raise ValueError('Reconcile the existing execution group before adding risk')
        if gross > allocation*c['event_gross_pct']/100: raise ValueError('Event gross exposure cap binds')
        if existing_gross+gross > allocation*c['gross_pct']/100: raise ValueError('Strategy gross exposure cap binds')
        if sector_gross+gross > allocation*c['sector_gross_pct']/100: raise ValueError('Sector gross exposure cap binds')
        if abs(existing_net+net) > allocation*c['absolute_net_pct']/100: raise ValueError('Strategy net exposure cap binds')
        marked_nav=(self.j.state('live_nav') or {}).get('net_nav',allocation)
        if marked_nav<=0 or abs(existing_beta+beta_dollars)/marked_nav > c['absolute_beta']: raise ValueError('Estimated market beta cap binds')
        legacy_risk = sum(float(t.get('risk_usd') or 0) for t in svc.engine.executor.owned())
        if legacy_risk+existing_gross+gross > whole_equity*min(current.max_open_risk_pct,c['existing_max_open_risk_pct'])/100:
            raise ValueError('The tighter whole-account open-risk cap binds')
        if len(snapshot['positions'])+2 > min(current.max_positions,c['existing_max_positions']): raise ValueError('Whole-account position count cap binds')
        for leg in legs:
            direction = 'long' if leg['side']=='buy' else 'short'
            if sum(p.get('side')==direction for p in snapshot['positions']) >= min(current.max_same_side,c['existing_max_same_side']):
                raise ValueError('Whole-account same-side cap binds')
        if gross > co.available('alpha')*.9: raise ValueError('Shared paper buying power is insufficient')
        spread_cost = sum(leg['qty']*leg['price']*quotes[leg['symbol']]['spread_bp']/10000 for leg in legs)
        borrow = sum(leg['qty']*leg['price'] for leg in legs if leg['side']=='sell')*c['borrow_annual_assumption']*16/365
        estimated_cost = spread_cost + gross*c['extra_latency_slippage_bp']/10000*2+borrow
        if estimated_cost/gross > c['max_estimated_round_trip_bp']/10000: raise ValueError('Estimated round-trip friction exceeds the frozen 50 bp ceiling')
        return {'legs':legs,'sector':hedge,'quotes':quotes,'desired_at':now,'allocation':allocation}, {
            'gross':gross,'net':net,'beta_dollars':beta_dollars,'volatility':volatility,'stock_beta':stock_beta,'hedge_beta':hedge_beta,
            'lookback_dates':dates,'risk_price_adjustment':'all','history_cutoff':last_close,'dollar_volume_iex':dollar_volume,
            'estimated_round_trip_cost':estimated_cost,'borrow_rate_assumption':c['borrow_annual_assumption'],
            'spread_is_in_fill':True,'existing_gross':existing_gross,'existing_net':existing_net}
