"""Alpaca Trading API adapter. Trading is permanently pinned to PAPER.

Never pass dashboard session tokens or browser credentials to this client.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
OPTION_SYMBOL = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}


def asset_class(symbol: str) -> str:
    return "crypto" if "/" in symbol else "option" if OPTION_SYMBOL.fullmatch(symbol) else "us_equity"


def timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class AlpacaError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Alpaca HTTP {status}: {message}")


class AlpacaPaper:
    def __init__(self, session: aiohttp.ClientSession, *, key: str | None = None, secret: str | None = None):
        self.session, self._key, self._secret = session, key, secret

    @property
    def headers(self) -> dict[str, str]:
        key = self._key if self._key is not None else os.environ.get("APCA_API_KEY_ID", "")
        secret = self._secret if self._secret is not None else os.environ.get("APCA_API_SECRET_KEY", "")
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    @property
    def configured(self) -> bool:
        return all(self.headers.values())

    async def request(self, method: str, path: str, *, data_api: bool = False,
                      params: dict | None = None, body: dict | None = None) -> Any:
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ValueError("Alpaca requests require a relative API path")
        base = DATA_URL if data_api else PAPER_URL
        async with self.session.request(method, base + path, headers=self.headers, params=params, json=body,
                                        timeout=aiohttp.ClientTimeout(total=25), allow_redirects=False) as r:
            text = await r.text()
            if not 200 <= r.status < 300:
                try:
                    msg = (await r.json()).get("message", text[:200])
                except Exception:
                    msg = text[:200]
                for credential in self.headers.values():
                    if credential:
                        msg = str(msg).replace(credential, "[redacted]")
                raise AlpacaError(r.status, str(msg))
            return await r.json() if text else None

    async def account(self) -> dict:
        return await self.request("GET", "/v2/account")

    async def clock(self) -> dict:
        return await self.request("GET", "/v2/clock")

    async def positions(self) -> list[dict]:
        return await self.request("GET", "/v2/positions")

    async def orders(self, **params) -> list[dict]:
        return await self.request("GET", "/v2/orders", params={"status": "open", "limit": 500, "nested": "true", **params})

    async def asset(self, symbol: str) -> dict:
        path = "/v2/options/contracts/" if asset_class(symbol) == "option" else "/v2/assets/"
        return await self.request("GET", path + quote(symbol, safe=""))

    async def order(self, client_id: str) -> dict:
        return await self.request("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_id})

    async def submit(self, body: dict) -> dict:
        if not str(body.get("client_order_id", "")).startswith("ctba-"):
            raise ValueError("Orders require a harness-owned client_order_id")
        # No automatic retry for order writes: ambiguous responses are recovered by client id.
        return await self.request("POST", "/v2/orders", body=body)

    async def cancel(self, order: dict) -> None:
        if not str(order.get("client_order_id", "")).startswith("ctba-"):
            raise ValueError("Refusing to cancel an order owned by another strategy")
        await self.request("DELETE", "/v2/orders/" + quote(order["id"], safe=""))

    async def bars(self, symbol: str, tf: str, start: float, end: float, *, limit: int = 1000,
                   sort: str = "desc", page_token: str | None = None) -> dict:
        kind = asset_class(symbol)
        path = {"crypto": "/v1beta3/crypto/us/bars", "option": "/v1beta1/options/bars",
                "us_equity": "/v2/stocks/bars"}[kind]
        minutes = int(tf)
        timeframe = f"{minutes}Min" if minutes < 60 else f"{minutes // 60}Hour"
        params = {"symbols": symbol, "timeframe": timeframe, "start": iso(start), "end": iso(end),
                  "limit": limit, "sort": sort}
        if kind == "us_equity":
            params.update(feed="iex", adjustment="all")
        if page_token:
            params["page_token"] = page_token
        return await self.request("GET", path, data_api=True, params=params)

    async def snapshots(self, symbols: list[str], kind: str) -> dict:
        if not symbols:
            return {}
        path = {"crypto": "/v1beta3/crypto/us/snapshots", "option": "/v1beta1/options/snapshots",
                "us_equity": "/v2/stocks/snapshots"}[kind]
        params = {"symbols": ",".join(symbols)}
        if kind == "us_equity":
            params["feed"] = "iex"
        if kind == "option":
            params["feed"] = "indicative"
        data = await self.request("GET", path, data_api=True, params=params)
        return data.get("snapshots", data)
