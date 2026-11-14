"""Prospective shadow outcomes and purged chronological research cohorts."""
import math
import statistics
import time
from datetime import datetime

from ..broker.alpaca import iso
from .config import VERSION, digest


def purged_split(rows, cutoff):
    """Event groups are indivisible; only labels mature before the training cutoff."""
    train_events={r['event_id'] for r in rows if r['mature_at']<cutoff}
    test_events={r['event_id'] for r in rows if r['entry_at']>=cutoff}
    train_events-=test_events
    train=[r for r in rows if r['event_id'] in train_events and r['mature_at']<cutoff]
    test=[r for r in rows if r['event_id'] in test_events and r['entry_at']>=cutoff]
    return train,test


def ridge_fit(rows, penalty=1):
    """A deliberately small shadow residual-return model after enough independent events."""
    if len({r['event_id'] for r in rows})<30: return {'status':'insufficient_independent_events','minimum':30}
    x=[float(r['features_json'].get('exposure_pct') or 0)/100 for r in rows]
    y=[float(r['result_json']['net_residual_return']) for r in rows]
    xm,ym=statistics.mean(x),statistics.mean(y)
    slope=sum((a-xm)*(b-ym) for a,b in zip(x,y))/(sum((a-xm)**2 for a in x)+penalty)
    return {'status':'fitted_shadow','intercept':ym-slope*xm,'exposure_coefficient':slope,'ridge_penalty':penalty,
            'independent_events':len({r['event_id'] for r in rows}),'features':['explicit_exposure'],'promotion':'disabled'}


