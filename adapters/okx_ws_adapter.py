"""
OKX WebSocket adapter — private channel (authenticated).

Milestone 1: Infrastructure and state cache only.
Order placement, amendment, and cancellation via WS are Milestone 2/3.
All trade-mutating operations delegate to the embedded REST adapter until then.

Private endpoint (testnet):
    wss://wspap.okx.com:8443/ws/v5/private  (header: x-simulated-trading: 1)

Private endpoint (live):
    wss://ws.okx.com:8443/ws/v5/private
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from adapters.base import ExchangeAdapter, is_derivative
from adapters.okx_adapter import OKXAdapter
from models import AccountInfo, MarketTick, OrderResult, Position

logger = logging.getLogger(__name__)

_TESTNET_PRIVATE_URL = "wss://wspap.okx.com:8443/ws/v5/private"
_LIVE_PRIVATE_URL = "wss://ws.okx.com:8443/ws/v5/private"

# Exponential backoff delays (seconds) for reconnect; last value is the cap.
_BACKOFF = [1, 2, 4, 8, 16, 30]


def _new_cid() -> str:
    """Return an 8-hex-char correlation ID for log tracing."""
    return uuid.uuid4().hex[:8]


def _to_float(val: Any) -> float:
    """Convert a value to float, treating None / empty-string / falsy as 0."""
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _normalise_order(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an OKX orders-channel push dict into the same shape returned by
    OKXAdapter.get_order_status() so the executor can consume either source.

    filled_price prefers accFillSz/fillPx (last chunk), falling back to avgPx
    (cumulative average) when fillPx is zero — which OKX sends for live orders
    and occasionally for market orders before the fill record propagates.
    """
    sz = _to_float(raw.get("sz"))
    fill_sz = _to_float(raw.get("accFillSz")) or _to_float(raw.get("fillSz"))
    fill_px_v = _to_float(raw.get("fillPx"))
    avg_px_v = _to_float(raw.get("avgPx"))
    # Prefer fillPx when non-zero; fall back to avgPx for market fills where
    # OKX may send fillPx="0" before the last-chunk record arrives.
    fill_px = fill_px_v if fill_px_v > 0 else avg_px_v
    return {
        "order_id": raw.get("ordId", ""),
        "symbol": raw.get("instId", ""),
        "state": raw.get("state", ""),
        "quantity": sz,
        "filled_qty": fill_sz,
        "filled_price": fill_px,
        "remaining_qty": sz - fill_sz,
        "side": raw.get("side", ""),
        "order_type": raw.get("ordType", ""),
        "cancel_source": raw.get("cancelSource", ""),
        "cancel_source_reason": raw.get("cancelSourceReason", ""),
    }


