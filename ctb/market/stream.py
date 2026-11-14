"""Authenticated Alpaca streams with subscription acknowledgement and reconnects."""
from __future__ import annotations

import asyncio
import time

import aiohttp


class AlpacaStream:
    def __init__(self, client, url, subscription, on_message, on_status):
        self.client, self.url, self.subscription = client, url, subscription
        self.on_message, self.on_status = on_message, on_status
        self._stop = asyncio.Event()
        self.symbol_limit = None

    def stop(self):
        self._stop.set()

    async def run(self):
        retry = 1
        while not self._stop.is_set():
            try:
                if not self.client.configured:
                    self.on_status('unconfigured', 'Add Alpaca paper keys')
                else:
                    self.on_status('connecting', None)
                    async with self.client.session.ws_connect(self.url, headers=self.client.headers, heartbeat=20) as ws:
                        subscribed = False
                        authenticated = False
                        deadline = time.monotonic() + 15
                        wanted = self.subscription()
                        while not self._stop.is_set():
                            desired = self.subscription()
                            if authenticated and desired != wanted:
                                remove = {k: sorted(set(v) - set(desired.get(k, []))) for k, v in wanted.items()}
                                add = {k: sorted(set(v) - set(wanted.get(k, []))) for k, v in desired.items()}
                                remove = {k: v for k, v in remove.items() if v}
                                add = {k: v for k, v in add.items() if v}
                                if remove:
                                    await ws.send_json({'action': 'unsubscribe', **remove})
                                if add:
                                    await ws.send_json({'action': 'subscribe', **add})
                                wanted = desired
                                subscribed = False
                                deadline = time.monotonic() + 15
                                self.on_status('subscribing', None)
                            try:
                                frame = await ws.receive(timeout=5)
                            except asyncio.TimeoutError:
                                if not subscribed and time.monotonic() > deadline:
                                    raise RuntimeError('Subscription acknowledgement timed out')
                                continue
                            if frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                raise RuntimeError('Stream disconnected')
                            if frame.type != aiohttp.WSMsgType.TEXT:
                                continue
                            messages = frame.json()
                            for message in messages:
                                kind = message.get('T')
                                if kind == 'error':
                                    if message.get('code') == 405:
                                        count = len({s for values in wanted.values() for s in values})
                                        if count > 1:
                                            # Entitlements can be stricter than the advertised plan maximum.
                                            # Reconnect with fewer priority symbols rather than losing all streams.
                                            self.symbol_limit = max(1, count // 2)
                                    raise RuntimeError(f"Alpaca stream {message.get('code')}: {message.get('msg')}")
                                if kind == 'success' and message.get('msg') == 'authenticated':
                                    authenticated = True
                                    await ws.send_json({'action': 'subscribe', **wanted})
                                elif kind == 'subscription':
                                    if not all(set(symbols) <= set(message.get(channel, [])) for channel, symbols in wanted.items()):
                                        if time.monotonic() > deadline:
                                            raise RuntimeError('Stream did not acknowledge all requested symbols')
                                        continue  # Unsubscribe acknowledgement can precede the new subscription.
                                    subscribed = True
                                    retry = 1
                                    self.on_status('live', None)
                                elif kind not in ('success', 'subscription'):
                                    await self.on_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No request headers, keys or raw transport objects enter the UI/logs.
                error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                self.on_status('reconnecting', error)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=retry)
            except asyncio.TimeoutError:
                pass
            retry = min(retry * 2, 30)
        self.on_status('stopped', None)
