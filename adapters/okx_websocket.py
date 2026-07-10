"""
OKX WebSocket adapter for real-time price streaming.

Based on OKX API v5 WebSocket documentation:
- Public endpoint: wss://ws.okx.com:8443/ws/v5/public
- Demo endpoint: wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999
- Ticker updates: every 100ms on price change, otherwise every second
- Heartbeat: ping/pong required within 30 seconds
"""

import json
import asyncio
import logging
from datetime import datetime
from typing import Optional, Callable, Dict, Any, List, Set
import aiohttp

from models import MarketTick

logger = logging.getLogger(__name__)


class OKXWebSocket:
    """
    OKX WebSocket client for real-time ticker streaming.

    Subscribes to the tickers channel for spot and perpetual instruments,
    providing real-time bid/ask/last prices with ~100ms latency.
    """

    # WebSocket endpoints
    PUBLIC_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
    DEMO_WS_URL = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"

    # Heartbeat interval (seconds) - must be < 30s
    HEARTBEAT_INTERVAL = 25

    def __init__(self, is_demo: bool = False):
        """
        Initialize OKX WebSocket client.

        Args:
            is_demo: If True, connect to OKX demo server endpoint.
        """
        self.is_demo = is_demo
        self.ws_url = self.DEMO_WS_URL if is_demo else self.PUBLIC_WS_URL

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        self._connected = False

        # Subscribed instruments
        self._subscribed: Set[str] = set()

        # Latest ticks by instrument
        self._ticks: Dict[str, MarketTick] = {}

        # Callbacks
        self.on_tick: Optional[Callable[[str, MarketTick], None]] = None
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[str], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

        # Tasks
        self._receive_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._connected and self._ws is not None and not self._ws.closed

    async def connect(self) -> bool:
        """
        Establish WebSocket connection.

        Returns:
            True if connection successful.
        """
        if self._connected:
            logger.warning("Already connected")
            return True

        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                self.ws_url,
                heartbeat=self.HEARTBEAT_INTERVAL,
                receive_timeout=35,
            )

            self._running = True
            self._connected = True

            # Start receive loop
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Start heartbeat
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            logger.info("Connected to OKX WebSocket (demo=%s)", self.is_demo)

            if self.on_connected:
                self.on_connected()

            return True

        except Exception as e:
            error_msg = f"Failed to connect to OKX WebSocket: {e}"
            logger.exception(error_msg)
            if self.on_error:
                self.on_error(error_msg)
            return False

    async def disconnect(self) -> None:
        """Close WebSocket connection."""
        self._running = False
        self._connected = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._ws and not self._ws.closed:
            await self._ws.close()

        if self._session:
            await self._session.close()

        self._subscribed.clear()
        logger.info("Disconnected from OKX WebSocket")

        if self.on_disconnected:
            self.on_disconnected("Disconnected")

    async def subscribe(self, instruments: List[str]) -> bool:
        """
        Subscribe to ticker updates for instruments.

        Args:
            instruments: List of instrument IDs (e.g., ["BTC-USDT", "BTC-USDT-SWAP"])

        Returns:
            True if subscription request sent successfully.
        """
        if not self.connected:
            logger.error("Cannot subscribe: not connected")
            return False

        # Build subscription message
        args = [{"channel": "tickers", "instId": inst} for inst in instruments]
        message = {
            "op": "subscribe",
            "args": args
        }

        try:
            await self._ws.send_json(message)
            logger.debug("Subscribed to tickers: %s", instruments)
            return True
        except Exception as e:
            logger.error("Failed to subscribe: %s", e)
            return False

    async def unsubscribe(self, instruments: List[str]) -> bool:
        """
        Unsubscribe from ticker updates.

        Args:
            instruments: List of instrument IDs to unsubscribe.

        Returns:
            True if unsubscription request sent successfully.
        """
        if not self.connected:
            return False

        args = [{"channel": "tickers", "instId": inst} for inst in instruments]
        message = {
            "op": "unsubscribe",
            "args": args
        }

        try:
            await self._ws.send_json(message)
            for inst in instruments:
                self._subscribed.discard(inst)
            logger.debug("Unsubscribed from tickers: %s", instruments)
            return True
        except Exception as e:
            logger.error("Failed to unsubscribe: %s", e)
            return False

    def get_tick(self, instrument: str) -> Optional[MarketTick]:
        """
        Get the latest tick for an instrument.

        Args:
            instrument: Instrument ID.

        Returns:
            Latest MarketTick or None if not available.
        """
        return self._ticks.get(instrument)

    async def _receive_loop(self) -> None:
        """Main loop for receiving WebSocket messages."""
        logger.debug("WebSocket receive loop started")

        while self._running and self._ws and not self._ws.closed:
            try:
                msg = await self._ws.receive(timeout=35)

                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)

                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.warning("WebSocket closed by server")
                    break

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", self._ws.exception())
                    break

            except asyncio.TimeoutError:
                # 35 s with no data on a ticker stream that normally pushes
                # several times a second means the socket is silently dead (TCP
                # up, no data — routine for a WS behind a load balancer). BREAK
                # to trigger reconnect + resubscribe. A bare `continue` here
                # re-armed receive() on the SAME dead socket forever, so ticks
                # never resumed, the tick-driven heartbeat went stale, and the
                # watchdog restarted the whole process every ~30 min. The
                # execution adapter (okx_ws_adapter) already breaks here.
                logger.warning("WebSocket receive timeout (35s) — connection stale, reconnecting")
                break

            except asyncio.CancelledError:
                break

            except Exception as e:
                logger.exception("Error in receive loop: %s", e)
                break

        logger.debug("WebSocket receive loop ended")
        self._connected = False

        # Attempt reconnect
        if self._running:
            asyncio.create_task(self._reconnect())

    async def _handle_message(self, data: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            # Handle pong response
            if data == "pong":
                return

            message = json.loads(data)

            # Handle subscription confirmations
            if "event" in message:
                event = message["event"]
                if event == "subscribe":
                    inst_id = message.get("arg", {}).get("instId")
                    if inst_id:
                        self._subscribed.add(inst_id)
                        logger.debug("Subscription confirmed: %s", inst_id)
                elif event == "error":
                    error_code = message.get("code", "")
                    error_msg = message.get("msg", "Unknown error")
                    logger.error("OKX WebSocket error: %s - %s", error_code, error_msg)
                    if self.on_error:
                        self.on_error(f"OKX error {error_code}: {error_msg}")
                return

            # Handle ticker data
            if "data" in message and "arg" in message:
                channel = message["arg"].get("channel")
                if channel == "tickers":
                    for ticker_data in message["data"]:
                        self._process_ticker(ticker_data)

        except json.JSONDecodeError as e:
            logger.warning("Invalid JSON received: %s", e)
        except Exception as e:
            logger.exception("Error handling message: %s", e)

    def _process_ticker(self, data: Dict[str, Any]) -> None:
        """Process ticker data and update internal state."""
        try:
            inst_id = data.get("instId", "")
            if not inst_id:
                return

            # Parse ticker data
            tick = MarketTick(
                symbol=inst_id,
                bid=float(data.get("bidPx", 0) or 0),
                ask=float(data.get("askPx", 0) or 0),
                last=float(data.get("last", 0) or 0),
                volume_24h=float(data.get("vol24h", 0) or 0),
                timestamp=datetime.utcnow(),
            )

            # Update internal state
            self._ticks[inst_id] = tick

            # Notify callback
            if self.on_tick:
                self.on_tick(inst_id, tick)

        except Exception as e:
            logger.error("Error processing ticker: %s", e)

    async def _heartbeat_loop(self) -> None:
        """Send periodic ping to keep connection alive."""
        while self._running and self.connected:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                if self.connected:
                    await self._ws.send_str("ping")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Heartbeat error: %s", e)

    async def _reconnect(self) -> None:
        """Attempt to reconnect after disconnection (max 15 attempts)."""
        retry_delay = 1
        max_delay = 30
        max_attempts = 15
        attempt = 0

        while self._running and not self._connected and attempt < max_attempts:
            attempt += 1
            logger.info("WebSocket reconnect attempt %d/%d in %ds...", attempt, max_attempts, retry_delay)
            await asyncio.sleep(retry_delay)

            if await self.connect():
                # Discard stale cached ticks from before the disconnect so the
                # engine never acts on prices that could be seconds/minutes old.
                self._ticks.clear()
                # Resubscribe to previously subscribed instruments
                if self._subscribed:
                    await self.subscribe(list(self._subscribed))
                return

            # Exponential backoff
            retry_delay = min(retry_delay * 2, max_delay)

        if attempt >= max_attempts:
            logger.error(
                "WebSocket failed to reconnect after %d attempts — giving up. "
                "Engine will use REST polling or stale prices until restart.",
                max_attempts,
            )
            if self.on_error:
                self.on_error(f"WebSocket permanently disconnected after {max_attempts} reconnect attempts")


class OKXWebSocketManager:
    """
    Manages OKX WebSocket connections for multiple instruments.

    Provides a simple interface for the trading engine to get
    real-time ticks without managing WebSocket complexity.
    """

    def __init__(self, is_demo: bool = False):
        self.ws = OKXWebSocket(is_demo=is_demo)
        self._tick_callbacks: List[Callable[[str, MarketTick], None]] = []

    async def start(self, spot_symbol: str, futures_symbol: str) -> bool:
        """
        Start WebSocket and subscribe to symbols.

        Args:
            spot_symbol: Spot instrument (e.g., "BTC-USDT")
            futures_symbol: Futures instrument (e.g., "BTC-USDT-SWAP")

        Returns:
            True if started successfully.
        """
        # Set up tick callback
        self.ws.on_tick = self._on_tick

        # Connect
        if not await self.ws.connect():
            return False

        # Subscribe to both symbols
        await self.ws.subscribe([spot_symbol, futures_symbol])
        return True

    async def stop(self) -> None:
        """Stop WebSocket connection."""
        await self.ws.disconnect()

    def get_spot_tick(self, symbol: str) -> Optional[MarketTick]:
        """Get latest spot tick."""
        return self.ws.get_tick(symbol)

    def get_futures_tick(self, symbol: str) -> Optional[MarketTick]:
        """Get latest futures tick."""
        return self.ws.get_tick(symbol)

    def add_tick_callback(self, callback: Callable[[str, MarketTick], None]) -> None:
        """Add callback for tick updates."""
        self._tick_callbacks.append(callback)

    def _on_tick(self, symbol: str, tick: MarketTick) -> None:
        """Internal tick handler."""
        for callback in self._tick_callbacks:
            try:
                callback(symbol, tick)
            except Exception as e:
                logger.error("Error in tick callback: %s", e)
