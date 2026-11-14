import test from 'node:test';
import assert from 'node:assert/strict';
import { chartMarkers, fillLoader } from '../lib/chart-markers.ts';
import { latestAccount } from '../lib/account-snapshot.ts';
import { sg } from '../lib/format.ts';

const symbol='ETH/USD', at=1794683101, fillAt=1794683108;
const signal=(ts,action)=>({symbol,ts,action});
const fill=(id,at,effect='entry')=>({id,at,symbol,effect,position_side:'long',qty:2.7,price:1832.96});
const build=(decisions,fills,tf='1')=>chartMarkers(decisions,fills,symbol,tf,new Set(tf==='5'?[1794682800,1794683100,1794683700]:[1794682800,1794683100,1794683700,1794683820]),'green','red');

test('HOLD never overwrites the LONG signal in either response order',()=>{
  const decisions=[signal(at,'long'),signal(at+30,'hold'),signal(at-30,'hold')];
  for(const rows of [decisions,[...decisions].reverse()])assert.ok(build(rows,[]).some(m=>m.text==='LONG SIGNAL'));
});
test('confirmed entry and exit survive later signals, reloads, and a five-minute chart',()=>{
  const fills=[fill('buy',fillAt),fill('sell',1794683828,'exit')];
  for(const tf of ['1','5']){
    const markers=build([signal(at+30,'hold')],fills,tf);
    assert.equal(markers.filter(m=>m.text==='LONG FILL').length,1);
    assert.equal(markers.filter(m=>m.text==='EXIT FILL').length,1);
    assert.equal(markers.find(m=>m.text==='LONG FILL').time,1794683100);
  }
});
test('a submitted/rejected signal cannot invent a broker fill',()=>{
  const markers=build([signal(at,'long')],[]);
  assert.ok(markers.some(m=>m.text==='LONG SIGNAL'));
  assert.ok(markers.every(m=>!m.text.includes('FILL')));
});
test('deduplicate order IDs and exclude other symbols and out-of-range events',()=>{
  const f=fill('buy',fillAt);
  const rows=[f,f,{...f,id:'other',symbol:'BTC/USD'},fill('old',at-86400)];
  assert.equal(build([],rows).length,1);
});
test('weekly fills map to the actual provider candle instead of an epoch Thursday',()=>{
  const monday=Date.parse('2026-11-09T00:00:00Z')/1000;
  const markers=chartMarkers([], [fill('buy',fillAt)],symbol,'W',new Set([monday]),'green','red');
  assert.equal(markers[0].time,monday);
});
test('same-symbol refresh and network failure retain confirmed fills',async()=>{
  let rows=[];
  const first=fillLoader(async()=>[fill('buy',fillAt)],value=>{rows=value;});
  await first.refresh();first.stop();
  const afterTrade=fillLoader(async()=>{throw Error('temporary reconnect');},value=>{rows=value;});
  await afterTrade.refresh();afterTrade.stop();
  assert.equal(build([],rows).find(m=>m.text==='LONG FILL').time,1794683100);
  assert.equal(chartMarkers([],rows,'BTC/USD','1',new Set([1794683100]),'green','red').length,0);
});
test('polling has only one in-flight request and drops obsolete effect responses',async()=>{
  let resolve,started=0,published=[];
  const loader=fillLoader(()=>{started++;return new Promise(done=>{resolve=done;});},rows=>published.push(rows));
  const pending=loader.refresh();await loader.refresh();assert.equal(started,1);
  loader.stop();resolve([fill('buy',fillAt)]);await pending;
  assert.equal(published.length,0);
});
test('old REST and queued account updates cannot replace newer live marks',()=>{
  const current={account_id:'paper',view_at:200,upnl:20};
  assert.equal(latestAccount(current,{account_id:'paper',view_at:199,upnl:10}),current);
  assert.equal(latestAccount(current,{account_id:'paper',view_at:201,upnl:30}).upnl,30);
  assert.equal(sg(null),'—');assert.equal(sg(0),'0.00');
});
