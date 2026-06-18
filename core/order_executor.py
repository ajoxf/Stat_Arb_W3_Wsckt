"""
Order execution module for crypto statistical arbitrage.

Supports two modes:
1. MARKET: Immediate execution with market orders (higher cost, guaranteed fill)
2. LIMIT: Pegged limit orders that track best bid/ask (lower cost, may not fill)

For spread trades, both legs must be managed simultaneously to avoid leg risk.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, Callable
from dataclasses import dataclass
from enum import Enum

from models import TradingConfig, OrderResult, MarketTick
from adapters.base import ExchangeAdapter, is_derivative

logger = logging.getLogger(__name__)


class ExecutionMode(Enum):
    """Order execution mode."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class LegStatus(Enum):
    """Status of a single leg in a spread trade."""
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class LegOrder:
    """Represents one leg of a spread trade."""
    symbol: str
    side: str  # BUY or SELL
    quantity: float
    target_price: float = 0.0
    order_id: str = ""
    status: LegStatus = LegStatus.PENDING
    filled_qty: float = 0.0
    filled_price: float = 0.0
    last_update: Optional[datetime] = None
    pos_side: Optional[str] = None  # For OKX long_short_mode: "long" or "short"
    # Price of the order currently resting on the exchange. Distinct from
    # target_price (which is the *desired* price; updated on every re-quote
    # cycle). Used by the amend-drift check so we compare the live order
    # against the current target, not previous-cycle-target vs new-target —
    # without it the drift threshold can be evaded by slow market moves where
    # each cycle is under the threshold but cumulative drift is large.
    placed_price: float = 0.0


@dataclass
class SpreadOrder:
    """Represents a complete spread trade (both legs)."""
    spot_leg: LegOrder
    futures_leg: LegOrder
    created_at: datetime = None
    timeout_at: datetime = None
    is_entry: bool = True  # True for entry, False for exit
    position_type: str = ""  # LONG or SHORT

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()

    @property
    def is_complete(self) -> bool:
        """Check if both legs are filled."""
        return (self.spot_leg.status == LegStatus.FILLED and
                self.futures_leg.status == LegStatus.FILLED)

    @property
    def is_failed(self) -> bool:
        """Check if either leg failed or was cancelled (POST_ONLY rejection)."""
        failed_states = (LegStatus.FAILED, LegStatus.CANCELLED)
        return (self.spot_leg.status in failed_states or
                self.futures_leg.status in failed_states)

    @property
    def has_partial_fill(self) -> bool:
        """Check if we have a partial fill (leg risk situation)."""
        spot_filled = self.spot_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)
        futures_filled = self.futures_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)
        return spot_filled != futures_filled

    @property
    def has_orphan_risk(self) -> bool:
        """
        Check if one leg failed/cancelled while the other filled.
        This is a critical leg risk situation requiring immediate action.
        """
        failed_states = (LegStatus.FAILED, LegStatus.CANCELLED)
        filled_states = (LegStatus.FILLED, LegStatus.PARTIAL)

        spot_failed = self.spot_leg.status in failed_states
        futures_failed = self.futures_leg.status in failed_states
        spot_filled = self.spot_leg.status in filled_states
        futures_filled = self.futures_leg.status in filled_states

        return (spot_failed and futures_filled) or (futures_failed and spot_filled)


