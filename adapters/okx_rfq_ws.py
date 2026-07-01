"""
OKX RFQ / Block-Trading WebSocket — /ws/v5/business (authenticated).

OKX recommends consuming maker quotes over WebSocket, not REST polling. This
adapter logs in, subscribes to the private rfqs / quotes / struc-block-trades
channels, and keeps an in-memory cache of active quotes by rfqId so the
RFQExecutor can read them with zero network latency.

Login + reconnect mirror adapters/okx_ws_adapter.py (the proven private-WS
path). Market data and REST stay elsewhere; this handles quote consumption only.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

from adapters.okx_rfq_adapter import RFQQuote

logger = logging.getLogger(__name__)

_LIVE_BUSINESS_URL = "wss://ws.okx.com:8443/ws/v5/business"
_DEMO_BUSINESS_URL = "wss://wspap.okx.com:8443/ws/v5/business"
_BACKOFF = [1, 2, 4, 8, 16, 30]
_RFQ_CHANNELS = ("rfqs", "quotes", "struc-block-trades")


class OKXRFQWebSocket:
    """Streams RFQ maker quotes over WS and caches active quotes by rfqId."""

    def __init__(self, api_key: str, secret_key: str, passphrase: str,
                 is_testnet: bool = False):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.is_testnet = is_testnet

        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._connected = False
        self._logged_in = False
        self._login_event: Optional[asyncio.Event] = None
        self._clock_offset_s = 0.0

        self._receive_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None

        # rfqId -> {quoteId -> RFQQuote}  (active quotes only)
        self._quotes: Dict[str, Dict[str, RFQQuote]] = {}
        # latest executed block trades, newest first (for reconciliation/debug)
        self.block_trades: List[Dict[str, Any]] = []

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed

    # ------------------------------------------------------------------ public

    async def connect(self) -> bool:
        self._running = True
        try:
            await self._ws_connect()
            return True
        except Exception as e:
            logger.warning("[rfq_ws] initial connect failed: %s — reconnect loop active", e)
            if self._running and (self._reconnect_task is None or self._reconnect_task.done()):
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._connected = False
        self._logged_in = False
        await self._cancel_tasks()
        await self._close_ws()
        logger.info("[rfq_ws] disconnected")

    def get_quotes(self, rfq_id: str) -> List[RFQQuote]:
        """Active quotes for an rfqId from the in-memory cache (no network)."""
        return [q for q in self._quotes.get(rfq_id, {}).values() if q.is_active()]

    async def wait_for_quotes(self, rfq_id: str, min_quotes: int, timeout_sec: float) -> List[RFQQuote]:
        """Wait (polling the push-fed cache) until >= min_quotes active quotes
        exist for rfqId, or the timeout elapses. In-memory only — quotes arrive
        via the WS push, this just watches the cache."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            active = self.get_quotes(rfq_id)
            if len(active) >= min_quotes:
                return active
            await asyncio.sleep(0.05)
        return self.get_quotes(rfq_id)

    def clear_rfq(self, rfq_id: str) -> None:
        """Drop cached quotes for an rfqId once we're done with it."""
        self._quotes.pop(rfq_id, None)

    # ------------------------------------------------------------------ connect

    async def _sync_clock(self) -> None:
        """Server-relative login timestamp so host clock drift doesn't get us
        50102-rejected. public/time is unauthenticated."""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://www.okx.com/api/v5/public/time",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    d = await r.json()
                    server_ms = float(d["data"][0]["ts"])
                    self._clock_offset_s = time.time() - server_ms / 1000.0
        except Exception as e:
            logger.debug("[rfq_ws] clock sync failed (non-fatal): %s", e)

    async def _ws_connect(self) -> None:
        await self._cancel_tasks()
        await self._close_ws()
        await self._sync_clock()

        url = _DEMO_BUSINESS_URL if self.is_testnet else _LIVE_BUSINESS_URL
        headers = {"x-simulated-trading": "1"} if self.is_testnet else {}
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(url, headers=headers, heartbeat=None)
        logger.info("[rfq_ws] WS TCP connected (%s)", url)

        self._login_event = asyncio.Event()
        self._logged_in = False
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        await self._send_login()
        try:
            await asyncio.wait_for(self._login_event.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            await self._cancel_tasks()
            await self._close_ws()
            raise RuntimeError("RFQ WS login timed out")
        if not self._logged_in:
            raise RuntimeError("RFQ WS login rejected")

        await self._subscribe()
        self._connected = True
        logger.info("[rfq_ws] ready — connected, authenticated, subscribed to %s",
                    ", ".join(_RFQ_CHANNELS))

    async def _send_login(self) -> None:
        ts = str(int(time.time() - self._clock_offset_s))
        mac = hmac.new(self.secret_key.encode(), (ts + "GET" + "/users/self/verify").encode(),
                       hashlib.sha256)
        sign = base64.b64encode(mac.digest()).decode()
        await self._ws.send_str(json.dumps({
            "op": "login",
            "args": [{"apiKey": self.api_key, "passphrase": self.passphrase,
                      "timestamp": ts, "sign": sign}],
        }))

    async def _subscribe(self) -> None:
        await self._ws.send_str(json.dumps({
            "op": "subscribe",
            "args": [{"channel": ch} for ch in _RFQ_CHANNELS],
        }))

    # ------------------------------------------------------------------ receive

    async def _receive_loop(self) -> None:
        while self._running and self._ws and not self._ws.closed:
            try:
                msg = await self._ws.receive(timeout=35)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._dispatch(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning("[rfq_ws] connection closed/errored")
                    break
            except asyncio.TimeoutError:
                logger.warning("[rfq_ws] receive timeout (35s) — reconnecting")
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("[rfq_ws] receive error: %s", e)
                break

        self._connected = False
        self._logged_in = False
        if self._running and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _dispatch(self, raw: str) -> None:
        if raw == "pong":
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        event = msg.get("event")
        if event == "login":
            self._logged_in = msg.get("code") == "0"
            if not self._logged_in:
                logger.error("[rfq_ws] login failed: code=%s msg=%s", msg.get("code"), msg.get("msg"))
            if self._login_event:
                self._login_event.set()
            return
        if event == "subscribe":
            logger.debug("[rfq_ws] subscribed: %s", msg.get("arg"))
            return
        if event == "error":
            logger.error("[rfq_ws] error event: code=%s msg=%s", msg.get("code"), msg.get("msg"))
            return

        channel = (msg.get("arg") or {}).get("channel", "")
        data = msg.get("data") or []
        if channel == "quotes":
            self._on_quotes(data)
        elif channel == "struc-block-trades":
            for bt in data:
                self.block_trades.insert(0, bt)
            del self.block_trades[50:]
        # rfqs channel: RFQ state updates — not needed for quote consumption

    def _on_quotes(self, data: List[Dict[str, Any]]) -> None:
        for item in data:
            rfq_id = item.get("rfqId", "")
            quote_id = item.get("quoteId", "")
            if not rfq_id or not quote_id:
                continue
            state = item.get("state", "")
            book = self._quotes.setdefault(rfq_id, {})
            if state in ("canceled", "expired", "filled", "failed"):
                book.pop(quote_id, None)
            else:
                book[quote_id] = RFQQuote(item)

    async def _heartbeat_loop(self) -> None:
        while self._running and self._ws and not self._ws.closed:
            try:
                await asyncio.sleep(20)
                if self._ws and not self._ws.closed:
                    await self._ws.send_str("ping")
            except (asyncio.CancelledError, Exception):
                break

    async def _reconnect_loop(self) -> None:
        attempt = 0
        while self._running and not self._connected:
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            logger.info("[rfq_ws] reconnect attempt %d — waiting %ds", attempt + 1, delay)
            await asyncio.sleep(delay)
            await self._cancel_tasks()
            await self._close_ws()
            try:
                await self._ws_connect()
                logger.info("[rfq_ws] reconnected after %d attempt(s)", attempt + 1)
                return
            except Exception as e:
                logger.warning("[rfq_ws] reconnect attempt %d failed: %s", attempt + 1, e)
                attempt += 1

    async def _cancel_tasks(self) -> None:
        for task in (self._receive_task, self._heartbeat_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._receive_task = None
        self._heartbeat_task = None

    async def _close_ws(self) -> None:
        try:
            if self._ws and not self._ws.closed:
                await self._ws.close()
        except Exception:
            pass
        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception:
            pass
        self._ws = None
        self._session = None