class OKXWebSocketAdapter(ExchangeAdapter):
    """
    OKX private-channel WebSocket adapter.

    Responsibilities (Milestone 1):
    - Maintain an authenticated WebSocket connection to the OKX private channel.
    - Route order / position / account push events to registered callbacks.
    - Keep an in-memory cache of orders, positions, and account state.
    - Auto-reconnect with exponential backoff; re-login and re-subscribe after reconnect.
    - Send raw "ping" heartbeat every 25 s; log received "pong".
    - Log every WS frame sent and received at DEBUG with a correlation ID.

    All trade-mutating operations (place_order, cancel_order, close_position,
    set_leverage, etc.) and market-data operations (get_tick, get_orderbook)
    delegate to an embedded OKXAdapter (REST) until Milestone 2/3.
    """

    HEARTBEAT_INTERVAL = 25  # seconds; OKX requires ping within 30 s

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str = "",
        is_testnet: bool = True,
    ) -> None:
        super().__init__(api_key, secret_key, passphrase, is_testnet)

        # Embedded REST adapter handles market-data and trade ops until WS migration
        self._rest = OKXAdapter(
            api_key=api_key,
            secret_key=secret_key,
            passphrase=passphrase,
            is_testnet=is_testnet,
        )

        # WS transport
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running: bool = False
        self._logged_in: bool = False
        self._login_event: Optional[asyncio.Event] = None

        # In-memory caches
        # _order_cache:    ord_id -> normalised order dict
        # _position_cache: "{instId}:{posSide}" -> raw push dict
        # _account_cache:  flat dict merged from account push items
        self._order_cache: Dict[str, Dict[str, Any]] = {}
        self._position_cache: Dict[str, Dict[str, Any]] = {}
        self._account_cache: Dict[str, Any] = {}

        # Caller-registered callbacks
        self.on_order_update: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_position_update: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_account_update: Optional[Callable[[Dict[str, Any]], None]] = None

        # Background tasks
        self._receive_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # ExchangeAdapter: connect / disconnect
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """
        Connect REST adapter (credentials check), then open the private WS,
        login, and subscribe to orders / positions / account channels.
        """
        if self._connected:
            return True

        # REST pre-flight validates credentials without an extra round-trip later
        if not await self._rest.connect():
            self._set_error(f"REST pre-flight failed: {self._rest.last_error}")
            return False

        self._running = True
        try:
            await self._ws_connect()
            return True
        except Exception as e:
            self._running = False
            self._set_error(str(e))
            logger.exception("[ws_adapter] connect failed: %s", e)
            return False

    async def disconnect(self) -> None:
        """Shut down the WS connection and the embedded REST adapter."""
        self._running = False
        self._connected = False
        self._logged_in = False

        await self._cancel_tasks()
        await self._close_ws()
        await self._rest.disconnect()
        logger.info("[ws_adapter] disconnected")

    # ------------------------------------------------------------------
    # ExchangeAdapter: market data — delegated to REST
    # ------------------------------------------------------------------

    async def get_tick(self, symbol: str) -> Optional[MarketTick]:
        return await self._rest.get_tick(symbol)

    async def get_orderbook(
        self, symbol: str, depth: int = 5
    ) -> Optional[Dict[str, Any]]:
        return await self._rest.get_orderbook(symbol, depth)

    async def get_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        return await self._rest.get_funding_rate(symbol)

    async def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        return await self._rest.get_symbol_info(symbol)

    async def get_instruments(self, inst_type: str = "SPOT") -> List[Dict[str, Any]]:
        return await self._rest.get_instruments(inst_type)

    # ------------------------------------------------------------------
    # ExchangeAdapter: order placement — delegated to REST (Milestone 2)
    # ------------------------------------------------------------------

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        reduce_only: bool = False,
        pos_side: Optional[str] = None,
        notional_usdt: Optional[float] = None,
        force_td_mode: Optional[str] = None,
    ) -> OrderResult:
        """Delegate to REST — WS order placement is Milestone 2."""
        return await self._rest.place_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            reduce_only=reduce_only,
            pos_side=pos_side,
            notional_usdt=notional_usdt,
            force_td_mode=force_td_mode,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Delegate to REST — WS cancel is Milestone 3."""
        return await self._rest.cancel_order(symbol, order_id)

    async def close_position(self, symbol: str) -> OrderResult:
        return await self._rest.close_position(symbol)

    # ------------------------------------------------------------------
    # ExchangeAdapter: positions — WS cache first, REST fallback
    # ------------------------------------------------------------------

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """Return positions from the WS cache; fall back to REST if cache is empty."""
        if self._position_cache:
            positions: List[Position] = []
            for raw in self._position_cache.values():
                try:
                    pos = self._parse_position(raw)
                    if pos is not None:
                        positions.append(pos)
                except Exception as e:
                    logger.debug("[ws_adapter] position parse error: %s", e)
            if symbol:
                positions = [p for p in positions if p.symbol == symbol]
            return positions

        return await self._rest.get_positions(symbol)

    # ------------------------------------------------------------------
    # ExchangeAdapter: account — WS cache first, REST fallback
    # ------------------------------------------------------------------

    async def get_account_info(self) -> Optional[AccountInfo]:
        """Return account info from the WS cache; fall back to REST if cache is empty."""
        if self._account_cache:
            try:
                return self._parse_account(self._account_cache)
            except Exception as e:
                logger.debug("[ws_adapter] account parse error: %s", e)
        return await self._rest.get_account_info()

    # ------------------------------------------------------------------
    # OKXAdapter-parity methods (used by executor and app.py)
    # All delegate to REST in Milestone 1.
    # ------------------------------------------------------------------

    async def get_order_status(
        self, symbol: str, order_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return normalised order status from WS cache, falling back to REST."""
        cached = self._order_cache.get(order_id)
        if cached:
            return cached
        return await self._rest.get_order_status(symbol, order_id)

    async def get_pending_orders(
        self,
        symbol: Optional[str] = None,
        inst_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return await self._rest.get_pending_orders(symbol, inst_type)

    async def cancel_all_orders(
        self,
        symbol: Optional[str] = None,
        inst_type: Optional[str] = None,
    ) -> int:
        return await self._rest.cancel_all_orders(symbol, inst_type)

    async def get_account_config(self) -> Optional[Dict[str, Any]]:
        return await self._rest.get_account_config()

    async def set_leverage(
        self, symbol: str, leverage: int, margin_mode: str = "cross"
    ) -> bool:
        return await self._rest.set_leverage(symbol, leverage, margin_mode)

    async def get_leverage(self, symbol: str) -> Optional[int]:
        return await self._rest.get_leverage(symbol)

    async def get_leverage_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        return await self._rest.get_leverage_info(symbol)

    async def get_order_history(
        self, symbol: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        return await self._rest.get_order_history(symbol, limit)

    async def get_position_margin_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        return await self._rest.get_position_margin_info(symbol)

    async def get_spot_balances(
        self, include_frozen: bool = True
    ) -> Dict[str, Dict[str, float]]:
        return await self._rest.get_spot_balances(include_frozen)

    async def get_trading_balances_detailed(self) -> List[Dict[str, Any]]:
        return await self._rest.get_trading_balances_detailed()

    async def get_funding_balances(
        self, include_zero: bool = False
    ) -> List[Dict[str, Any]]:
        return await self._rest.get_funding_balances(include_zero)

    async def get_asset_valuation(self, ccy: str = "USDT") -> Optional[float]:
        return await self._rest.get_asset_valuation(ccy)

    async def get_asset_balance(self, currency: str) -> Dict[str, float]:
        return await self._rest.get_asset_balance(currency)

    async def sell_spot_to_usdt(
        self, currency: str, quantity: float = None
    ) -> OrderResult:
        return await self._rest.sell_spot_to_usdt(currency, quantity)

    # ------------------------------------------------------------------
    # Private WS infrastructure
    # ------------------------------------------------------------------

    async def _ws_connect(self) -> None:
        """
        Low-level: open TCP, send login, wait for ack, subscribe channels.
        Called both on initial connect and on every reconnect attempt.
        """
        cid = _new_cid()
        url = _TESTNET_PRIVATE_URL if self.is_testnet else _LIVE_PRIVATE_URL
        headers = {"x-simulated-trading": "1"} if self.is_testnet else {}

        logger.debug("[%s] WS connecting to %s", cid, url)
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            url, headers=headers, heartbeat=None
        )
        logger.info("[%s] WS TCP connected", cid)

        # Create event before starting the receive loop so the loop can set it
        self._login_event = asyncio.Event()
        self._logged_in = False

        # Start background loops
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Send login frame
        await self._send_login(cid)

        # Block until login is confirmed or rejected (10 s timeout)
        try:
            await asyncio.wait_for(self._login_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            raise RuntimeError("OKX WS login timed out (10 s)")

        if not self._logged_in:
            raise RuntimeError("OKX WS login rejected by exchange")

        # Subscribe to private channels
        await self._subscribe_all()
        self._connected = True
        logger.info("[ws_adapter] ready — connected, authenticated, subscribed")

    async def _send_login(self, cid: str) -> None:
        """Build and send the WS login frame per OKX docs."""
        ts = str(int(time.time()))
        sign_payload = ts + "GET" + "/users/self/verify"
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            sign_payload.encode("utf-8"),
            hashlib.sha256,
        )
        sign = base64.b64encode(mac.digest()).decode()

        frame = {
            "op": "login",
            "args": [{
                "apiKey": self.api_key,
                "passphrase": self.passphrase,
                "timestamp": ts,
                "sign": sign,
            }],
        }
        await self._send_frame(frame, cid)

    async def _subscribe_all(self) -> None:
        """Subscribe to orders (ANY), positions (ANY), and account channels."""
        cid = _new_cid()
        frame = {
            "op": "subscribe",
            "args": [
                {"channel": "orders",    "instType": "ANY"},
                {"channel": "positions", "instType": "ANY"},
                {"channel": "account"},
            ],
        }
        await self._send_frame(frame, cid)

    async def _send_frame(
        self, frame: Dict[str, Any], cid: Optional[str] = None
    ) -> None:
        """Serialise a dict to JSON, log it at DEBUG with a correlation ID, and send."""
        if cid is None:
            cid = _new_cid()
        raw = json.dumps(frame)
        logger.debug("[%s] WS SEND: %s", cid, raw)
        await self._ws.send_str(raw)

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Read frames from the WS and dispatch them. Triggers reconnect on exit."""
        logger.debug("[recv_loop] started")
        while self._running and self._ws and not self._ws.closed:
            try:
                msg = await self._ws.receive(timeout=35)

                if msg.type == aiohttp.WSMsgType.TEXT:
                    cid = _new_cid()
                    logger.debug("[%s] WS RECV: %s", cid, msg.data[:2000])
                    await self._dispatch(msg.data, cid)

                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.warning("[recv_loop] server closed the connection")
                    break

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("[recv_loop] WS error: %s", self._ws.exception())
                    break

            except asyncio.TimeoutError:
                # 35 s with no data — connection is likely stale
                logger.warning("[recv_loop] receive timeout (35 s), reconnecting")
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("[recv_loop] unexpected error: %s", e)
                break

        logger.debug("[recv_loop] ended")
        self._connected = False
        self._logged_in = False

        if self._running:
            asyncio.create_task(self._reconnect_loop())

    async def _dispatch(self, raw: str, cid: str) -> None:
        """Parse and route a single incoming message."""
        # Raw heartbeat reply
        if raw == "pong":
            logger.debug("[%s] pong received", cid)
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("[%s] invalid JSON from server: %s", cid, e)
            return

        event = msg.get("event")

        # --- control events ---
        if event == "login":
            if msg.get("code") == "0":
                self._logged_in = True
                logger.info("[%s] WS login confirmed", cid)
            else:
                logger.error(
                    "[%s] WS login failed: code=%s msg=%s",
                    cid, msg.get("code"), msg.get("msg"),
                )
            if self._login_event:
                self._login_event.set()
            return

        if event in ("subscribe", "unsubscribe"):
            logger.debug("[%s] subscription ack: %s", cid, msg.get("arg"))
            return

        if event == "error":
            logger.error(
                "[%s] WS error event: code=%s msg=%s",
                cid, msg.get("code"), msg.get("msg"),
            )
            return

        # --- push data ---
        channel = (msg.get("arg") or {}).get("channel", "")
        data_list = msg.get("data") or []

        if channel == "orders":
            for item in data_list:
                oid = item.get("ordId", "")
                if not oid:
                    continue
                normalised = _normalise_order(item)
                self._order_cache[oid] = normalised
                logger.debug(
                    "[%s] order cache update: ordId=%s state=%s",
                    cid, oid, normalised["state"],
                )
                if self.on_order_update:
                    try:
                        self.on_order_update(normalised)
                    except Exception as e:
                        logger.error("[%s] on_order_update callback error: %s", cid, e)

        elif channel == "positions":
            for item in data_list:
                key = f"{item.get('instId')}:{item.get('posSide', 'net')}"
                self._position_cache[key] = item
                logger.debug("[%s] position cache update: key=%s", cid, key)
                if self.on_position_update:
                    try:
                        self.on_position_update(item)
                    except Exception as e:
                        logger.error("[%s] on_position_update callback error: %s", cid, e)

        elif channel == "account":
            for item in data_list:
                self._account_cache.update(item)
                logger.debug("[%s] account cache updated", cid)
                if self.on_account_update:
                    try:
                        self.on_account_update(item)
                    except Exception as e:
                        logger.error("[%s] on_account_update callback error: %s", cid, e)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Send raw 'ping' every HEARTBEAT_INTERVAL seconds."""
        while self._running and self._ws and not self._ws.closed:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                cid = _new_cid()
                logger.debug("[%s] WS SEND: ping", cid)
                await self._ws.send_str("ping")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[heartbeat] send error: %s", e)
                break

    # ------------------------------------------------------------------
    # Reconnect with exponential backoff
    # ------------------------------------------------------------------

    async def _reconnect_loop(self) -> None:
        """
        After an unexpected disconnect: wait with exponential backoff, then
        call _ws_connect() which re-logs in and re-subscribes automatically.
        """
        attempt = 0
        while self._running and not self._connected:
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            logger.info(
                "[reconnect] attempt %d — waiting %d s before retry",
                attempt + 1, delay,
            )
            await asyncio.sleep(delay)

            await self._cancel_tasks()
            await self._close_ws()

            try:
                await self._ws_connect()
                logger.info("[reconnect] reconnected after %d attempt(s)", attempt + 1)
                return
            except Exception as e:
                logger.warning("[reconnect] attempt %d failed: %s", attempt + 1, e)
                attempt += 1

        if not self._connected:
            logger.error("[reconnect] gave up after %d attempts", attempt)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _cancel_tasks(self) -> None:
        """Cancel background receive/heartbeat tasks and wait for them to stop."""
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
        """Close the WS connection and HTTP session."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session:
            await self._session.close()
        self._session = None

    def _parse_position(self, data: Dict[str, Any]) -> Optional[Position]:
        """Convert a raw WS positions-channel push item into a Position object."""
        pos_raw = float(data.get("pos", 0) or 0)
        if pos_raw == 0:
            return None
        pos_side_field = (data.get("posSide", "") or "").lower()
        if pos_side_field in ("long", "short"):
            side = pos_side_field.upper()
        else:
            side = "LONG" if pos_raw > 0 else "SHORT"
        qty = abs(pos_raw)
        if qty == 0:
            return None
        return Position(
            symbol=data.get("instId", ""),
            side=side,
            quantity=qty,
            entry_price=float(data.get("avgPx", 0) or 0),
            unrealized_pnl=float(data.get("upl", 0) or 0),
            leverage=float(data.get("lever", 1) or 1),
        )

    def _parse_account(self, data: Dict[str, Any]) -> AccountInfo:
        """Convert the cached WS account state dict into an AccountInfo object."""
        total_eq = float(data.get("totalEq", 0) or 0)
        avail_eq = float(data.get("availEq", 0) or 0)
        imr = float(data.get("imr", 0) or 0)
        mmr = float(data.get("mmr", 0) or 0)
        upl = float(data.get("upl", 0) or 0)
        margin_ratio = (total_eq / mmr * 100) if mmr > 0 else 0.0
        return AccountInfo(
            exchange="OKX",
            balance_usd=total_eq,
            available_balance_usd=avail_eq,
            margin_used=imr,
            unrealized_pnl=upl,
            total_equity=total_eq,
            initial_margin=imr,
            maintenance_margin=mmr,
            margin_ratio=margin_ratio,
            available_margin=avail_eq,
            leverage_used=imr / total_eq if total_eq > 0 else 0,
        )
