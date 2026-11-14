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
                        deadline = time.monotonic() + 15
                        wanted = self.subscription()
                        while not self._stop.is_set():
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
                                    raise RuntimeError(f"Alpaca stream {message.get('code')}: {message.get('msg')}")
                                if kind == 'success' and message.get('msg') == 'authenticated':
                                    await ws.send_json({'action': 'subscribe', **wanted})
                                elif kind == 'subscription':
                                    if not all(set(symbols) <= set(message.get(channel, [])) for channel, symbols in wanted.items()):
                                        raise RuntimeError('Stream did not acknowledge all requested symbols')
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