class OrderExecutor:
    """
    Handles order execution for spread trades.

    Supports both market orders (immediate) and pegged limit orders
    (track best bid/ask for better fills).
    """

    # How often to re-price / amend limit orders (ms)
    PRICE_UPDATE_INTERVAL_MS = 1000  # 1 second

    # How often to poll fill status (ms). Faster than the amend cadence so fills
    # are detected promptly even when the price hasn't moved enough to trigger
    # a re-quote. Avoids the case where an order fills shortly after placement
    # but isn't seen until the next 1-second amend tick (which on trade 11
    # left a filled order undetected for ~50 seconds before timeout).
    STATUS_POLL_INTERVAL_MS = 250

    # Pause after placing/amending an order before the first status check.
    # OKX needs a brief moment to acknowledge state changes — too short and the
    # poll comes back "live" for an already-filled order.
    POST_PLACE_POLL_DELAY_MS = 200

    def __init__(
        self,
        config: TradingConfig,
        spot_adapter: ExchangeAdapter,
        futures_adapter: ExchangeAdapter,
    ):
        self.config = config
        self.spot_adapter = spot_adapter
        self.futures_adapter = futures_adapter

        # Current spread order being executed
        self.active_order: Optional[SpreadOrder] = None

        # Callbacks
        self.on_fill: Optional[Callable[[SpreadOrder], None]] = None
        self.on_partial_fill: Optional[Callable[[SpreadOrder], None]] = None
        self.on_timeout: Optional[Callable[[SpreadOrder], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

        # Execution state
        self._executing = False
        self._execution_task: Optional[asyncio.Task] = None

    def update_config(self, config: TradingConfig) -> None:
        """Update configuration."""
        self.config = config

    async def execute_entry(
        self,
        position_type: str,  # LONG or SHORT
        spot_tick: MarketTick,
        futures_tick: MarketTick,
        quantity: float,
        futures_quantity: Optional[float] = None,
    ) -> Optional[SpreadOrder]:
        """
        Execute entry trade for a spread position.

        LONG spread: Buy spot, Sell futures
        SHORT spread: Sell spot, Buy futures

        Args:
            quantity: Spot leg quantity (base units).
            futures_quantity: Futures leg quantity. Defaults to ``quantity``
                when None (same-underlying basis trade); differs when a hedge
                ratio scales the legs for a cross-instrument pair.

        Returns SpreadOrder with execution results.
        """
        if self._executing:
            logger.warning("Already executing an order")
            return None

        fut_quantity = futures_quantity if futures_quantity is not None else quantity

        # Determine leg sides and per-leg pos_side for OKX long_short_mode.
        # LONG spread = position is long Leg A, short Leg B (regardless of which
        # slot holds a derivative). pos_side reflects the POSITION direction;
        # set None for any leg that's spot — OKX rejects posSide on spot orders.
        if position_type == "LONG":
            spot_side, futures_side = "BUY", "SELL"
            leg_a_pos, leg_b_pos = "long", "short"
        else:
            spot_side, futures_side = "SELL", "BUY"
            leg_a_pos, leg_b_pos = "short", "long"

        leg_a_pos_side = leg_a_pos if is_derivative(self.config.spot_symbol) else None
        leg_b_pos_side = leg_b_pos if is_derivative(self.config.futures_symbol) else None

        # Create spread order
        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol=self.config.spot_symbol,
                side=spot_side,
                quantity=quantity,
                pos_side=leg_a_pos_side,  # None for spot, "long"/"short" for derivative
            ),
            futures_leg=LegOrder(
                symbol=self.config.futures_symbol,
                side=futures_side,
                quantity=fut_quantity,
                pos_side=leg_b_pos_side,
            ),
            is_entry=True,
            position_type=position_type,
            timeout_at=datetime.utcnow(),
        )

        # Set timeout
        from datetime import timedelta
        spread_order.timeout_at = datetime.utcnow() + timedelta(
            seconds=self.config.limit_order_timeout_sec
        )

        return await self._execute_spread(spread_order, spot_tick, futures_tick)

    async def execute_exit(
        self,
        position_type: str,  # Current position: LONG or SHORT
        spot_tick: MarketTick,
        futures_tick: MarketTick,
        quantity: float,
        futures_quantity: Optional[float] = None,
    ) -> Optional[SpreadOrder]:
        """
        Execute exit trade to close a spread position.

        Close LONG spread: Sell spot, Buy futures
        Close SHORT spread: Buy spot, Sell futures

        Args:
            quantity: Spot leg quantity (base units).
            futures_quantity: Futures leg quantity; defaults to ``quantity``.
                Must mirror the entry leg sizing for a clean close.
        """
        if self._executing:
            logger.warning("Already executing an order")
            return None

        fut_quantity = futures_quantity if futures_quantity is not None else quantity

        # Opposite trade direction from entry, but SAME pos_side (we're closing
        # the same position). pos_side is the position direction, not the trade.
        if position_type == "LONG":
            spot_side, futures_side = "SELL", "BUY"
            leg_a_pos, leg_b_pos = "long", "short"
        else:
            spot_side, futures_side = "BUY", "SELL"
            leg_a_pos, leg_b_pos = "short", "long"

        leg_a_pos_side = leg_a_pos if is_derivative(self.config.spot_symbol) else None
        leg_b_pos_side = leg_b_pos if is_derivative(self.config.futures_symbol) else None

        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol=self.config.spot_symbol,
                side=spot_side,
                quantity=quantity,
                pos_side=leg_a_pos_side,
            ),
            futures_leg=LegOrder(
                symbol=self.config.futures_symbol,
                side=futures_side,
                quantity=fut_quantity,
                pos_side=leg_b_pos_side,
            ),
            is_entry=False,
            position_type=position_type,
        )

        from datetime import timedelta
        spread_order.timeout_at = datetime.utcnow() + timedelta(
            seconds=self.config.limit_order_timeout_sec
        )

        # Pre-flight: ask the exchange what we actually hold. If a previous
        # cycle's fill went undetected, the corresponding leg's position is
        # already flat and we MUST NOT place a fresh order for it — that's
        # what double-sold the spot on the 04:30 trade and left an accidental
        # naked margin SHORT.
        await self._reconcile_exit_with_exchange(spread_order)

        return await self._execute_spread(spread_order, spot_tick, futures_tick)

    async def _reconcile_exit_with_exchange(self, spread_order: SpreadOrder) -> None:
        """
        Query the exchange for current positions and pre-mark any leg whose
        underlying position is already flat as FILLED. Subsequent placement
        code (`_place_limit_orders`, `_execute_market`) skips legs that are
        already FILLED so we never accidentally open a new naked position
        when "exiting" a leg that was already closed by a previous cycle.

        Only runs on exits. Fails open: if the position query errors out we
        proceed with the full exit quantity rather than blocking the close.
        """
        if spread_order.is_entry:
            return

        # What position should each leg currently be holding (= what we want to close)?
        if spread_order.position_type == "LONG":
            expected_spot_side = "LONG"     # we bought spot at entry
            expected_fut_side = "SHORT"     # we sold the perp short at entry
        else:
            expected_spot_side = "SHORT"    # we sold spot at entry (margin short)
            expected_fut_side = "LONG"      # we bought the perp long at entry

        # Consider a leg flat if exchange holds < 10% of the expected qty
        # (handles dust positions and floating-point noise).
        flat_threshold = 0.1
        # Treat anything between 10% and 95% as partial — scale exit qty
        # rather than over-closing or skipping outright.
        partial_threshold = 0.95

        # ---- spot leg ----
        try:
            spot_positions = await self.spot_adapter.get_positions(spread_order.spot_leg.symbol)
            spot_qty = next(
                (p.quantity for p in spot_positions
                 if p.symbol == spread_order.spot_leg.symbol and p.side == expected_spot_side),
                0.0,
            )
            expected = spread_order.spot_leg.quantity
            if spot_qty < expected * flat_threshold:
                logger.warning(
                    "Exit reconcile: spot %s position is flat on exchange "
                    "(have=%.6f, expected=%.6f) — skipping spot leg placement",
                    expected_spot_side, spot_qty, expected,
                )
                spread_order.spot_leg.status = LegStatus.FILLED
                spread_order.spot_leg.filled_qty = expected
                # Reconcile-skip: we missed the fill but the position IS flat,
                # so an order this cycle must have filled. POST_ONLY fills at
                # the posted price, so the last target_price is the best
                # available approximation. Falling back to 0.0 here would
                # make _close_position compute exit_spread = -β × 0 = 0,
                # producing a wildly wrong P&L (trade 30 hit this: -$24
                # logged on a +$0.65 actual trade).
                spread_order.spot_leg.filled_price = (
                    spread_order.spot_leg.target_price or 0.0
                )
                if spread_order.spot_leg.filled_price > 0:
                    logger.warning(
                        "Exit reconcile: spot fill price not detected — "
                        "approximating with last quoted price %.4f for P&L",
                        spread_order.spot_leg.filled_price,
                    )
                else:
                    logger.warning(
                        "Exit reconcile: spot fill price unknown and no "
                        "quoted price available — P&L will use mid placeholder",
                    )
            elif spot_qty < expected * partial_threshold:
                logger.warning(
                    "Exit reconcile: spot position partial (have=%.6f, expected=%.6f) — "
                    "scaling exit qty down",
                    spot_qty, expected,
                )
                spread_order.spot_leg.quantity = spot_qty
        except Exception as e:
            logger.error(
                "Exit reconcile: spot position lookup failed (%s) — proceeding with full exit qty",
                e,
            )

        # ---- futures leg ----
        # SWAP position quantities are in CONTRACTS; the leg quantity is in BTC.
        # Convert contracts → BTC via ctVal so we compare like-for-like.
        try:
            fut_positions = await self.futures_adapter.get_positions(spread_order.futures_leg.symbol)
            fut_contracts = next(
                (p.quantity for p in fut_positions
                 if p.symbol == spread_order.futures_leg.symbol and p.side == expected_fut_side),
                0.0,
            )
            info = await self.futures_adapter.get_symbol_info(spread_order.futures_leg.symbol)
            ct_val = float(info.get("contract_val") or 0) if info else 0.0
            if ct_val <= 0:
                logger.warning(
                    "Exit reconcile: missing contract_val for %s — cannot compare position "
                    "to leg quantity, skipping futures reconcile",
                    spread_order.futures_leg.symbol,
                )
                return
            fut_qty_btc = fut_contracts * ct_val
            expected = spread_order.futures_leg.quantity
            if fut_qty_btc < expected * flat_threshold:
                logger.warning(
                    "Exit reconcile: futures %s position is flat on exchange "
                    "(have=%.6f BTC / %.0f contracts, expected=%.6f BTC) — "
                    "skipping futures leg placement",
                    expected_fut_side, fut_qty_btc, fut_contracts, expected,
                )
                spread_order.futures_leg.status = LegStatus.FILLED
                spread_order.futures_leg.filled_qty = expected
                # See spot-side comment above re: target_price fallback.
                spread_order.futures_leg.filled_price = (
                    spread_order.futures_leg.target_price or 0.0
                )
                if spread_order.futures_leg.filled_price > 0:
                    logger.warning(
                        "Exit reconcile: futures fill price not detected — "
                        "approximating with last quoted price %.4f for P&L",
                        spread_order.futures_leg.filled_price,
                    )
                else:
                    logger.warning(
                        "Exit reconcile: futures fill price unknown and no "
                        "quoted price available — P&L will use mid placeholder",
                    )
            elif fut_qty_btc < expected * partial_threshold:
                logger.warning(
                    "Exit reconcile: futures position partial (have=%.6f BTC, expected=%.6f BTC) — "
                    "scaling exit qty down",
                    fut_qty_btc, expected,
                )
                spread_order.futures_leg.quantity = fut_qty_btc
        except Exception as e:
            logger.error(
                "Exit reconcile: futures position lookup failed (%s) — proceeding with full exit qty",
                e,
            )

        if spread_order.is_complete:
            logger.info(
                "Exit reconcile: both legs already flat on exchange — trade already closed, "
                "no orders needed",
            )

    async def _execute_spread(
        self,
        spread_order: SpreadOrder,
        spot_tick: MarketTick,
        futures_tick: MarketTick,
    ) -> SpreadOrder:
        """Execute a spread order using configured mode (different for entry vs exit)."""
        self._executing = True
        self.active_order = spread_order

        try:
            # Use different execution modes for entries vs exits
            # This allows maker fees on entries and fast execution on exits
            if spread_order.is_entry:
                execution_mode = getattr(self.config, 'entry_execution_mode', self.config.order_execution_mode)
            else:
                execution_mode = getattr(self.config, 'exit_execution_mode', self.config.order_execution_mode)

            if execution_mode == "MARKET":
                spot_mid = spot_tick.mid if spot_tick else 0.0
                return await self._execute_market(spread_order, spot_mid=spot_mid)
            else:
                return await self._execute_limit(spread_order, spot_tick, futures_tick)
        finally:
            self._executing = False
            self.active_order = None

    async def _execute_market(
        self,
        spread_order: SpreadOrder,
        spot_mid: float = 0.0,
    ) -> SpreadOrder:
        """Execute spread using market orders (immediate fill)."""
        logger.info("Executing spread with MARKET orders: %s %s",
                    spread_order.position_type,
                    "ENTRY" if spread_order.is_entry else "EXIT")

        # Cross-margin SPOT MARKET BUY: OKX reads sz as USDT when ccy=USDT is set.
        # Compute notional so the adapter can override sz with the correct USDT amount.
        spot_notional = (
            round(spread_order.spot_leg.quantity * spot_mid, 2)
            if spread_order.spot_leg.side == "BUY" and spot_mid > 0
            else None
        )

        # Honour the pre-flight reconcile: don't re-place a leg the exchange
        # says is already flat (that's the bug that double-sold the spot).
        spot_skip = spread_order.spot_leg.status == LegStatus.FILLED
        futures_skip = spread_order.futures_leg.status == LegStatus.FILLED

        if spot_skip and futures_skip:
            logger.info("Reconcile-skip: both legs already FILLED, no orders to place")
            if self.on_fill:
                self.on_fill(spread_order)
            return spread_order

        # Execute both legs simultaneously (or just one if the other is pre-filled)
        async def _already_done():
            return None  # placeholder so asyncio.gather stays symmetric

        spot_task = (
            _already_done() if spot_skip
            else self._place_market_order(
                self.spot_adapter, spread_order.spot_leg, notional_usdt=spot_notional,
            )
        )
        futures_task = (
            _already_done() if futures_skip
            else self._place_market_order(self.futures_adapter, spread_order.futures_leg)
        )

        spot_result, futures_result = await asyncio.gather(
            spot_task, futures_task, return_exceptions=True
        )

        # Process results (skipped legs stay FILLED from reconcile)
        if not spot_skip:
            if isinstance(spot_result, Exception):
                spread_order.spot_leg.status = LegStatus.FAILED
                logger.error("Spot leg failed: %s", spot_result)
            else:
                self._update_leg_from_result(spread_order.spot_leg, spot_result)
        else:
            logger.info("Reconcile-skip: spot leg already FILLED, did not place market order")

        if not futures_skip:
            if isinstance(futures_result, Exception):
                spread_order.futures_leg.status = LegStatus.FAILED
                logger.error("Futures leg failed: %s", futures_result)
            else:
                self._update_leg_from_result(spread_order.futures_leg, futures_result)
        else:
            logger.info("Reconcile-skip: futures leg already FILLED, did not place market order")

        # Handle partial fills (leg risk)
        if spread_order.has_partial_fill:
            logger.warning("PARTIAL FILL - Leg risk detected!")
            if self.on_partial_fill:
                self.on_partial_fill(spread_order)
            # Try to recover by market-closing the filled leg
            await self._handle_leg_risk(spread_order)

        if spread_order.is_complete and self.on_fill:
            self.on_fill(spread_order)

        return spread_order

    async def _execute_limit(
        self,
        spread_order: SpreadOrder,
        spot_tick: MarketTick,
        futures_tick: MarketTick,
    ) -> SpreadOrder:
        """Execute spread using pegged limit orders."""
        timeout_sec = self.config.limit_order_timeout_sec
        logger.info("LIMIT EXECUTION START: %s %s, timeout=%ds, spot_qty=%.6f, fut_qty=%.6f",
                    spread_order.position_type,
                    "ENTRY" if spread_order.is_entry else "EXIT",
                    timeout_sec,
                    spread_order.spot_leg.quantity,
                    spread_order.futures_leg.quantity)

        # Calculate initial prices
        self._update_target_prices(spread_order, spot_tick, futures_tick)

        # Place initial limit orders, then poll once shortly after so an
        # instant-fill (market-crossing limit, deep book) is registered
        # before the next status tick.
        await self._place_limit_orders(spread_order)
        await asyncio.sleep(self.POST_PLACE_POLL_DELAY_MS / 1000)
        await self._check_order_status(spread_order)

        poll_interval_sec = self.STATUS_POLL_INTERVAL_MS / 1000
        amend_interval_sec = self.PRICE_UPDATE_INTERVAL_MS / 1000
        last_amend_check = datetime.utcnow()

        # Monitor and adjust until filled or timeout. Status polls run on the
        # fast cadence; price re-quotes only on the slower amend cadence so we
        # don't thrash the exchange with cancel/replace cycles.
        while not spread_order.is_complete and not spread_order.is_failed:
            # Check timeout
            if datetime.utcnow() >= spread_order.timeout_at:
                logger.warning("Limit order timeout reached")
                await self._handle_timeout(spread_order)
                if self.on_timeout:
                    self.on_timeout(spread_order)
                break

            await asyncio.sleep(poll_interval_sec)

            # Always poll fill status on the fast cadence
            await self._check_order_status(spread_order)
            if spread_order.is_complete or spread_order.is_failed:
                break

            # Re-quote prices on the slower amend cadence only
            now = datetime.utcnow()
            if (now - last_amend_check).total_seconds() < amend_interval_sec:
                continue
            last_amend_check = now

            new_spot_tick = await self.spot_adapter.get_tick(self.config.spot_symbol)
            new_futures_tick = await self.futures_adapter.get_tick(self.config.futures_symbol)

            if not new_spot_tick or not new_futures_tick:
                continue

            self._update_target_prices(spread_order, new_spot_tick, new_futures_tick)

            # Amend orders if the *current target* has drifted more than 1 bp
            # from the price actually resting on the exchange (placed_price).
            # Previously this compared the previous CYCLE's target to the new
            # target — which a slow market move could evade entirely: each
            # 2-second cycle's drift stays under threshold while cumulative
            # drift across 50 seconds is large. The result was an order
            # sitting unfilled the whole way to timeout, then leg-risk on
            # entry. Using placed_price as the anchor catches that cumulative
            # drift the moment it crosses the threshold.
            spot_placed = spread_order.spot_leg.placed_price
            fut_placed  = spread_order.futures_leg.placed_price
            spot_change_pct = (
                abs(spread_order.spot_leg.target_price - spot_placed) / spot_placed
                if spot_placed else 0
            )
            futures_change_pct = (
                abs(spread_order.futures_leg.target_price - fut_placed) / fut_placed
                if fut_placed else 0
            )
            amend_threshold = 0.0001  # 0.01% = 1 basis point (~$6.50 on BTC)

            if spot_change_pct > amend_threshold or futures_change_pct > amend_threshold:
                await self._amend_limit_orders(spread_order)
                # Re-poll immediately after amend so a fast fill on the new
                # order doesn't have to wait a full poll cycle to be noticed.
                await asyncio.sleep(self.POST_PLACE_POLL_DELAY_MS / 1000)
                await self._check_order_status(spread_order)

        # Handle partial fills or orphan risk (one leg filled, other cancelled/failed)
        if spread_order.has_partial_fill or spread_order.has_orphan_risk:
            if spread_order.has_orphan_risk:
                logger.error("ORPHAN RISK: One leg filled while other was cancelled/failed!")
            else:
                logger.warning("PARTIAL FILL after limit execution - Leg risk!")
            await self._handle_leg_risk(spread_order)

        if spread_order.is_complete and self.on_fill:
            self.on_fill(spread_order)

        return spread_order

    def _update_target_prices(
        self,
        spread_order: SpreadOrder,
        spot_tick: MarketTick,
        futures_tick: MarketTick,
    ) -> None:
        """
        Calculate target prices for POST_ONLY limit orders.

        For MAKER orders:
        - BUY: place at bid + offset, but NEVER >= ask (POST_ONLY rejection)
        - SELL: place at ask - offset, but NEVER <= bid (POST_ONLY rejection)

        Safety cap prevents the price from crossing the spread which would
        cause OKX to immediately cancel the POST_ONLY order, creating orphans.

        offset = 0: exactly at bid/ask (most passive, safest for POST_ONLY)
        offset = 1-2 bps: slightly better price, still maker if spread is wider
        """
        offset_bps = self.config.limit_order_price_offset_bps / 10000
        # Safety buffer: keep price 1.0 bp away from the opposite side. Widened
        # from 0.5 bp when the executor switched to POST_ONLY — a tighter cap
        # let the price land too close to the touch on fast books, causing OKX
        # to reject ~5-10% of orders for would-have-crossed. The reprice loop
        # handles those, but a 1.0 bp buffer cuts rejections back to noise.
        SAFETY_BUFFER_BPS = 1.0 / 10000

        if spread_order.spot_leg.side == "BUY":
            target = spot_tick.bid * (1 + offset_bps)
            # Cap below ask to guarantee maker fill (prevents POST_ONLY rejection)
            max_price = spot_tick.ask * (1 - SAFETY_BUFFER_BPS)
            spread_order.spot_leg.target_price = round(min(target, max_price), 2)
        else:
            target = spot_tick.ask * (1 - offset_bps)
            # Cap above bid to guarantee maker fill (prevents POST_ONLY rejection)
            min_price = spot_tick.bid * (1 + SAFETY_BUFFER_BPS)
            spread_order.spot_leg.target_price = round(max(target, min_price), 2)

        if spread_order.futures_leg.side == "BUY":
            target = futures_tick.bid * (1 + offset_bps)
            max_price = futures_tick.ask * (1 - SAFETY_BUFFER_BPS)
            spread_order.futures_leg.target_price = round(min(target, max_price), 2)
        else:
            target = futures_tick.ask * (1 - offset_bps)
            min_price = futures_tick.bid * (1 + SAFETY_BUFFER_BPS)
            spread_order.futures_leg.target_price = round(max(target, min_price), 2)

    async def _place_market_order(
        self,
        adapter: ExchangeAdapter,
        leg: LegOrder,
        notional_usdt: Optional[float] = None,
    ) -> OrderResult:
        """Place a market order for a single leg."""
        return await adapter.place_order(
            symbol=leg.symbol,
            side=leg.side,
            order_type="MARKET",
            quantity=leg.quantity,
            pos_side=leg.pos_side,  # For OKX long_short_mode
            notional_usdt=notional_usdt,
        )

    async def _place_limit_orders(self, spread_order: SpreadOrder) -> None:
        """
        Place initial limit orders for both legs.

        Uses regular LIMIT orders with passive prices (at/near best bid/ask).
        This achieves maker fills without the POST_ONLY cancellation risk in tight spreads.

        Note: POST_ONLY was causing issues - OKX cancels immediately if price would
        cross the spread, which happens often in tight BTC markets.
        """
        # Use regular LIMIT orders - prices are already calculated to be passive
        # (at best bid for BUY, best ask for SELL) which should achieve maker fills
        # POST_ONLY = limit order that OKX cancels if it would fill immediately,
        # guaranteeing the maker fee (3.4× cheaper at VIP4 dated futures: 0.8
        # bps maker vs 2.7 bps taker). Plain "limit" at the bid/ask races the
        # order book — by the time OKX receives the order the book has often
        # moved and the order matches as a taker. POST_ONLY trades a small risk
        # of rejection (the reprice loop handles it on the next interval) for
        # guaranteed maker fills on every order that does land.
        order_type = "POST_ONLY"

        # Skip legs already marked FILLED by the pre-flight reconcile — those
        # are positions the exchange says we no longer hold, so placing a fresh
        # order would open a new naked position in the wrong direction.
        spot_skip = spread_order.spot_leg.status == LegStatus.FILLED
        futures_skip = spread_order.futures_leg.status == LegStatus.FILLED

        # Place spot limit order first (unless reconcile says it's already done)
        if not spot_skip:
            spot_result = await self.spot_adapter.place_order(
                symbol=spread_order.spot_leg.symbol,
                side=spread_order.spot_leg.side,
                order_type=order_type,
                quantity=spread_order.spot_leg.quantity,
                price=spread_order.spot_leg.target_price,
                pos_side=spread_order.spot_leg.pos_side,
            )

            if spot_result.success:
                spread_order.spot_leg.order_id = spot_result.order_id
                spread_order.spot_leg.placed_price = spread_order.spot_leg.target_price
                spread_order.spot_leg.status = LegStatus.OPEN
                logger.info("Placed spot LIMIT order: %s @ %.2f",
                           spread_order.spot_leg.side, spread_order.spot_leg.target_price)
            else:
                spread_order.spot_leg.status = LegStatus.FAILED
                logger.error("Failed to place spot limit order: %s", spot_result.error)
                # Don't place futures if spot failed immediately
                return
        else:
            logger.info("Reconcile-skip: spot leg already FILLED, not placing")

        if futures_skip:
            logger.info("Reconcile-skip: futures leg already FILLED, not placing")
            # Brief delay then check status of whatever we did place
            await asyncio.sleep(0.1)
            await self._check_order_status(spread_order)
            return

        # Place futures limit order
        futures_result = await self.futures_adapter.place_order(
            symbol=spread_order.futures_leg.symbol,
            side=spread_order.futures_leg.side,
            order_type=order_type,
            quantity=spread_order.futures_leg.quantity,
            price=spread_order.futures_leg.target_price,
            pos_side=spread_order.futures_leg.pos_side,  # CRITICAL for OKX long_short_mode
        )

        if futures_result.success:
            spread_order.futures_leg.order_id = futures_result.order_id
            spread_order.futures_leg.placed_price = spread_order.futures_leg.target_price
            spread_order.futures_leg.status = LegStatus.OPEN
            logger.info("Placed futures LIMIT order: %s @ %.2f",
                       spread_order.futures_leg.side, spread_order.futures_leg.target_price)
        elif getattr(futures_result, 'already_flat', False) and not spread_order.is_entry:
            # OKX 51169: futures position already closed on exchange (e.g. filled during a
            # previous timeout). Treat the futures leg as already handled and close spot only.
            logger.warning(
                "Futures position already flat on exchange (51169) during EXIT — "
                "marking futures leg done and closing spot leg only"
            )
            spread_order.futures_leg.status = LegStatus.FILLED
            spread_order.futures_leg.filled_qty = spread_order.futures_leg.quantity
            spread_order.futures_leg.filled_price = 0.0
            # Do NOT return — let the loop continue waiting for spot to fill
        else:
            spread_order.futures_leg.status = LegStatus.FAILED
            logger.error("Failed to place futures limit order: %s", futures_result.error)
            # Futures failed - cancel spot to prevent orphan (only if we placed one)
            if not spot_skip and spread_order.spot_leg.order_id:
                logger.warning("Cancelling spot order since futures placement failed")
                try:
                    await self.spot_adapter.cancel_order(
                        spread_order.spot_leg.symbol,
                        spread_order.spot_leg.order_id,
                    )
                    spread_order.spot_leg.status = LegStatus.CANCELLED
                except Exception as e:
                    logger.error("Failed to cancel spot after futures failure: %s", e)
            return

        # Brief delay then check status (LIMIT orders won't auto-cancel like POST_ONLY)
        await asyncio.sleep(0.1)
        await self._check_order_status(spread_order)

    async def _amend_limit_orders(self, spread_order: SpreadOrder) -> None:
        """
        Amend (update price of) existing limit orders.

        IMPORTANT: Only place new order if cancel succeeds to prevent duplicate orders.
        """
        # Amend spot order if still open
        if spread_order.spot_leg.status == LegStatus.OPEN:
            try:
                # First check if already filled before attempting cancel
                status = await self.spot_adapter.get_order_status(
                    spread_order.spot_leg.symbol,
                    spread_order.spot_leg.order_id
                )
                if status and status["state"] == "filled":
                    spread_order.spot_leg.status = LegStatus.FILLED
                    spread_order.spot_leg.filled_qty = status["filled_qty"]
                    spread_order.spot_leg.filled_price = status["filled_price"]
                    logger.info("Spot leg already filled during amend check")
                elif status and status["state"] in ("live", "partially_filled"):
                    # Cancel and replace with LIMIT order
                    cancel_success = await self.spot_adapter.cancel_order(
                        spread_order.spot_leg.symbol,
                        spread_order.spot_leg.order_id,
                    )
                    if cancel_success:
                        remaining_qty = spread_order.spot_leg.quantity - spread_order.spot_leg.filled_qty
                        if remaining_qty > 0:
                            result = await self.spot_adapter.place_order(
                                symbol=spread_order.spot_leg.symbol,
                                side=spread_order.spot_leg.side,
                                order_type="POST_ONLY",  # guaranteed maker (reprice on rejection)
                                quantity=remaining_qty,
                                price=spread_order.spot_leg.target_price,
                                pos_side=spread_order.spot_leg.pos_side,
                            )
                            if result.success:
                                spread_order.spot_leg.order_id = result.order_id
                                spread_order.spot_leg.placed_price = spread_order.spot_leg.target_price
                                logger.debug("Spot order amended: new_id=%s, price=%.2f",
                                           result.order_id, spread_order.spot_leg.target_price)
                            else:
                                logger.error("Failed to place new spot order after cancel: %s", result.error)
                                spread_order.spot_leg.status = LegStatus.FAILED
                    else:
                        logger.warning("Failed to cancel spot order for amend - skipping to avoid duplicates")
            except Exception as e:
                logger.error("Failed to amend spot order: %s", e)

        # Amend futures order if still open
        if spread_order.futures_leg.status == LegStatus.OPEN:
            try:
                # First check if already filled before attempting cancel
                status = await self.futures_adapter.get_order_status(
                    spread_order.futures_leg.symbol,
                    spread_order.futures_leg.order_id
                )
                if status and status["state"] == "filled":
                    spread_order.futures_leg.status = LegStatus.FILLED
                    spread_order.futures_leg.filled_qty = status["filled_qty"]
                    spread_order.futures_leg.filled_price = status["filled_price"]
                    logger.info("Futures leg already filled during amend check")
                elif status and status["state"] in ("live", "partially_filled"):
                    # Cancel and replace with LIMIT order
                    cancel_success = await self.futures_adapter.cancel_order(
                        spread_order.futures_leg.symbol,
                        spread_order.futures_leg.order_id,
                    )
                    if cancel_success:
                        remaining_qty = spread_order.futures_leg.quantity - spread_order.futures_leg.filled_qty
                        if remaining_qty > 0:
                            result = await self.futures_adapter.place_order(
                                symbol=spread_order.futures_leg.symbol,
                                side=spread_order.futures_leg.side,
                                order_type="POST_ONLY",  # guaranteed maker (reprice on rejection)
                                quantity=remaining_qty,
                                price=spread_order.futures_leg.target_price,
                                pos_side=spread_order.futures_leg.pos_side,
                            )
                            if result.success:
                                spread_order.futures_leg.order_id = result.order_id
                                spread_order.futures_leg.placed_price = spread_order.futures_leg.target_price
                                logger.debug("Futures order amended: new_id=%s, price=%.2f",
                                           result.order_id, spread_order.futures_leg.target_price)
                            else:
                                logger.error("Failed to place new futures order after cancel: %s", result.error)
                                spread_order.futures_leg.status = LegStatus.FAILED
                    else:
                        logger.warning("Failed to cancel futures order for amend - skipping to avoid duplicates")
            except Exception as e:
                logger.error("Failed to amend futures order: %s", e)

    # Map of OKX cancelSource codes → human-readable explanations.
    # Source: OKX docs and observed behaviour. The 20/21/13 codes are the
    # ones we hit in practice; everything else routes to "exchange/system".
    _OKX_CANCEL_REASONS = {
        "0":  "user initiated",
        "1":  "system cancelled",
        "2":  "not matched and cancelled",
        "13": "price limit breach",
        "17": "IOC unfilled portion",
        "20": "POST_ONLY would have crossed the book — quoted price was too aggressive",
        "21": "self-trade prevented",
        "31": "trigger-order limit",
    }

    @classmethod
    def _explain_cancel(cls, status: Dict[str, Any]) -> str:
        """Translate an OKX cancelled-order status into a human-readable reason."""
        code = str(status.get("cancel_source") or "")
        text = (status.get("cancel_source_reason") or "").strip()
        if code and code in cls._OKX_CANCEL_REASONS:
            return f"{cls._OKX_CANCEL_REASONS[code]} (cancelSource={code})"
        if text:
            return f"{text} (cancelSource={code or 'n/a'})"
        if code:
            return f"exchange cancelled (cancelSource={code})"
        return "exchange cancelled (no reason supplied)"

    async def _check_order_status(self, spread_order: SpreadOrder) -> None:
        """Check the fill status of both legs by querying the exchange."""
        # Check spot leg
        if spread_order.spot_leg.status == LegStatus.OPEN and spread_order.spot_leg.order_id:
            try:
                status = await self.spot_adapter.get_order_status(
                    spread_order.spot_leg.symbol,
                    spread_order.spot_leg.order_id
                )
                if status:
                    if status["state"] == "filled":
                        spread_order.spot_leg.status = LegStatus.FILLED
                        spread_order.spot_leg.filled_qty = status["filled_qty"]
                        spread_order.spot_leg.filled_price = status["filled_price"]
                        logger.info("Spot leg filled: qty=%.6f @ %.2f",
                                   status["filled_qty"], status["filled_price"])
                    elif status["state"] == "partially_filled":
                        spread_order.spot_leg.status = LegStatus.PARTIAL
                        spread_order.spot_leg.filled_qty = status["filled_qty"]
                        spread_order.spot_leg.filled_price = status["filled_price"]
                    elif status["state"] == "canceled":
                        spread_order.spot_leg.status = LegStatus.CANCELLED
                        logger.warning(
                            "Spot leg cancelled: %s",
                            self._explain_cancel(status),
                        )
            except Exception as e:
                logger.error("Error checking spot order status: %s", e)

        # Check futures leg
        if spread_order.futures_leg.status == LegStatus.OPEN and spread_order.futures_leg.order_id:
            try:
                status = await self.futures_adapter.get_order_status(
                    spread_order.futures_leg.symbol,
                    spread_order.futures_leg.order_id
                )
                if status:
                    if status["state"] == "filled":
                        spread_order.futures_leg.status = LegStatus.FILLED
                        spread_order.futures_leg.filled_qty = status["filled_qty"]
                        spread_order.futures_leg.filled_price = status["filled_price"]
                        logger.info("Futures leg filled: qty=%.6f @ %.2f",
                                   status["filled_qty"], status["filled_price"])
                    elif status["state"] == "partially_filled":
                        spread_order.futures_leg.status = LegStatus.PARTIAL
                        spread_order.futures_leg.filled_qty = status["filled_qty"]
                        spread_order.futures_leg.filled_price = status["filled_price"]
                    elif status["state"] == "canceled":
                        spread_order.futures_leg.status = LegStatus.CANCELLED
                        logger.warning(
                            "Futures leg cancelled: %s",
                            self._explain_cancel(status),
                        )
            except Exception as e:
                logger.error("Error checking futures order status: %s", e)

        # CRITICAL: If one leg is cancelled and other is still OPEN, cancel the other immediately
        # This prevents orphan positions from POST_ONLY rejections
        await self._cancel_if_one_leg_failed(spread_order)

    async def _cancel_if_one_leg_failed(self, spread_order: SpreadOrder) -> None:
        """
        Cancel the remaining open leg if one leg was cancelled/failed.

        This is CRITICAL for preventing orphan positions when POST_ONLY orders
        are rejected by the exchange (cancelled because they would cross the spread).
        """
        failed_states = (LegStatus.FAILED, LegStatus.CANCELLED)

        # If spot failed but futures is still open, cancel futures
        if spread_order.spot_leg.status in failed_states and spread_order.futures_leg.status == LegStatus.OPEN:
            logger.warning("Spot leg failed/cancelled - cancelling futures leg to prevent orphan")
            try:
                cancelled = await self.futures_adapter.cancel_order(
                    spread_order.futures_leg.symbol,
                    spread_order.futures_leg.order_id,
                )
                if cancelled:
                    spread_order.futures_leg.status = LegStatus.CANCELLED
                    logger.info("Futures leg cancelled successfully - no orphan")
                else:
                    # Check if it filled while we tried to cancel
                    status = await self.futures_adapter.get_order_status(
                        spread_order.futures_leg.symbol,
                        spread_order.futures_leg.order_id
                    )
                    if status and status["state"] == "filled":
                        spread_order.futures_leg.status = LegStatus.FILLED
                        spread_order.futures_leg.filled_qty = status["filled_qty"]
                        spread_order.futures_leg.filled_price = status["filled_price"]
                        logger.error("ORPHAN: Futures filled while spot was cancelled - LEG RISK!")
            except Exception as e:
                logger.error("Error cancelling futures after spot failure: %s", e)

        # If futures failed but spot is still open, cancel spot
        if spread_order.futures_leg.status in failed_states and spread_order.spot_leg.status == LegStatus.OPEN:
            logger.warning("Futures leg failed/cancelled - cancelling spot leg to prevent orphan")
            try:
                cancelled = await self.spot_adapter.cancel_order(
                    spread_order.spot_leg.symbol,
                    spread_order.spot_leg.order_id,
                )
                if cancelled:
                    spread_order.spot_leg.status = LegStatus.CANCELLED
                    logger.info("Spot leg cancelled successfully - no orphan")
                else:
                    # Check if it filled while we tried to cancel
                    status = await self.spot_adapter.get_order_status(
                        spread_order.spot_leg.symbol,
                        spread_order.spot_leg.order_id
                    )
                    if status and status["state"] == "filled":
                        spread_order.spot_leg.status = LegStatus.FILLED
                        spread_order.spot_leg.filled_qty = status["filled_qty"]
                        spread_order.spot_leg.filled_price = status["filled_price"]
                        logger.error("ORPHAN: Spot filled while futures was cancelled - LEG RISK!")
            except Exception as e:
                logger.error("Error cancelling spot after futures failure: %s", e)

    async def _handle_timeout(self, spread_order: SpreadOrder) -> None:
        """Handle timeout - cancel unfilled orders and close any partial fills."""
        logger.debug("Handling limit order timeout")

        # Cancel any open orders
        if spread_order.spot_leg.status == LegStatus.OPEN:
            try:
                await self.spot_adapter.cancel_order(
                    spread_order.spot_leg.symbol,
                    spread_order.spot_leg.order_id,
                )
                spread_order.spot_leg.status = LegStatus.CANCELLED
            except Exception as e:
                logger.error("Failed to cancel spot order: %s", e)

        if spread_order.futures_leg.status == LegStatus.OPEN:
            try:
                await self.futures_adapter.cancel_order(
                    spread_order.futures_leg.symbol,
                    spread_order.futures_leg.order_id,
                )
                spread_order.futures_leg.status = LegStatus.CANCELLED
            except Exception as e:
                logger.error("Failed to cancel futures order: %s", e)

        # Handle partial fills
        if spread_order.has_partial_fill:
            await self._handle_leg_risk(spread_order)

    async def _handle_leg_risk(self, spread_order: SpreadOrder) -> None:
        """
        Handle leg risk when one leg is filled but the other isn't.

        Strategy (fee-aware):
        1. Try to fill the missing leg with a LIMIT order (maker fees, no slippage)
        2. Progressively move price toward market if unfilled
        3. Only use market order as absolute last resort after timeout

        This avoids paying taker fees + slippage to close what could be
        recovered as a complete spread trade at maker rates.
        """
        filled_states = (LegStatus.FILLED, LegStatus.PARTIAL)

        spot_filled = spread_order.spot_leg.status in filled_states and spread_order.spot_leg.filled_qty > 0
        futures_filled = spread_order.futures_leg.status in filled_states and spread_order.futures_leg.filled_qty > 0

        if spot_filled and not futures_filled:
            logger.warning("Orphan SPOT filled - attempting LIMIT recovery for futures leg")
            recovered = await self._attempt_maker_recovery(
                adapter=self.futures_adapter,
                leg=spread_order.futures_leg,
                label="futures",
                recovery_timeout_sec=getattr(self.config, 'orphan_recovery_timeout_sec', 60),
            )
            if not recovered:
                # Last resort: close the spot leg at market
                logger.error("Futures recovery failed - closing spot orphan at MARKET (taker fees apply)")
                close_side = "SELL" if spread_order.spot_leg.side == "BUY" else "BUY"
                # Cross-margin SPOT MARKET BUY needs notional_usdt (sz must be in USDT)
                spot_notional = None
                if close_side == "BUY":
                    try:
                        spot_tick = await self.spot_adapter.get_tick(spread_order.spot_leg.symbol)
                        if spot_tick:
                            spot_notional = round(spread_order.spot_leg.filled_qty * spot_tick.mid, 2)
                    except Exception as e:
                        logger.warning("Could not fetch spot tick for notional calc: %s", e)
                result = await self.spot_adapter.place_order(
                    symbol=spread_order.spot_leg.symbol,
                    side=close_side,
                    order_type="MARKET",
                    quantity=spread_order.spot_leg.filled_qty,
                    notional_usdt=spot_notional,
                )
                if result.success:
                    logger.info("Closed orphan spot leg at market: order_id=%s", result.order_id)
                else:
                    logger.error("CRITICAL: Failed to close orphan spot leg: %s", result.error)

        elif futures_filled and not spot_filled:
            logger.warning("Orphan FUTURES filled - attempting LIMIT recovery for spot leg")
            recovered = await self._attempt_maker_recovery(
                adapter=self.spot_adapter,
                leg=spread_order.spot_leg,
                label="spot",
                recovery_timeout_sec=getattr(self.config, 'orphan_recovery_timeout_sec', 60),
            )
            if not recovered:
                # Last resort: close the futures leg at market
                logger.error("Spot recovery failed - closing futures orphan at MARKET (taker fees apply)")
                close_side = "SELL" if spread_order.futures_leg.side == "BUY" else "BUY"
                result = await self.futures_adapter.place_order(
                    symbol=spread_order.futures_leg.symbol,
                    side=close_side,
                    order_type="MARKET",
                    quantity=spread_order.futures_leg.filled_qty,
                    pos_side=spread_order.futures_leg.pos_side,
                    reduce_only=True,
                )
                if result.success:
                    logger.info("Closed orphan futures leg at market: order_id=%s", result.order_id)
                else:
                    logger.error("CRITICAL: Failed to close orphan futures leg: %s", result.error)

        if self.on_error:
            self.on_error("Leg risk occurred - check positions for orphan state")

    async def _attempt_maker_recovery(
        self,
        adapter: ExchangeAdapter,
        leg: LegOrder,
        label: str,
        recovery_timeout_sec: int = 60,
        price_step_bps: float = 2.0,
        max_price_steps: int = 10,
    ) -> bool:
        """
        Attempt to fill an orphaned leg using LIMIT orders before falling back to market.

        Strategy:
        - Place a passive LIMIT order (not POST_ONLY, so it won't be auto-cancelled)
        - Check every second for fill
        - Every (timeout / max_steps) seconds, nudge price 1 bps closer to market
        - Return True if filled as maker, False if gave up (caller should market close)

        This preserves maker fees instead of paying taker fees + slippage.
        """
        logger.info("Starting maker recovery for %s leg: %s %.6f",
                    label, leg.side, leg.quantity)

        step_interval = recovery_timeout_sec / max_price_steps
        price_offset_bps = 0.0  # Start at best bid/ask, move toward market each step
        deadline = datetime.utcnow() + timedelta(seconds=recovery_timeout_sec)
        step_deadline = datetime.utcnow() + timedelta(seconds=step_interval)
        current_order_id = None

        try:
            # Get current market price for the leg
            tick = await adapter.get_tick(leg.symbol)
            if not tick:
                logger.error("Cannot get tick for %s during recovery", leg.symbol)
                return False

            # Calculate initial recovery price (passive - at best bid/ask)
            recovery_price = self._calc_recovery_price(leg.side, tick, price_offset_bps)

            # Place initial LIMIT order (not POST_ONLY - it won't get auto-cancelled)
            result = await adapter.place_order(
                symbol=leg.symbol,
                side=leg.side,
                order_type="LIMIT",
                quantity=leg.quantity,
                price=recovery_price,
                pos_side=leg.pos_side,
            )

            if not result.success:
                logger.error("Failed to place recovery LIMIT order for %s: %s", label, result.error)
                return False

            current_order_id = result.order_id
            logger.info("Recovery LIMIT order placed for %s: id=%s @ %.4f",
                       label, current_order_id, recovery_price)

            # Poll until filled or deadline
            while datetime.utcnow() < deadline:
                await asyncio.sleep(1.0)

                # Check fill status
                status = await adapter.get_order_status(leg.symbol, current_order_id)
                if status:
                    if status["state"] == "filled":
                        leg.status = LegStatus.FILLED
                        leg.filled_qty = status["filled_qty"]
                        leg.filled_price = status["filled_price"]
                        leg.order_id = current_order_id
                        logger.info("Recovery SUCCESS: %s leg filled as maker @ %.4f",
                                   label, leg.filled_price)
                        return True
                    elif status["state"] == "partially_filled":
                        leg.status = LegStatus.PARTIAL
                        leg.filled_qty = status["filled_qty"]
                        leg.filled_price = status["filled_price"]
                        # Continue waiting for full fill

                # Time to nudge the price closer to market?
                if datetime.utcnow() >= step_deadline and price_offset_bps < max_price_steps * price_step_bps:
                    price_offset_bps += price_step_bps
                    step_deadline = datetime.utcnow() + timedelta(seconds=step_interval)

                    # Get fresh tick
                    tick = await adapter.get_tick(leg.symbol)
                    if not tick:
                        continue

                    new_price = self._calc_recovery_price(leg.side, tick, price_offset_bps)
                    logger.info("Recovery: nudging %s price toward market: %.4f (offset=%.1f bps)",
                               label, new_price, price_offset_bps)

                    # Cancel current order and replace with better price
                    cancelled = await adapter.cancel_order(leg.symbol, current_order_id)
                    if cancelled:
                        remaining_qty = leg.quantity - leg.filled_qty
                        if remaining_qty > 0:
                            result = await adapter.place_order(
                                symbol=leg.symbol,
                                side=leg.side,
                                order_type="LIMIT",
                                quantity=remaining_qty,
                                price=new_price,
                                pos_side=leg.pos_side,
                            )
                            if result.success:
                                current_order_id = result.order_id
                                recovery_price = new_price
                            else:
                                logger.error("Failed to replace recovery order: %s", result.error)
                                return False

            # Timeout - cancel the recovery order
            logger.warning("Recovery timeout for %s after %ds - falling back to market",
                          label, recovery_timeout_sec)
            if current_order_id:
                await adapter.cancel_order(leg.symbol, current_order_id)

            return False

        except Exception as e:
            logger.exception("Error during maker recovery for %s: %s", label, e)
            # Try to cancel any open recovery order
            if current_order_id:
                try:
                    await adapter.cancel_order(leg.symbol, current_order_id)
                except Exception:
                    pass
            return False

    def _calc_recovery_price(self, side: str, tick: MarketTick, offset_bps: float) -> float:
        """
        Calculate a passive LIMIT price for recovery, optionally nudged toward market.

        offset_bps=0: exactly at best bid/ask (most passive, best fees)
        offset_bps=N: N bps closer to the other side of the spread
        Higher offset = higher fill probability but still maker (until it crosses spread)
        """
        offset = offset_bps / 10000
        if side == "BUY":
            # BUY: start at bid, nudge toward ask
            return round(tick.bid * (1 + offset), 2)
        else:
            # SELL: start at ask, nudge toward bid
            return round(tick.ask * (1 - offset), 2)

    def _update_leg_from_result(self, leg: LegOrder, result: OrderResult) -> None:
        """Update leg status from order result."""
        if result.success:
            leg.order_id = result.order_id
            leg.filled_qty = result.filled_qty
            leg.filled_price = result.filled_price
            leg.status = LegStatus.FILLED if result.filled_qty >= leg.quantity else LegStatus.PARTIAL
        else:
            leg.status = LegStatus.FAILED

        leg.last_update = datetime.utcnow()

    async def cancel_active_order(self) -> None:
        """Cancel any active order."""
        if self.active_order:
            await self._handle_timeout(self.active_order)