class ResearchLab:
    def __init__(self, service): self.svc,self.j,self.client=service,service.j,service.client

    async def price(self,symbol,at):
        # Snapshot sampling begins after the decision. Historical fills are never invented.
        result=await self.client.request('GET','/v2/stocks/bars',data_api=True,params={'symbols':symbol,'timeframe':'1Min',
            'start':iso(at),'end':iso(at+300),'feed':'iex','adjustment':'all','sort':'asc','limit':10})
        rows=result.get('bars',{}).get(symbol,[])
        if not rows: raise ValueError('No entitled IEX observation at the shadow sampling window')
        return {'price':float(rows[0]['o']),'observed_bar':rows[0],'sampled_at':time.time(),'adjustment':'all','executable_fill':False}

    async def tick(self):
        now=time.time();sessions=self.svc.sessions
        for row in self.j.query("SELECT * FROM alpha_outcomes WHERE state='awaiting_price_direction' LIMIT 100"):
            decision=self.j.one('SELECT * FROM alpha_decisions WHERE id=?',[row['decision_id']]);f=row['features_json']
            cutoff=decision['cutoff_at'];changes={};observations={}
            try:
                last_close=max(s['close'] for s in sessions.rows if s['close']<cutoff)
                for symbol in (f['supplier'],f['sector']):
                    result=await self.client.request('GET','/v2/stocks/bars',data_api=True,params={'symbols':symbol,'timeframe':'1Day',
                        'start':iso(last_close-10*86400),'end':iso(last_close+1),'feed':'iex','adjustment':'all','sort':'desc','limit':2})
                    bars=result.get('bars',{}).get(symbol,[])
                    if len(bars)!=2: raise ValueError('Two lagged closes are unavailable')
                    changes[symbol]=float(bars[0]['c'])/float(bars[1]['c'])-1;observations[symbol]=bars
                residual=changes[f['supplier']]-changes[f['sector']]
                self.j.update('alpha_outcomes',row['id'],{'state':'awaiting_entry','features_json':{**f,
                    'direction':'long' if residual>0 else 'short' if residual<0 else None,'price_sector_residual':residual,
                    'price_observations':observations,'price_information_cutoff':last_close}})
            except Exception as exc:
                self.j.update('alpha_outcomes',row['id'],{'state':'unavailable','result_json':{'reason':str(exc)[:200]}})
        for row in self.j.query("SELECT * FROM alpha_outcomes WHERE state IN ('awaiting_entry','open') ORDER BY entry_at,id LIMIT 200"):
            decision=self.j.one('SELECT * FROM alpha_decisions WHERE id=?',[row['decision_id']])
            f=row['features_json'];direction=f.get('direction')
            if direction not in ('long','short'):
                self.j.update('alpha_outcomes',row['id'],{'state':'abstained','result_json':{'reason':'Baseline has no eligible direction'}});continue
            starts=[t for s in sessions.rows for t in (s['open']+1800,s['close']-1800) if t>=decision['decided_at']]
            if not starts: continue
            entry=starts[0]
            if row['variant']=='delay_one_session':
                day=next(s for s in sessions.rows if s['open']<=entry<s['close'])
                next_day=next((s for s in sessions.rows if s['open']>day['close']),None)
                if not next_day: continue
                entry=next_day['open']+1800
            if now<entry+300: continue
            if row['state']=='awaiting_entry':
                try:
                    stock=await self.price(f['supplier'],entry);sector=await self.price(f['sector'],entry)
                    mature=sessions.exit_deadline(entry,10)
                    self.j.update('alpha_outcomes',row['id'],{'entry_at':entry,'mature_at':mature,'state':'open',
                        'features_json':{**f,'entry_stock':stock,'entry_sector':sector}})
                except Exception as exc:
                    self.j.update('alpha_outcomes',row['id'],{'state':'unavailable','result_json':{'reason':str(exc)[:250]}})
                continue
            if row['mature_at']>now-300: continue
            try:
                end_stock=await self.price(f['supplier'],row['mature_at']);end_sector=await self.price(f['sector'],row['mature_at'])
                sign=1 if direction=='long' else -1
                raw=sign*(end_stock['price']/f['entry_stock']['price']-1)
                sector_return=end_sector['price']/f['entry_sector']['price']-1
                hedge=0 if row['variant'] in ('long_only','stock_unhedged') else sign*sector_return
                # Adjusted-return economic shadow series already includes distributions/splits.
                cost=.0015*2+self.svc.config['extra_latency_slippage_bp']/10000*2
                cost+=self.svc.config['borrow_annual_assumption']*(row['mature_at']-row['entry_at'])/(365*86400)
                if row['variant']=='higher_cost': cost*=3
                self.j.update('alpha_outcomes',row['id'],{'state':'matured','result_json':{
                    'stock_return':raw,'sector_return':sector_return,'net_residual_return':raw-hedge-cost,
                    'modeled_cost':cost,'exit_stock':end_stock,'exit_sector':end_sector,
                    'fill_model':'IEX first-minute opening observation; assumed 15 bp per side, not an executable fill',
                    'price_adjustment':'all; no additional dividends or split cash flows','evaluated_at':now}})
            except Exception as exc:
                self.j.update('alpha_outcomes',row['id'],{'state':'unavailable','result_json':{'reason':str(exc)[:250]}})
        matured=self.j.query("SELECT * FROM alpha_outcomes WHERE state='matured' ORDER BY entry_at")
        variants={}
        for name in {r['variant'] for r in matured}:
            selected=[r for r in matured if r['variant']==name]
            variants[name]={'events':len({r['event_id'] for r in selected}),
                'mean_event_residual_return':statistics.mean(r['result_json']['net_residual_return'] for r in selected),
                'portfolio_sharpe':None,'reason':'Event residual returns are not daily portfolio returns'}
        last=self.j.state('last_shadow_training') or 0
        if now-last>=7*86400:
            primary=[r for r in matured if r['variant']=='drex_hedged']
            cutoff=now-30*86400
            train,test=purged_split(primary,cutoff)
            result=ridge_fit(train)
            if result['status']=='fitted_shadow':
                errors=[(r['result_json']['net_residual_return']-result['intercept']-result['exposure_coefficient']*float(r['features_json'].get('exposure_pct') or 0)/100)**2 for r in test]
                result['test_mse']=statistics.mean(errors) if errors else None
            params={'training_cutoff':cutoff,'matured_only':True,'label_overlap_purge':True,'group_by':'event',
                    'features':['explicit_exposure'],'ridge_penalty':1,'model':MODEL_ID,'auto_promote':False}
            self.j.insert('alpha_experiments',{'id':digest([VERSION,'weekly_ridge',int(now//(7*86400))]),'registered_at':now,
                'version':VERSION,'name':'weekly_ridge','cutoff_at':cutoff,'params_json':params,'state':'shadow_only','result_json':result})
            self.j.state('last_shadow_training',now,write=True)
        self.j.state('research_report',{'at':now,'matured_labels':len(matured),'independent_events':len({r['event_id'] for r in matured}),
            'variants':variants,'next_evaluation':max(now,last+7*86400),'minimum_training_events':30,
            'limitations':['Prospective narrow issuer cohort; no survivorship-complete historical universe',
                'Pretrained model history is not point-in-time training data','Few or no independent events cannot establish an edge',
                'Multiple tested variants are registered; no selection-adjusted significance claim is made'],
            'promotion':'Only an explicitly deployed new version can replace the active experiment'},write=True)


MODEL_ID='ridge-residual-v0-shadow'
