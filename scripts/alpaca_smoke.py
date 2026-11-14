"""Read-only connection check, or a <= $20 isolated crypto PAPER round trip with --execute.

Uses a separate smoke database; existing positions/orders are never adopted. No live host exists.
"""
import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aiohttp
from ctb.broker.alpaca import AlpacaPaper
from ctb.broker.alpaca_executor import AlpacaExecutor
from ctb.config import ROOT, load_settings
from ctb.db import DB
from ctb.market.alpaca import AlpacaFeed
from ctb.market.store import MarketStore


async def main(execute=False):
    s = replace(load_settings(), venue='alpaca', symbols=['BTC/USD'], trading_enabled=True,
                entry_mode='taker', risk_per_trade_pct=.001, dynamic_sizing=False,
                max_notional_usdt=20, maker_wait_s=2)
    async with aiohttp.ClientSession() as session:
        client = AlpacaPaper(session)
        account = await client.account()
        if account['id'] != s.alpaca_account_id:
            raise RuntimeError('Paper account does not match the configured harness account')
        positions, orders = await asyncio.gather(client.positions(), client.orders())
        if any(p['symbol'].replace('/', '') == 'BTCUSD' for p in positions) or any(o['symbol'].replace('/', '') == 'BTCUSD' for o in orders):
            raise RuntimeError('BTC already has a position/order; refusing to share a symbol for the smoke test')
        print('Connected to pinned Alpaca PAPER account; existing BTC position/order: none', flush=True)
        if not execute:
            return
        store = MarketStore(s.symbols)
        feed = AlpacaFeed(store, client)
        await feed.refresh_quotes()
        db = DB(ROOT / 'data' / 'alpaca-smoke.db')
        ex = AlpacaExecutor(s, db, client, lambda *_: None, feed)
        await ex.start()
        if ex.owned():
            raise RuntimeError('A prior smoke trade needs reconciliation before another test')
        summary = {'paper': True, 'account_id': account['id'], 'max_notional_usd': 20, 'started_at': time.time()}
        try:
            result = await ex.open(symbol='BTC/USD', side='long', sl_bp=100, tp_bp=150,
                                   decision_id=None, ev_r=1, horizon_min=5)
            print('Entry:', result, flush=True)
            if not result.get('ok'):
                raise RuntimeError(f'Paper entry did not fill: {result}')
            trade = db.one('SELECT * FROM trades WHERE id=?', [result['trade_id']])
            summary.update(entry_price=trade['entry_price'], qty=trade['qty'], entry_order_id=trade['entry_order_id'])
            stop = await client.order(f"{trade['entry_link_id']}-stop")
            if stop['type'] != 'stop_limit' or stop['status'] in ('rejected', 'canceled', 'expired'):
                raise RuntimeError('Broker-side stop-limit backstop was not verified')
            summary['backstop_verified'] = True
            print('Broker-side protection verified:', stop['type'], stop['status'], flush=True)
            await ex.close('BTC/USD', 'smoke_test')
            for _ in range(50):
                trade = db.one('SELECT * FROM trades WHERE id=?', [trade['id']])
                if trade['status'] == 'closed':
                    break
                await ex.continue_close(trade)
                await asyncio.sleep(2)
            trade = db.one('SELECT * FROM trades WHERE id=?', [trade['id']])
            if trade['status'] != 'closed':
                raise RuntimeError('Paper exit is still pending')
            pos, remaining_orders = await asyncio.gather(client.positions(), client.orders())
            if any(p['symbol'].replace('/', '') == 'BTCUSD' for p in pos):
                raise RuntimeError('Paper BTC position remains after exit')
            if any(o['symbol'].replace('/', '') == 'BTCUSD' for o in remaining_orders):
                raise RuntimeError('Paper BTC order remains after exit')
            summary.update(ok=True, closed_at=trade['closed_ts'], exit_price=trade['exit_price'],
                           fill_pnl_usd=trade['realized_pnl'], fees=trade['fees'], pnl_basis=trade['close_notes'], flat_verified=True)
            (ROOT / 'data' / 'alpaca-smoke-validation.json').write_text(json.dumps(summary, indent=2))
            print('Paper round trip:', summary, flush=True)
        finally:
            if ex.owned():
                await ex.kill('smoke cleanup')
                for _ in range(30):
                    for t in ex.owned():
                        if t['status'] == 'closing':
                            await ex.continue_close(t)
                    if not ex.owned():
                        break
                    await asyncio.sleep(2)


asyncio.run(main('--execute' in sys.argv))
