"""
OKX Exchange adapter implementation.
"""

import asyncio
import hmac
import hashlib
import base64
import json
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
import aiohttp

from .base import ExchangeAdapter, is_derivative
from models import MarketTick, OrderResult, Position, AccountInfo

logger = logging.getLogger(__name__)


def _detect_inst_type(symbol: str) -> str:
    """Classify an OKX instId by its segment shape.

    - "BTC-USDT"            -> SPOT      (1 dash)
    - "BTC-USDT-SWAP" /
      "BTC-USD-SWAP"        -> SWAP      (suffix)
    - "BTC-USDT-250628" /
      "BTC-USD-250628"      -> FUTURES   (3 segments, no -SWAP suffix)
    """
    if not symbol:
        return "SPOT"
    if symbol.endswith("-SWAP"):
        return "SWAP"
    if symbol.count("-") >= 2:
        return "FUTURES"
    return "SPOT"


class OKXAdapter(ExchangeAdapter):
    """
    OKX exchange adapter supporting spot and perpetual swaps.

    Symbol format:
    - Spot: BTC-USDT, ETH-USDT
    - Perpetual: BTC-USDT-SWAP, ETH-USDT-SWAP
    """

    # API endpoints
    BASE_URL = "https://www.okx.com"
    DEMO_URL = "https://www.okx.com"  # Same URL, different header

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str = "",
        is_testnet: bool = False,
        spot_leverage: int = 1,
    ):
        super().__init__(api_key, secret_key, passphrase, is_testnet)
        self._session: Optional[aiohttp.ClientSession] = None
        self.base_url = self.BASE_URL
        # cross mode required for leveraged spot accounts; cash for simple 1x spot
        self._spot_td_mode = "cross" if spot_leverage > 1 else "cash"
        # Position cache — avoids hammering /account/positions from multiple callers
        self._positions_cache: Optional[List[Position]] = None
        self._positions_cache_ts: float = 0.0
        self._POSITIONS_TTL: float = 5.0  # seconds

    async def connect(self) -> bool:
        """Establish connection to OKX."""
        try:
            self._session = aiohttp.ClientSession()

            # Test connection with account info
            result = await self._request("GET", "/api/v5/account/balance")

            if result and "data" in result:
                self._connected = True
                self._clear_error()
                logger.info("Connected to OKX (demo=%s)", self.is_testnet)
                return True
            else:
                error = result.get("msg", "Unknown error") if result else "No response"
                self._set_error(f"OKX connection failed: {error}")
                return False

        except Exception as e:
            self._set_error(f"OKX connection error: {str(e)}")
            logger.exception("OKX connection error")
            return False

    async def disconnect(self) -> None:
        """Disconnect from OKX."""
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False
        logger.info("Disconnected from OKX")

    def _get_timestamp(self) -> str:
        """Get ISO timestamp for signing."""
        now = datetime.utcnow()
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + now.strftime("%f")[:3] + "Z"

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """Generate signature for request."""
        message = timestamp + method + path + body
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode()

    def _get_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Generate request headers."""
        timestamp = self._get_timestamp()
        signature = self._sign(timestamp, method, path, body)

        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

        # Demo trading header
        if self.is_testnet:
            headers["x-simulated-trading"] = "1"

        return headers

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        quiet_codes: Optional[set] = None,
    ) -> Optional[Dict]:
        """Make API request with automatic 429 retry (up to 3 attempts).

        quiet_codes: OKX error codes (strings) to log at DEBUG instead of WARNING.
        Use for expected non-fatal failures like 51001 (instrument not found) when
        probing for optional instrument data.
        """
        if not self._session:
            self._session = aiohttp.ClientSession()

        url = self.base_url + path
        body = json.dumps(data) if data else ""

        if params:
            path = path + "?" + "&".join(f"{k}={v}" for k, v in params.items())
            url = self.base_url + path

        headers = self._get_headers(method, path, body)

        for attempt in range(3):
            try:
                async with self._session.request(
                    method, url, headers=headers, data=body if data else None
                ) as response:
                    # Retry on HTTP 429 (rate limit)
                    if response.status == 429:
                        retry_after = float(response.headers.get("Retry-After", 1))
                        wait = max(retry_after, 2 ** attempt)
                        logger.warning("OKX rate limit (429) on %s %s — retrying in %.1fs", method, path, wait)
                        await asyncio.sleep(wait)
                        continue

                    result = await response.json()

                    # OKX also returns rate-limit errors inside JSON (code=50011 / 50013)
                    if result.get("code") in ("50011", "50013") and attempt < 2:
                        wait = 2 ** (attempt + 1)
                        logger.warning("OKX JSON rate limit code=%s — retrying in %.1fs", result.get("code"), wait)
                        await asyncio.sleep(wait)
                        continue

                    if result.get("code") != "0":
                        error = result.get("msg", "Unknown error")
                        data_arr = result.get("data", [])
                        if data_arr and isinstance(data_arr, list) and len(data_arr) > 0:
                            sub_code = data_arr[0].get("sCode", "")
                            sub_msg = data_arr[0].get("sMsg", "")
                            if sub_code or sub_msg:
                                error = f"{error} (sCode={sub_code}: {sub_msg})"
                        if quiet_codes and result.get("code") in quiet_codes:
                            logger.debug("OKX API [%s %s] code=%s (expected): %s",
                                         method, path, result.get("code"), error)
                        else:
                            logger.warning("OKX API error [%s %s]: %s | Full response: %s",
                                           method, path, error, result)
                        self._set_error(error)

                    return result

            except Exception as e:
                logger.exception("OKX request error: %s %s", method, path)
                self._set_error(str(e))
                return None

        logger.error("OKX request %s %s exhausted retries", method, path)
        return None

    async def get_tick(self, symbol: str) -> Optional[MarketTick]:
        """Get current market tick."""
        try:
            result = await self._request(
                "GET", "/api/v5/market/ticker", params={"instId": symbol}
            )

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                return MarketTick(
                    symbol=symbol,
                    bid=float(data.get("bidPx", 0)),
                    ask=float(data.get("askPx", 0)),
                    last=float(data.get("last", 0)),
                    volume_24h=float(data.get("vol24h", 0)),
                    timestamp=datetime.utcnow(),
                )
            else:
                # Log API error if result exists but code is not "0"
                if result:
                    error_msg = result.get("msg", "Unknown")
                    error_code = result.get("code", "?")
                    logger.warning("OKX ticker API error for %s: code=%s, msg=%s",
                                  symbol, error_code, error_msg)
                else:
                    logger.warning("OKX ticker API returned None for %s", symbol)

        except Exception as e:
            logger.error("Error fetching OKX tick for %s: %s", symbol, e)

        return None

    async def get_orderbook(
        self, symbol: str, depth: int = 5
    ) -> Optional[Dict[str, Any]]:
        """Get order book."""
        try:
            result = await self._request(
                "GET",
                "/api/v5/market/books",
                params={"instId": symbol, "sz": str(depth)},
            )

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                return {
                    "bids": [[float(b[0]), float(b[1])] for b in data.get("bids", [])],
                    "asks": [[float(a[0]), float(a[1])] for a in data.get("asks", [])],
                    "timestamp": datetime.utcnow(),
                }

        except Exception as e:
            logger.error("Error fetching OKX orderbook: %s", e)

        return None

    async def _prepare_order(
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
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], float]:
        """
        Validate inputs and build the OKX order parameter dict without making an HTTP call.

        Returns (order_data, error_msg, effective_sz):
          - Success: (order_data_dict, None, sz)
          - Failure: (None, error_string, 0.0)
        """
        inst_type = _detect_inst_type(symbol)
        td_mode = "cross" if inst_type in ("SWAP", "FUTURES") else self._spot_td_mode
        if force_td_mode:
            td_mode = force_td_mode

        symbol_info = await self.get_symbol_info(symbol)
        sz = quantity
        sz_str = ""

        if inst_type in ("SWAP", "FUTURES"):
            if symbol_info:
                ct_type = symbol_info.get("ct_type", "linear")
                if ct_type == "inverse":
                    # Inverse (coin-margined): ctVal is in USD, quantity is in base coin.
                    # e.g. BTC-USD-SWAP ctVal=100 USD, qty=0.01 BTC at $100k → 10 contracts.
                    # ct_val_usd = raw OKX ctVal; contract_val is already normalised to base coin.
                    ct_val_usd = symbol_info.get("ct_val_usd")
                    if not ct_val_usd or ct_val_usd <= 0:
                        return None, (f"Symbol info for inverse {symbol} missing ct_val_usd "
                                      f"(adapter mismatch)"), 0.0
                    effective_price = price
                    if not effective_price or effective_price <= 0:
                        # Market order with no explicit price — fetch live mid for sizing
                        tick = await self.get_tick(symbol)
                        if tick and tick.bid > 0 and tick.ask > 0:
                            effective_price = (tick.bid + tick.ask) / 2.0
                        elif tick and tick.last > 0:
                            effective_price = tick.last
                    if not effective_price or effective_price <= 0:
                        return None, (f"Cannot size inverse contract {symbol}: "
                                      f"no price available"), 0.0
                    contracts = (quantity * effective_price) / ct_val_usd
                    ct_val_log = ct_val_usd
                else:
                    # Linear (USDT-margined): ctVal is in base coin
                    ct_val = float(symbol_info.get("contract_val") or 0.01)
                    if ct_val <= 0:
                        return None, f"Invalid linear ctVal {ct_val}", 0.0
                    effective_price = price or 0
                    contracts = quantity / ct_val
                    ct_val_log = ct_val
                sz = int(contracts)
                if sz < 1:
                    logger.error("%s %s quantity %.6f price %.2f = %.4f contracts (ctVal=%.4f), minimum is 1",
                                inst_type, ct_type, quantity, effective_price if ct_type == "inverse" else price or 0,
                                contracts, ct_val_log)
                    return None, (f"Quantity {quantity} too small for {symbol}: "
                                  f"need at least 1 contract"), 0.0
                logger.info("%s %s order: %.6f %s @ %.2f = %d contracts (ctVal=%.4f)",
                           inst_type, ct_type, quantity, symbol.split("-")[0],
                           effective_price if ct_type == "inverse" else price or 0, sz, ct_val_log)
            else:
                return None, f"Cannot place {inst_type} order without symbol info", 0.0
            sz_str = str(int(sz))
        else:
            decimals: int = 8
            if symbol_info:
                min_sz = symbol_info.get("min_qty", 0)
                lot_sz = symbol_info.get("lot_sz", 0.00000001)
                decimals = symbol_info.get("qty_precision", 8)
                sz = round(quantity, decimals)
                if sz < min_sz:
                    logger.error("SPOT order size %.8f below minimum %.8f for %s",
                                sz, min_sz, symbol)
                    return None, f"Size {sz} below minimum {min_sz}", 0.0
                logger.info("SPOT order: qty=%.8f (minSz=%.8f, lotSz=%.8f, decimals=%d)",
                           sz, min_sz, lot_sz, decimals)
            else:
                sz = round(quantity, decimals)
                logger.warning("SPOT order without symbol info, using defaults: qty=%.8f, decimals=%d",
                              sz, decimals)

            if sz <= 0:
                return None, f"Order size {sz} is not positive after rounding", 0.0

            sz_str = f"{sz:.{decimals}f}".rstrip("0").rstrip(".")
            if not sz_str or sz_str == "0":
                return None, f"Order size formatted to invalid value: {sz_str}", 0.0

        if order_type == "MARKET":
            okx_ord_type = "market"
        elif order_type == "POST_ONLY":
            okx_ord_type = "post_only"
        else:
            okx_ord_type = "limit"

        order_data: Dict[str, Any] = {
            "instId": symbol,
            "tdMode": td_mode,
            "side": side.lower(),
            "ordType": okx_ord_type,
            "sz": sz_str,
        }

        if order_type in ("LIMIT", "POST_ONLY") and price:
            if symbol_info:
                price_decimals = symbol_info.get("price_precision", 2)
                rounded_price = round(price, price_decimals)
                px_str = f"{rounded_price:.{price_decimals}f}"
            else:
                px_str = str(round(price, 2))
            order_data["px"] = px_str

        if reduce_only and inst_type in ("SWAP", "FUTURES"):
            order_data["reduceOnly"] = True

        if inst_type == "SPOT" and okx_ord_type == "market" and side.upper() == "BUY" and td_mode == "cash":
            order_data["tgtCcy"] = "base_ccy"

        if inst_type == "SPOT" and td_mode == "cross":
            symbol_parts = symbol.split("-")
            if len(symbol_parts) >= 2 and symbol_parts[1]:
                order_data["ccy"] = symbol_parts[1]
                if okx_ord_type == "market" and side.upper() == "BUY":
                    if notional_usdt and notional_usdt > 0:
                        sz_str = f"{round(notional_usdt, 2):.2f}"
                        order_data["sz"] = sz_str
                        logger.info("Cross-margin SPOT MARKET BUY: overriding sz to notional_usdt=%.2f USDT",
                                    notional_usdt)
                    else:
                        logger.error(
                            "Cross-margin SPOT MARKET BUY requires notional_usdt "
                            "(qty=%.8f BTC would be read as USDT by OKX). Blocking order.", quantity
                        )
                        return None, (
                            "Cross-margin SPOT MARKET BUY missing notional_usdt — "
                            "order blocked to prevent wrong-size placement"
                        ), 0.0

        if inst_type in ("SWAP", "FUTURES"):
            if pos_side:
                order_data["posSide"] = pos_side
                logger.info("Using explicit posSide=%s", pos_side)
            elif not reduce_only:
                account_config = await self.get_account_config()
                if account_config and account_config.get("position_mode") == "long_short_mode":
                    order_data["posSide"] = "long" if side.upper() == "BUY" else "short"
                    logger.info("Account in long_short_mode, auto-setting posSide=%s for entry",
                                order_data["posSide"])

        return order_data, None, sz

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
        """
        Place an order.

        Note: For SWAP contracts, quantity is in base currency (e.g., BTC).
        This method converts to contracts automatically.

        Args:
            pos_side: Position side for long/short mode accounts ("long" or "short").
                      If None, will auto-detect based on account mode.
        """
        try:
            order_data, error, sz = await self._prepare_order(
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
            if error:
                return OrderResult(success=False, error=error)

            logger.info("Placing order: %s", order_data)
            result = await self._request("POST", "/api/v5/trade/order", data=order_data)

            if result and result.get("code") == "0" and result.get("data"):
                order_info = result["data"][0]
                order_id = order_info.get("ordId", "")
                logger.info("Order placed successfully: %s %s %s qty=%s, order_id=%s",
                           side, order_type, symbol, sz, order_id)

                # Invalidate position cache so the next get_positions() call sees
                # the new position instead of the pre-order stale snapshot.
                self._positions_cache = None

                # For MARKET orders, assume immediate fill
                # For LIMIT/POST_ONLY orders, return 0 filled until confirmed via get_order_status
                if order_type == "MARKET":
                    # Poll for actual fill — MARKET orders on OKX typically settle within 200-500ms
                    confirmed_fill = None
                    for _attempt in range(3):
                        await asyncio.sleep(0.3)
                        status = await self.get_order_status(symbol, order_id)
                        if status and status.get("state") == "filled":
                            confirmed_fill = status
                            break
                    if confirmed_fill:
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            filled_qty=confirmed_fill["filled_qty"],
                            filled_price=confirmed_fill["filled_price"],
                        )
                    logger.warning(
                        "MARKET order %s fill not confirmed in 900ms polling window — "
                        "returning filled_qty=0 so executor polls for confirmation",
                        order_id,
                    )
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        filled_qty=0,
                        filled_price=0,
                    )
                else:
                    # Limit/post_only order - don't assume fill, let caller check status
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        filled_qty=0,  # Not filled yet - must check status
                        filled_price=0,
                    )
            else:
                # Log full error details
                error = result.get("msg", "Unknown error") if result else "No response"
                data_errors = result.get("data", []) if result else []
                already_flat = False
                if data_errors and isinstance(data_errors, list) and len(data_errors) > 0:
                    sub_error = data_errors[0].get("sMsg", "") or data_errors[0].get("sCode", "")
                    if sub_error:
                        error = f"{error}: {sub_error}"
                    # sCode 51169: no position in this direction — futures already closed
                    already_flat = any(
                        str(d.get("sCode", "")) == "51169"
                        for d in data_errors if isinstance(d, dict)
                    )
                logger.error("Order failed: %s | Request: %s | Response: %s",
                            error, order_data, result)
                return OrderResult(success=False, error=error, already_flat=already_flat)

        except Exception as e:
            logger.exception("Error placing OKX order")
            return OrderResult(success=False, error=str(e))

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order."""
        try:
            result = await self._request(
                "POST",
                "/api/v5/trade/cancel-order",
                data={"instId": symbol, "ordId": order_id},
            )

            return result and result.get("code") == "0"

        except Exception as e:
            logger.exception("Error canceling OKX order")
            return False

    async def get_order_status(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the status of a specific order.

        Returns:
            Dict with order status info including:
            - state: 'live', 'partially_filled', 'filled', 'canceled'
            - filled_qty: Amount filled
            - filled_price: Average fill price
            - remaining_qty: Unfilled amount
        """
        try:
            result = await self._request(
                "GET",
                "/api/v5/trade/order",
                params={"instId": symbol, "ordId": order_id},
            )

            if result and result.get("code") == "0" and result.get("data"):
                o = result["data"][0]
                sz = float(o.get("sz", 0) or 0)
                fill_sz = float(o.get("accFillSz", 0) or o.get("fillSz", 0) or 0)
                fill_px = float(o.get("avgPx", 0) or o.get("fillPx", 0) or 0)
                state = o.get("state", "")

                return {
                    "order_id": order_id,
                    "symbol": symbol,
                    "state": state,  # live, partially_filled, filled, canceled
                    "quantity": sz,
                    "filled_qty": fill_sz,
                    "filled_price": fill_px,
                    "remaining_qty": sz - fill_sz,
                    "side": o.get("side", ""),
                    "order_type": o.get("ordType", ""),
                    # OKX cancelSource codes (only set when state == 'canceled'):
                    #   0  user-initiated     1  system
                    #   2  not-matched cancel 20 POST_ONLY would-have-matched (rejected)
                    #   21 self-trade-prevention triggered
                    #   31 trigger order limit  17 IOC unfilled portion
                    # Full list: https://www.okx.com/docs-v5/en/#error-code
                    "cancel_source": o.get("cancelSource", ""),
                    "cancel_source_reason": o.get("cancelSourceReason", ""),
                    # Actual fee charged by OKX (negative = fee paid, in fee_ccy units)
                    "fee":     float(o.get("fee", 0) or 0),
                    "fee_ccy": o.get("feeCcy", ""),
                }
            else:
                # Order might not exist (already cancelled or never placed)
                logger.warning("Could not fetch order status for %s: %s",
                             order_id, result.get("msg") if result else "No response")
                return None

        except Exception as e:
            logger.error("Error fetching order status: %s", e)
            return None

    async def get_pending_orders(self, symbol: Optional[str] = None, inst_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all pending (open) orders.

        Args:
            symbol: Optional specific symbol to filter
            inst_type: Optional instrument type ('SPOT', 'SWAP')

        Returns:
            List of pending orders
        """
        try:
            params: Dict[str, Any] = {}
            if symbol:
                params["instId"] = symbol
                # The symbol is authoritative — derive instType from it rather
                # than trusting a caller-supplied value. Callers historically
                # hardcoded SPOT/SWAP, which is wrong for dated FUTURES
                # (BTC-USDT-260626) and made OKX reject the query with 51015.
                inst_type = _detect_inst_type(symbol)
            if inst_type:
                params["instType"] = inst_type

            result = await self._request("GET", "/api/v5/trade/orders-pending", params=params)

            orders = []
            if result and result.get("code") == "0" and result.get("data"):
                for o in result["data"]:
                    orders.append({
                        "order_id": o.get("ordId", ""),
                        "symbol": o.get("instId", ""),
                        "side": o.get("side", ""),
                        "order_type": o.get("ordType", ""),
                        "quantity": float(o.get("sz", 0) or 0),
                        "price": float(o.get("px", 0) or 0),
                        # accFillSz = cumulative; fillSz = last chunk only
                        "filled_qty": float(o.get("accFillSz", 0) or o.get("fillSz", 0) or 0),
                        "state": o.get("state", ""),
                        "created_at": o.get("cTime", ""),
                    })
            return orders

        except Exception as e:
            logger.error("Error fetching pending orders: %s", e)
            return []

    async def cancel_all_orders(self, symbol: Optional[str] = None, inst_type: Optional[str] = None) -> int:
        """
        Cancel all pending orders for a symbol or instrument type.

        Returns:
            Number of orders cancelled
        """
        try:
            pending = await self.get_pending_orders(symbol, inst_type)
            cancelled = 0

            for order in pending:
                success = await self.cancel_order(order["symbol"], order["order_id"])
                if success:
                    cancelled += 1
                    logger.info("Cancelled orphan order: %s on %s", order["order_id"], order["symbol"])

            return cancelled

        except Exception as e:
            logger.error("Error cancelling all orders: %s", e)
            return 0

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """Get open positions."""
        try:
            # Return cached result if within TTL — prevents multiple concurrent callers
            # (engine reconciler, account-info route, position-margin route) from all
            # firing separate requests within the same polling cycle.
            now = time.monotonic()
            if (
                symbol is None
                and self._positions_cache is not None
                and (now - self._positions_cache_ts) < self._POSITIONS_TTL
            ):
                return self._positions_cache

            params = {}
            if symbol:
                params["instId"] = symbol

            result = await self._request("GET", "/api/v5/account/positions", params=params)

            positions = []
            if result and result.get("code") == "0" and result.get("data"):
                for p in result["data"]:
                    inst_type = p.get("instType", "")   # MARGIN / SWAP / FUTURES
                    inst_id   = p.get("instId", "")
                    pos_raw   = float(p.get("pos", 0))
                    pos_ccy   = p.get("posCcy", "")
                    avg_px    = float(p.get("avgPx", 0) or 0)

                    if pos_raw == 0:
                        continue

                    if inst_type == "MARGIN":
                        # OKX returns 'pos' in *posCcy* units for MARGIN positions:
                        #   LONG  (bought base): posCcy = base (BTC), pos = +BTC qty
                        #   SHORT (sold base):   posCcy = quote (USDT), pos = +USDT received
                        # Some OKX responses omit posCcy; fall back to posSide field.
                        base_ccy = inst_id.split("-")[0]  # "BTC" from "BTC-USDT"
                        pos_side_field = p.get("posSide", "")  # "long", "short", or ""
                        if pos_ccy and pos_ccy != base_ccy:
                            # posCcy is quote currency → SHORT
                            side = "SHORT"
                            qty  = (pos_raw / avg_px) if avg_px > 0 else 0.0
                        elif pos_side_field.lower() == "short":
                            # posCcy absent or ambiguous but posSide says short
                            side = "SHORT"
                            qty  = (pos_raw / avg_px) if avg_px > 0 else abs(pos_raw)
                        else:
                            # LONG: pos is already in base currency
                            side = "LONG"
                            qty  = abs(pos_raw)
                    else:
                        # SWAP / FUTURES direction depends on the account's position mode:
                        #
                        #   long_short_mode (hedge): `pos` is always positive (just the
                        #     absolute size); direction comes from `posSide` which is
                        #     "long" or "short". This is the user's current account mode.
                        #
                        #   net_mode (one-way): `pos` is signed (+ = long, - = short)
                        #     and `posSide` is "net".
                        #
                        # Reading the sign of `pos` alone (which we did) mis-classifies
                        # every long_short_mode SHORT as a LONG (since `pos` is positive),
                        # which then causes the orphan auto-close to send
                        # posSide=long → OKX 51169 (no long position to close).
                        pos_side_field = (p.get("posSide", "") or "").lower()
                        if pos_side_field in ("long", "short"):
                            side = pos_side_field.upper()
                        else:
                            # net_mode (posSide=="net") or unspecified: fall back to sign.
                            side = "LONG" if pos_raw > 0 else "SHORT"
                        qty  = abs(pos_raw)

                    if qty == 0:
                        continue

                    positions.append(Position(
                        symbol=inst_id,
                        side=side,
                        quantity=qty,
                        entry_price=avg_px,
                        unrealized_pnl=float(p.get("upl", 0)),
                        leverage=float(p.get("lever", 1)),
                    ))

            # Cache unfiltered (symbol=None) results only
            if symbol is None:
                self._positions_cache = positions
                self._positions_cache_ts = time.monotonic()

            return positions

        except Exception as e:
            logger.exception("Error fetching OKX positions")
            return []

    async def get_leverage_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get leverage setting for a specific instrument."""
        try:
            # Determine margin mode from symbol
            mgn_mode = "cross"  # Default to cross margin

            params = {
                "instId": symbol,
                "mgnMode": mgn_mode,
            }

            result = await self._request("GET", "/api/v5/account/leverage-info", params=params)

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                return {
                    "symbol": data.get("instId", symbol),
                    "leverage": float(data.get("lever", 1)),
                    "margin_mode": data.get("mgnMode", "cross"),
                    "pos_side": data.get("posSide", ""),
                }

            return None

        except Exception as e:
            logger.warning("Error fetching leverage info for %s: %s", symbol, e)
            return None

    async def close_position(self, symbol: str) -> OrderResult:
        """Close an open position.

        For SWAP/FUTURES: uses OKX /trade/close-position endpoint.
        For MARGIN (spot): places an explicit market order in the opposite direction
        because the close-position endpoint returns success but doesn't reliably fill
        in OKX demo mode for spot margin.
        """
        try:
            positions = await self.get_positions(symbol)
            if not positions:
                return OrderResult(success=True)  # Nothing to close

            pos = positions[0]
            is_swap = is_derivative(symbol)

            if not is_swap:
                # Spot margin: place explicit covering market order
                # SHORT margin (borrowed BTC sold) → BUY to repay borrow
                # LONG  margin (bought BTC on margin) → SELL to close
                cover_side = "BUY" if pos.side == "SHORT" else "SELL"
                qty = round(pos.quantity, 8)
                if qty <= 0:
                    return OrderResult(success=True)

                logger.info(
                    "Closing spot margin %s %s: placing %s MARKET qty=%.8f",
                    pos.side, symbol, cover_side, qty,
                )

                # For cross-margin SPOT MARKET BUY, OKX interprets sz as USDT (quote
                # currency), not BTC.  We must pass notional_usdt so place_order can
                # override sz with the correct USDT amount.
                notional_usdt: Optional[float] = None
                if cover_side == "BUY":
                    tick = await self.get_tick(symbol)
                    ref_price = tick.ask if (tick and tick.ask > 0) else (tick.last if tick else 0)
                    if ref_price <= 0 and pos.entry_price > 0:
                        ref_price = pos.entry_price  # fallback
                    if ref_price > 0:
                        notional_usdt = round(qty * ref_price * 1.002, 2)  # 0.2% buffer
                        logger.info("Cross-margin SPOT MARKET BUY: using notional_usdt=%.2f (ref_price=%.2f)",
                                    notional_usdt, ref_price)

                result = await self.place_order(
                    symbol=symbol,
                    side=cover_side,
                    order_type="MARKET",
                    quantity=qty,
                    notional_usdt=notional_usdt,
                )
                if result.success:
                    logger.info("Spot margin position closed: %s %s", symbol, pos.side)
                else:
                    logger.error("Failed to close spot margin %s: %s", symbol, result.error)
                return result

            # SWAP / FUTURES: use OKX close-position endpoint
            close_data: Dict[str, Any] = {
                "instId": symbol,
                "mgnMode": "cross",
            }
            is_perp = is_derivative(symbol)
            if not is_perp:
                parts = symbol.split("-")
                if len(parts) >= 2:
                    close_data["ccy"] = parts[-1]  # e.g. "USDT"

            # In long/short mode OKX accounts posSide is required for close-position.
            # Detect account mode and supply it so the close doesn't fail with
            # "posSide cannot be empty".
            account_config = await self.get_account_config()
            if account_config and account_config.get("position_mode") == "long_short_mode":
                # Map position side to OKX posSide value
                close_data["posSide"] = "long" if pos.side == "LONG" else "short"
                logger.info("long_short_mode: adding posSide=%s to close-position", close_data["posSide"])

            result = await self._request(
                "POST",
                "/api/v5/trade/close-position",
                data=close_data,
            )

            if result and result.get("code") == "0":
                return OrderResult(success=True)
            else:
                error = result.get("msg", "Unknown error") if result else "No response"
                return OrderResult(success=False, error=error)

        except Exception as e:
            logger.exception("Error closing OKX position")
            return OrderResult(success=False, error=str(e))

    async def get_account_info(self) -> Optional[AccountInfo]:
        """Get detailed account information including margin requirements."""
        try:
            result = await self._request("GET", "/api/v5/account/balance")

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                total_eq = float(data.get("totalEq", 0))
                imr = float(data.get("imr") or 0)  # Initial margin requirement
                mmr = float(data.get("mmr") or 0)  # Maintenance margin requirement
                upl = float(data.get("upl") or 0)  # Unrealized P&L

                # Available equity from top-level (cross-margin available)
                # This is more accurate than just USDT availBal
                avail_eq = float(data.get("availEq") or 0)

                # Per-currency totals
                usdt_avail = 0
                usdt_eq = 0
                total_cash_bal = 0
                total_avail_eq = 0  # sum of per-currency availEq (margin-eligible across all CCYs)
                for detail in data.get("details", []):
                    ccy = detail.get("ccy", "")
                    cash_bal = float(detail.get("cashBal") or 0)
                    eq = float(detail.get("eq") or 0)
                    ccy_avail_eq = float(detail.get("availEq") or 0)
                    total_cash_bal += cash_bal
                    total_avail_eq += ccy_avail_eq

                    if ccy == "USDT":
                        usdt_avail = float(detail.get("availBal") or 0)
                        usdt_eq = eq

                # Account-level availEq is the most accurate (cross-margin free collateral).
                # For accounts with no open positions OKX returns 0 there; fall back to the
                # sum of per-currency availEq which is always populated.
                available = avail_eq if avail_eq > 0 else (total_avail_eq if total_avail_eq > 0 else usdt_avail)

                # Calculate margin ratio (lower is riskier)
                margin_ratio = 0.0
                if mmr > 0:
                    margin_ratio = (total_eq / mmr) * 100  # As percentage

                logger.debug("Account: totalEq=%.2f, availEq=%.2f, usdt_avail=%.2f, imr=%.2f, upl=%.2f",
                            total_eq, avail_eq, usdt_avail, imr, upl)

                return AccountInfo(
                    exchange="OKX",
                    balance_usd=total_eq,
                    available_balance_usd=available,
                    margin_used=imr,
                    unrealized_pnl=upl,
                    total_equity=total_eq,
                    initial_margin=imr,
                    maintenance_margin=mmr,
                    margin_ratio=margin_ratio,
                    available_margin=available,
                    leverage_used=imr / total_eq if total_eq > 0 else 0,
                )

        except Exception as e:
            logger.exception("Error fetching OKX account info")

        return None

    async def get_position_margin_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get detailed margin info for a specific position."""
        try:
            positions = await self.get_positions(symbol)
            if not positions:
                return None

            pos = positions[0]

            # Get mark price
            mark_result = await self._request(
                "GET",
                "/api/v5/public/mark-price",
                params={"instId": symbol},
            )
            mark_price = None
            if mark_result and mark_result.get("code") == "0" and mark_result.get("data"):
                mark_price = float(mark_result["data"][0].get("markPx", 0))

            # Get position details with margin info
            pos_result = await self._request(
                "GET",
                "/api/v5/account/positions",
                params={"instId": symbol},
            )

            if pos_result and pos_result.get("code") == "0" and pos_result.get("data"):
                p = pos_result["data"][0]

                def safe_float(val, default=0.0):
                    """Convert to float safely, handling empty strings from OKX."""
                    try:
                        return float(val) if val not in (None, '', 'None') else default
                    except (ValueError, TypeError):
                        return default

                liq_px = p.get("liqPx")
                return {
                    "symbol": symbol,
                    "side": pos.side,
                    "quantity": pos.quantity,
                    "entry_price": pos.entry_price,
                    "mark_price": mark_price,
                    "liquidation_price": safe_float(liq_px) if liq_px not in (None, '', 'None') else None,
                    "margin": safe_float(p.get("margin")),
                    "margin_ratio": safe_float(p.get("mgnRatio")) * 100,
                    "unrealized_pnl": pos.unrealized_pnl,
                    "leverage": pos.leverage,
                    "imr": safe_float(p.get("imr")),
                    "mmr": safe_float(p.get("mmr")),
                }

        except Exception as e:
            logger.error("Error fetching position margin info: %s", e)

        return None

    async def get_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get funding rate for perpetual."""
        try:
            result = await self._request(
                "GET",
                "/api/v5/public/funding-rate",
                params={"instId": symbol},
            )

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                return {
                    "symbol": symbol,
                    "funding_rate": float(data.get("fundingRate", 0)),
                    "next_funding_time": data.get("fundingTime"),
                }

        except Exception as e:
            logger.error("Error fetching OKX funding rate: %s", e)

        return None

    async def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get symbol trading information."""
        try:
            inst_type = _detect_inst_type(symbol)
            # Dated FUTURES contracts expire and OKX returns 51001 after expiry.
            # Suppress that to DEBUG — it is expected, not an error.
            quiet = {"51001"} if inst_type == "FUTURES" else None
            result = await self._request(
                "GET",
                "/api/v5/public/instruments",
                params={"instType": inst_type, "instId": symbol},
                quiet_codes=quiet,
            )

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                lot_sz_str = data.get("lotSz", "0.00000001")
                tick_sz_str = data.get("tickSz", "0.01")

                # Calculate decimal precision from string representation
                # e.g., "0.00000001" -> 8 decimals, "0.001" -> 3 decimals
                qty_precision = len(lot_sz_str.split(".")[1]) if "." in lot_sz_str else 0
                price_precision = len(tick_sz_str.split(".")[1]) if "." in tick_sz_str else 0

                ct_type = data.get("ctType", "linear")
                ct_val_raw = float(data.get("ctVal") or 1)

                # For inverse (coin-margined) contracts, ctVal is in USD.
                # The trading engine's min-size check compares base_qty (BTC) to
                # contract_val, so we must normalise to base-coin equivalent here.
                # ct_val_usd preserves the raw USD face value for the sizing formula.
                ct_val_usd = None
                contract_val = ct_val_raw
                if ct_type == "inverse":
                    ct_val_usd = ct_val_raw
                    # Fetch live mid-price to convert USD face → base-coin equivalent.
                    # Fails open: returns a tiny sentinel so min-size never blocks.
                    try:
                        tick = await self.get_tick(symbol)
                        mid = 0.0
                        if tick:
                            mid = (tick.bid + tick.ask) / 2.0 if tick.bid > 0 and tick.ask > 0 else tick.last
                        contract_val = (ct_val_usd / mid) if mid > 0 else 1e-9
                    except Exception:
                        contract_val = 1e-9  # fail open — never blocks entry

                return {
                    "symbol": symbol,
                    "min_qty": float(data.get("minSz") or 0),
                    "lot_sz": float(lot_sz_str),  # Minimum order increment
                    "tick_sz": float(tick_sz_str),  # Price tick size
                    "qty_precision": qty_precision,
                    "price_precision": price_precision,
                    # Base-coin equivalent of 1 contract (used by min-size check)
                    "contract_val": contract_val,
                    # Raw USD face value per contract; only set for inverse contracts
                    "ct_val_usd": ct_val_usd,
                    # "USD" means inverse (coin-margined); base ccy means linear
                    "ct_val_ccy": data.get("ctValCcy", ""),
                    "ct_type": ct_type,
                }

        except Exception as e:
            logger.error("Error fetching OKX symbol info: %s", e)

        return None

    async def get_server_time_ms(self) -> Optional[float]:
        """Return OKX server time in milliseconds, or None on error."""
        try:
            result = await self._request("GET", "/api/v5/public/time")
            if result and result.get("code") == "0":
                return float(result["data"][0]["ts"])
        except Exception as e:
            logger.debug("get_server_time_ms failed: %s", e)
        return None

    async def get_instruments(self, inst_type: str = "SPOT") -> List[Dict[str, Any]]:
        """List all tradable instruments of a given type from OKX.

        Public (unauthenticated) endpoint, so it works before login and in
        demo mode. Each entry is tagged with a ``category`` describing its
        trade-time math characteristics:

        - ``usdt_linear``: USDT spot or USDT-settled linear perpetual.
          Sizing (position_size_usd / price), fees (bps), PnL and the
          hedge-ratio math are all correct.
        - ``usdc_linear``: USDC rail. USDC ~= 1 USD so the math is
          approximately correct (small USDT/USDC basis).
        - ``inverse``: coin-margined perpetual. Margin and PnL are in the
          base coin, not USD.
        - ``dated_usdt`` / ``dated_usdc``: dated futures, USD-stable settled.
          Basis compresses to zero at expiry.
        - ``inverse_dated``: dated + inverse — both caveats apply.
        - ``non_usd_quoted`` / ``other`` / ``other_dated``: spot quoted in
          another crypto/fiat, or anything else exotic.

        Only ``usdt_linear`` is guaranteed safe with the current engine; the
        UI surfaces the category so the operator can choose with eyes open.

        Args:
            inst_type: "SPOT", "SWAP", or "FUTURES".

        Returns:
            Sorted list of {instId, base, quote, category, expiry, label}.
            ``usdt_linear`` entries are listed first to keep the common case
            at the top of the dropdown.
        """
        inst_type = inst_type.upper()
        out: List[Dict[str, Any]] = []
        try:
            result = await self._request(
                "GET",
                "/api/v5/public/instruments",
                params={"instType": inst_type},
            )

            if not (result and result.get("code") == "0" and result.get("data")):
                return out

            for d in result["data"]:
                inst_id = d.get("instId", "")
                if not inst_id or d.get("state") != "live":
                    continue

                expiry = ""

                if inst_type == "SPOT":
                    base = d.get("baseCcy") or inst_id.split("-")[0]
                    quote = d.get("quoteCcy") or (
                        inst_id.split("-", 1)[1] if "-" in inst_id else ""
                    )
                    if quote == "USDT":
                        category = "usdt_linear"
                    elif quote == "USDC":
                        category = "usdc_linear"
                    else:
                        category = "non_usd_quoted"

                elif inst_type == "SWAP":
                    base = d.get("ctValCcy") or inst_id.split("-")[0]
                    quote = d.get("settleCcy") or ""
                    ct_type = (d.get("ctType") or "linear").lower()
                    if ct_type == "inverse":
                        category = "inverse"
                    elif quote == "USDT":
                        category = "usdt_linear"
                    elif quote == "USDC":
                        category = "usdc_linear"
                    else:
                        category = "other"

                elif inst_type == "FUTURES":
                    base = d.get("ctValCcy") or inst_id.split("-")[0]
                    quote = d.get("settleCcy") or ""
                    ct_type = (d.get("ctType") or "linear").lower()
                    expiry = d.get("expTime") or ""
                    if ct_type == "inverse":
                        category = "inverse_dated"
                    elif quote == "USDT":
                        category = "dated_usdt"
                    elif quote == "USDC":
                        category = "dated_usdc"
                    else:
                        category = "other_dated"

                else:
                    # OPTION / MARGIN — not usable as a stat-arb leg
                    continue

                out.append({
                    "instId": inst_id,
                    "base": base,
                    "quote": quote,
                    "category": category,
                    "expiry": expiry,
                    "label": inst_id,
                })

            # Safe instruments first; alphabetic within each tier
            out.sort(key=lambda x: (x["category"] != "usdt_linear", x["instId"]))
            logger.info("OKX %s instruments listed: %d", inst_type, len(out))

        except Exception as e:
            logger.error("Error fetching OKX instruments (%s): %s", inst_type, e)

        return out

    async def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "cross") -> bool:
        """
        Set leverage for a symbol.

        Args:
            symbol: Instrument ID (e.g., BTC-USDT-SWAP)
            leverage: Leverage value (1-125 depending on instrument)
            margin_mode: 'cross' or 'isolated'

        Returns:
            True if successful, False otherwise
        """
        try:
            # Leverage setting is only for derivatives (SWAP + dated FUTURES), not spot
            if not is_derivative(symbol):
                logger.debug("Leverage not applicable for spot symbol: %s", symbol)
                return True

            data = {
                "instId": symbol,
                "lever": str(leverage),
                "mgnMode": margin_mode,
            }

            result = await self._request("POST", "/api/v5/account/set-leverage", data=data)

            if result and result.get("code") == "0":
                logger.info("Leverage set to %dx for %s (mode=%s)", leverage, symbol, margin_mode)
                return True
            else:
                error = result.get("msg", "Unknown error") if result else "No response"
                logger.error("Failed to set leverage for %s: %s", symbol, error)
                return False

        except Exception as e:
            logger.exception("Error setting leverage for %s", symbol)
            return False

    async def get_leverage(self, symbol: str) -> Optional[int]:
        """
        Get current leverage setting for a symbol.

        Args:
            symbol: Instrument ID (e.g., BTC-USDT-SWAP)

        Returns:
            Current leverage value or None if error
        """
        try:
            if not is_derivative(symbol):
                return 1  # Spot doesn't have leverage

            result = await self._request(
                "GET",
                "/api/v5/account/leverage-info",
                params={"instId": symbol, "mgnMode": "cross"},
            )

            if result and result.get("code") == "0" and result.get("data"):
                return int(float(result["data"][0].get("lever", 1)))

        except Exception as e:
            logger.error("Error getting leverage for %s: %s", symbol, e)

        return None

    async def get_order_history(self, symbol: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Fetch recent order history from OKX.

        Returns filled and cancelled orders for both SPOT and SWAP.
        """
        orders = []

        # Include dated FUTURES, not just SPOT + perpetual SWAP — otherwise
        # dated-future trades (e.g. ETH-USDT-260626) never show in the order log.
        # OKX returns sz/fillSz in CONTRACTS for SWAP and FUTURES. Convert to
        # base-currency amounts via ctVal so the dashboard shows '0.7 ETH'
        # instead of '7 contracts' — matching what humans see on OKX itself.
        ctval_cache: Dict[str, float] = {}
        async def _ctval(sym: str) -> float:
            if sym not in ctval_cache:
                info = await self.get_symbol_info(sym)
                ctval_cache[sym] = float((info or {}).get("contract_val") or 1.0) or 1.0
            return ctval_cache[sym]

        inst_types = ["SPOT", "SWAP", "FUTURES"]
        for inst_type in inst_types:
            params: Dict[str, Any] = {"instType": inst_type, "limit": str(limit)}
            if symbol:
                params["instId"] = symbol

            result = await self._request("GET", "/api/v5/trade/orders-history", params=params)

            if result and result.get("code") == "0" and result.get("data"):
                for o in result["data"]:
                    try:
                        fee = o.get("fee", "0") or "0"
                        # Prefer accumulated/average across all fills over last-chunk
                        # values — OKX fills market orders in pieces and `fillSz`/`fillPx`
                        # only reflect the final chunk, which made the 02:13:13 market
                        # sell appear as 0.0005 BTC instead of the full 0.286 BTC.
                        fill_px = o.get("avgPx", "") or o.get("fillPx", "") or "0"
                        fill_sz = o.get("accFillSz", "") or o.get("fillSz", "") or "0"
                        inst_id = o.get("instId", "")
                        raw_qty = float(o.get("sz", 0) or 0)
                        raw_fill = float(fill_sz)
                        # SWAP/FUTURES: sz is in CONTRACTS — multiply by ctVal
                        # to get base-currency units. SPOT: sz is already in base.
                        ctval = await _ctval(inst_id) if inst_type in ("SWAP", "FUTURES") else 1.0
                        orders.append({
                            "order_id":    o.get("ordId", ""),
                            "symbol":      inst_id,
                            "inst_type":   inst_type,
                            "side":        o.get("side", ""),       # buy / sell
                            "pos_side":    o.get("posSide", ""),    # long / short / net
                            "order_type":  o.get("ordType", ""),    # market / limit
                            "state":       o.get("state", ""),      # filled / cancelled / live
                            "quantity":    raw_qty * ctval,         # base units (e.g. 0.7 ETH not 7 contracts)
                            "fill_qty":    raw_fill * ctval,
                            "fill_price":  float(fill_px),
                            "contracts":   raw_qty,                 # original for audit
                            "fill_contracts": raw_fill,
                            "ct_val":      ctval,
                            "fee":         float(fee),
                            "fee_ccy":     o.get("feeCcy", ""),
                            "leverage":    o.get("lever", ""),
                            "pnl":         float(o.get("pnl", 0) or 0),
                            "created_at":  o.get("cTime", ""),
                            "filled_at":   o.get("uTime", ""),
                            "td_mode":     o.get("tdMode", ""),     # cash / cross / isolated
                        })
                    except (ValueError, TypeError) as e:
                        logger.debug("Skipping order record due to parse error: %s", e)
                        continue

        # Sort by creation time descending
        orders.sort(key=lambda x: x["created_at"], reverse=True)
        return orders[:limit]

    async def get_account_config(self) -> Optional[Dict[str, Any]]:
        """
        Get account configuration including UID.

        Returns:
            Dict with uid, account_level, position_mode, etc.
        """
        try:
            result = await self._request("GET", "/api/v5/account/config")

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                uid = data.get("uid", "")
                level = data.get("level", "")
                logger.debug("Account config fetched: UID=%s, Level=%s", uid, level)
                return {
                    "uid": uid,
                    "account_level": data.get("acctLv", ""),  # 1=Simple, 2=Single-currency margin, etc.
                    "position_mode": data.get("posMode", ""),  # long_short_mode or net_mode
                    "auto_loan": data.get("autoLoan", False),
                    "greeks_type": data.get("greeksType", ""),
                    "level": level,  # User level (VIP tier)
                    "level_tmp": data.get("levelTmp", ""),  # Temporary VIP level
                }
            else:
                error_msg = result.get("msg", "Unknown error") if result else "No response"
                logger.warning("Failed to fetch account config: %s", error_msg)

        except Exception as e:
            logger.error("Error fetching account config: %s", e)

        return None

    async def get_spot_balances(self, include_frozen: bool = True) -> Dict[str, Dict[str, float]]:
        """
        Get all spot balances from trading account.

        Returns:
            Dict mapping currency to balance details:
            {'BTC': {'available': 0.1, 'frozen': 0.15, 'total': 0.25, 'equity': 0.25}}
        """
        balances = {}
        try:
            result = await self._request("GET", "/api/v5/account/balance")

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                for detail in data.get("details", []):
                    ccy = detail.get("ccy", "")
                    avail = float(detail.get("availBal", 0) or 0)
                    frozen = float(detail.get("frozenBal", 0) or 0)
                    cash_bal = float(detail.get("cashBal", 0) or 0)
                    eq = float(detail.get("eq", 0) or 0)

                    # Include if there's any balance (available or frozen)
                    total = avail + frozen
                    if total > 0.00000001 or cash_bal > 0.00000001:  # Filter out true dust
                        balances[ccy] = {
                            'available': avail,
                            'frozen': frozen,
                            'total': cash_bal if cash_bal > 0 else total,
                            'equity': eq,
                        }
                        if ccy not in ('USDT', 'USDC'):
                            logger.debug("Balance %s: avail=%.8f, frozen=%.8f, total=%.8f, eq=%.8f",
                                        ccy, avail, frozen, cash_bal, eq)

        except Exception as e:
            logger.error("Error fetching spot balances: %s", e)

        return balances

    async def get_trading_balances_detailed(self) -> List[Dict[str, Any]]:
        """Per-currency breakdown of the Trading (Unified) account.

        Returned by OKX's /api/v5/account/balance under data[0].details[]. Each
        entry carries both the native balance (cashBal/availBal) and OKX's
        USD-converted equity contribution (eq/availEq). The pair eq>0,
        availEq=0 is the key 'has value but not margin-eligible' signature —
        the canonical case being fiat (AED/EUR) or illiquid altcoins on most
        account tiers. The diagnostic uses this to explain why the dashboard
        can show \$X equity with \$0 available.
        """
        try:
            result = await self._request("GET", "/api/v5/account/balance")
            if not (result and result.get("code") == "0" and result.get("data")):
                return []
            out = []
            for d in (result["data"][0].get("details") or []):
                cash_bal = float(d.get("cashBal") or 0)
                eq = float(d.get("eq") or 0)
                if cash_bal == 0 and eq == 0:
                    continue
                out.append({
                    "ccy":      d.get("ccy", ""),
                    "cashBal":  cash_bal,
                    "availBal": float(d.get("availBal") or 0),
                    "eq":       eq,
                    "availEq":  float(d.get("availEq") or 0),
                })
            return out
        except Exception as e:
            logger.error("Error fetching OKX trading balance details: %s", e)
            return []

    async def get_funding_balances(self, include_zero: bool = False) -> List[Dict[str, Any]]:
        """List balances in the **Funding** (asset) account.

        Critical OKX distinction: deposits land in the Funding account by
        default. The Trading (Unified) account — which the algo and the rest
        of get_account_info query via /api/v5/account/balance — is separate.
        Money in Funding is invisible to anything that only reads Trading.

        Returns a list of {ccy, bal, availBal, frozenBal} dicts. Empty list
        on error (caller decides what to display).
        """
        try:
            result = await self._request("GET", "/api/v5/asset/balances")
            if not (result and result.get("code") == "0"):
                return []
            out = []
            for d in (result.get("data") or []):
                bal = float(d.get("bal") or 0)
                if not include_zero and bal == 0:
                    continue
                out.append({
                    "ccy": d.get("ccy", ""),
                    "bal": bal,
                    "availBal": float(d.get("availBal") or 0),
                    "frozenBal": float(d.get("frozenBal") or 0),
                })
            return out
        except Exception as e:
            logger.error("Error fetching OKX funding balances: %s", e)
            return []

    async def get_asset_valuation(self, ccy: str = "USDT") -> Optional[float]:
        """Total cross-account valuation in ``ccy``, summed across Trading +
        Funding + Earn. The honest 'how much is in my OKX account total' number.
        Returns None on error so callers can show '—'.
        """
        try:
            result = await self._request(
                "GET", "/api/v5/asset/asset-valuation",
                params={"ccy": ccy},
            )
            if not (result and result.get("code") == "0" and result.get("data")):
                return None
            return float(result["data"][0].get("totalBal") or 0)
        except Exception as e:
            logger.error("Error fetching OKX asset valuation: %s", e)
            return None

    async def get_asset_balance(self, currency: str) -> Dict[str, float]:
        """
        Get balance info for a specific currency.

        Args:
            currency: Currency code (e.g., 'BTC', 'USDT')

        Returns:
            Dict with 'available', 'frozen', 'total', 'equity'
        """
        balances = await self.get_spot_balances()
        return balances.get(currency, {'available': 0, 'frozen': 0, 'total': 0, 'equity': 0})

    async def sell_spot_to_usdt(self, currency: str, quantity: float = None) -> OrderResult:
        """
        Sell a spot asset to USDT.

        Args:
            currency: Currency to sell (e.g., 'BTC')
            quantity: Amount to sell (if None, sells entire available balance)

        Returns:
            OrderResult with success/failure info
        """
        try:
            # Get current balance if quantity not specified
            bal_info = await self.get_asset_balance(currency)
            available = bal_info.get('available', 0)
            frozen = bal_info.get('frozen', 0)
            total = bal_info.get('total', 0)

            if quantity is None:
                quantity = available

            if quantity <= 0.00000001:  # Essentially zero
                if frozen > 0.00000001:
                    return OrderResult(
                        success=False,
                        error=f"No available {currency} to sell. {frozen:.6f} is frozen (used as margin). Close positions first."
                    )
                return OrderResult(success=True, error=f"No {currency} balance to sell")

            symbol = f"{currency}-USDT"

            # Get symbol info for precision
            symbol_info = await self.get_symbol_info(symbol)
            min_qty = symbol_info.get("min_qty", 0) if symbol_info else 0

            if quantity < min_qty:
                return OrderResult(success=False, error=f"Quantity {quantity} below minimum {min_qty}")

            # Always use tdMode=cash here — we're selling actual holdings, not margin trading.
            # Using tdMode=cross when wallet BTC is empty would open a new SHORT (OKX borrows
            # BTC to fill the sell), creating an infinite close→SHORT→close cycle.
            result = await self.place_order(
                symbol=symbol,
                side="SELL",
                order_type="MARKET",
                quantity=quantity,
                force_td_mode="cash",
            )

            if result.success:
                logger.info("Sold %.6f %s to USDT, order_id=%s", quantity, currency, result.order_id)
            else:
                logger.error("Failed to sell %s: %s", currency, result.error)

            return result

        except Exception as e:
            logger.exception("Error selling %s to USDT", currency)
            return OrderResult(success=False, error=str(e))
