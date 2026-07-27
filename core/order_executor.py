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
    throttled: bool = False  # True if a POST_ONLY rejection (cancelSource=31/20) occurred

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

    @property
    def has_size_imbalance(self) -> bool:
        """Both legs have fills but in mismatched SIZE — the hedge is broken.

        has_partial_fill (XOR of 'any fill') and has_orphan_risk (which needs a
        FAILED/CANCELLED leg) both MISS this: a leg FILLED while the other is
        only PARTIAL reads as 'both filled', so neither fires. Left unhandled it
        leaves a naked leg — live #136: spot FILLED (49), futures ~19% → −$18.57
        when the 60s orphan-guard finally market-flattened. Compares fill
        FRACTIONS so a normal complete fill (both ~100%) never trips it.
        """
        def _frac(leg) -> float:
            return (leg.filled_qty / leg.quantity) if leg.quantity else 0.0
        s, f = _frac(self.spot_leg), _frac(self.futures_leg)
        return max(s, f) > 0.10 and abs(s - f) > 0.10


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

        # RFQ executor — set via set_rfq_executor() when RFQ is configured.
        # None = order book always used regardless of notional size.
        self._rfq_executor = None
        # Cache of instrument info (ctVal) for base->contracts sizing of RFQ legs.
        self._ctval_cache: Dict[str, Dict[str, Any]] = {}
        # Live in-memory tick provider (set by the engine). Lets order pricing
        # read the freshest WS tick with ZERO latency instead of a REST get_tick
        # — the last REST hop in the hot path and the main remaining cause of
        # POST_ONLY (cancelSource=31) rejections.
        self._live_tick_provider: Optional[Callable[[str], Optional[MarketTick]]] = None
        self._live_tick_max_age_s: float = 2.0

    def update_config(self, config: TradingConfig) -> None:
        """Update configuration."""
        self.config = config
        if self._rfq_executor is not None:
            self._rfq_executor.update_config(config)

    def set_rfq_executor(self, rfq_executor) -> None:
        """Register the RFQ executor. Called from app.py after adapter setup."""
        self._rfq_executor = rfq_executor
        logger.info("[executor] RFQ executor registered (threshold=$%.0f)",
                    getattr(self.config, 'rfq_notional_threshold_usd', 0.0))

    def set_live_tick_provider(self, provider: Callable[[str], Optional[MarketTick]]) -> None:
        """Register a callable(symbol)->MarketTick returning the engine's latest
        WS-streamed tick, used to price limit legs with no REST round-trip."""
        self._live_tick_provider = provider

    async def _snapshot_tick(self, symbol: str, adapter: ExchangeAdapter) -> Optional[MarketTick]:
        """Freshest tick for pricing a leg: prefer the in-memory live WS tick
        (zero latency), fall back to a REST fetch only if it's missing or stale."""
        if self._live_tick_provider is not None:
            try:
                t = self._live_tick_provider(symbol)
                if t is not None:
                    ts = getattr(t, "timestamp", None)
                    age = (datetime.utcnow() - ts).total_seconds() if ts else 0.0
                    if age <= self._live_tick_max_age_s:
                        return t
            except Exception:
                pass
        try:
            return await adapter.get_tick(symbol)
        except Exception:
            return None

    async def _rfq_contracts(
        self, adapter: ExchangeAdapter, symbol: str, base_qty: float, price: float,
    ) -> Optional[int]:
        """Convert a base-currency quantity to integer OKX contracts for an RFQ leg.

        OKX RFQ `sz` is denominated in CONTRACTS for SWAP/FUTURES (identical to
        place_order), but leg.quantity is in base units — so the RFQ path MUST
        convert or it would request the wrong size (~10x off for ETH, ~100x for
        BTC). Mirrors OKXAdapter._prepare_order. Returns None if it can't size
        safely, so the caller falls back to the order book rather than guessing.
        """
        try:
            info = self._ctval_cache.get(symbol)
            if not info:
                info = await adapter.get_symbol_info(symbol)
                if info:
                    self._ctval_cache[symbol] = info
            if not info:
                return None
            if info.get("ct_type") == "inverse":
                ct_val_usd = float(info.get("ct_val_usd") or 0.0)
                if ct_val_usd <= 0 or not price or price <= 0:
                    return None
                contracts = int(base_qty * price / ct_val_usd)
            else:
                ct_val = float(info.get("contract_val") or 0.0)
                if ct_val <= 0:
                    return None
                contracts = int(base_qty / ct_val)
            return contracts if contracts >= 1 else None
        except Exception as e:
            logger.warning("[executor] RFQ contract sizing failed for %s: %s", symbol, e)
            return None

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
        force_market: bool = False,
        allow_rfq: bool = True,
    ) -> Optional[SpreadOrder]:
        """
        Execute exit trade to close a spread position.

        allow_rfq=False routes the exit to the order book even above the RFQ
        notional threshold — set by the engine for urgent stop exits.

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

        return await self._execute_spread(spread_order, spot_tick, futures_tick,
                                           force_market=force_market, allow_rfq=allow_rfq)

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
        force_market: bool = False,
        allow_rfq: bool = True,
    ) -> SpreadOrder:
        """Execute a spread order via RFQ (atomic) or order book, depending on notional.

        allow_rfq=False forces the order-book path even above the RFQ notional
        threshold — used for urgent (stop) exits, where waiting seconds for a
        maker quote is more dangerous than legging in on the book.
        """
        self._executing = True
        self.active_order = spread_order

        try:
            # ── RFQ routing ──────────────────────────────────────────────────
            # Route to atomic RFQ execution when per-leg notional >= threshold.
            # force_market or allow_rfq=False bypass RFQ (emergency / stop exits).
            rfq_threshold = getattr(self.config, 'rfq_notional_threshold_usd', 0.0)
            if not force_market and allow_rfq and rfq_threshold > 0 and spot_tick and futures_tick:
                spot_notional = spread_order.spot_leg.quantity * (spot_tick.mid or 0.0)
                fut_notional = spread_order.futures_leg.quantity * (futures_tick.mid or 0.0)
                per_leg_notional = max(spot_notional, fut_notional)

                if per_leg_notional >= rfq_threshold:
                    if self._rfq_executor is None:
                        logger.critical(
                            "[executor] NOTIONAL GUARD: per-leg $%.0f >= rfq_threshold $%.0f "
                            "but no RFQ executor configured. Proceeding on order book — "
                            "configure RFQ or reduce position size to eliminate legging risk.",
                            per_leg_notional, rfq_threshold,
                        )
                    else:
                        from core.rfq_executor import RFQResult
                        sl = spread_order.spot_leg
                        fl = spread_order.futures_leg
                        label = ("ENTRY" if spread_order.is_entry else "EXIT") + f" {spread_order.position_type}"
                        # OKX RFQ sz is in CONTRACTS — convert from base units first.
                        spot_contracts = await self._rfq_contracts(
                            self.spot_adapter, sl.symbol, sl.quantity, spot_tick.mid or 0.0)
                        fut_contracts = await self._rfq_contracts(
                            self.futures_adapter, fl.symbol, fl.quantity, futures_tick.mid or 0.0)
                        if spot_contracts is None or fut_contracts is None:
                            logger.error(
                                "[executor] RFQ contract sizing failed (spot=%s fut=%s) — "
                                "falling back to order book", spot_contracts, fut_contracts,
                            )
                            # Fall through to order-book path below (safe: nothing executed)
                        else:
                            rfq_result: RFQResult = await self._rfq_executor.execute_spread_rfq(
                                spot_symbol=sl.symbol,
                                futures_symbol=fl.symbol,
                                spot_side=sl.side,
                                futures_side=fl.side,
                                spot_qty=spot_contracts,    # CONTRACTS for OKX RFQ sz
                                futures_qty=fut_contracts,  # CONTRACTS for OKX RFQ sz
                                spot_pos_side=sl.pos_side,
                                futures_pos_side=fl.pos_side,
                                spot_tick=spot_tick,
                                futures_tick=futures_tick,
                                label=label,
                            )
                            if rfq_result.success:
                                # Populate SpreadOrder from atomic fills
                                sl.filled_price = rfq_result.spot_filled_price
                                sl.filled_qty = rfq_result.spot_filled_qty
                                sl.status = LegStatus.FILLED
                                fl.filled_price = rfq_result.futures_filled_price
                                fl.filled_qty = rfq_result.futures_filled_qty
                                fl.status = LegStatus.FILLED
                                logger.info(
                                    "[executor] RFQ atomic fill: %s @ %.4f | %s @ %.4f "
                                    "(tTradeId=%s)",
                                    sl.symbol, sl.filled_price,
                                    fl.symbol, fl.filled_price,
                                    rfq_result.trade_id,
                                )
                                return spread_order

                            # AMBIGUOUS execute (timeout) — outcome unknown. Do NOT fall
                            # back (would risk double-execution). Leave legs unfilled so
                            # the engine's position reconciler verifies and acts.
                            if rfq_result.ambiguous:
                                logger.critical(
                                    "[executor] RFQ execute AMBIGUOUS — NOT falling back; "
                                    "engine will reconcile positions: %s", rfq_result.error,
                                )
                                return spread_order  # legs not FILLED → treated as failure

                            # Clean RFQ failure (no quote / markup too high / create
                            # failed) — nothing executed, so order-book fallback is safe.
                            fallback = getattr(self.config, 'rfq_fallback_to_orderbook', True)
                            if not fallback:
                                logger.error(
                                    "[executor] RFQ failed and rfq_fallback_to_orderbook=False — "
                                    "aborting: %s", rfq_result.error,
                                )
                                return spread_order  # legs not FILLED → engine treats as failure
                            logger.warning(
                                "[executor] RFQ failed (%s) — falling back to order book",
                                rfq_result.error,
                            )
                            # Fall through to order-book path below

            # ── Order-book path ──────────────────────────────────────────────
            if force_market:
                execution_mode = "MARKET"
            elif spread_order.is_entry:
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

        # Parallel snap of both ticks to initialise target prices. The per-leg
        # snaps in _place_limit_orders will re-snap each leg again right before
        # its individual place_order call — this initial snap is just a warm-up
        # so _update_target_prices has reasonable prices from the start.
        _snap = await asyncio.gather(
            self.spot_adapter.get_tick(self.config.spot_symbol),
            self.futures_adapter.get_tick(self.config.futures_symbol),
            return_exceptions=True,
        )
        snap_spot    = _snap[0] if not isinstance(_snap[0], Exception) else None
        snap_futures = _snap[1] if not isinstance(_snap[1], Exception) else None
        if isinstance(_snap[0], Exception) or isinstance(_snap[1], Exception):
            logger.warning("Initial orderbook snap partial/failed — per-leg snap will correct before placement")

        self._update_target_prices(
            spread_order,
            snap_spot if snap_spot else spot_tick,
            snap_futures if snap_futures else futures_tick,
        )

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

            # Use a wider safety buffer on re-quotes after a POST_ONLY rejection.
            # The rejection means the market moved faster than our tick snapshot;
            # quoting 2 bp from the opposite touch (vs the normal 1 bp) gives
            # more headroom against the async gap on the next placement attempt.
            extra_buf = 1.0 if spread_order.throttled else 0.0
            self._update_target_prices(spread_order, new_spot_tick, new_futures_tick,
                                       extra_buffer_bps=extra_buf)

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

        # Final sanity refresh: catches the case where the legs both became
        # FILLED on the exchange after our last poll (matching-engine lag).
        # Without this, is_complete can stay False because a PARTIAL status
        # never got upgraded to FILLED, even though both legs are 100% filled
        # on the exchange. The engine then mis-reports the entry as a
        # failure and the orphan detector kills the position.
        if not spread_order.is_complete and not spread_order.is_failed:
            await self._refresh_partial_legs(spread_order)

        # Size-imbalance guard: both legs have fills but mismatched (e.g. spot
        # FILLED, futures only PARTIAL). has_partial_fill (XOR) and
        # has_orphan_risk (needs a FAILED leg) both miss it, so _handle_leg_risk
        # above never ran. Left as-is the filled leg is a naked position the
        # engine rejects and the 60s orphan-guard market-flattens at a loss
        # (#136). Flatten the residual to flat NOW — seconds, not a minute. The
        # not-(partial/orphan) guard keeps this from double-acting on a case
        # _handle_leg_risk already handled.
        if (not spread_order.is_complete and not spread_order.is_failed
                and not (spread_order.has_partial_fill or spread_order.has_orphan_risk)
                and spread_order.has_size_imbalance):
            sf = (spread_order.spot_leg.filled_qty / spread_order.spot_leg.quantity
                  if spread_order.spot_leg.quantity else 0.0)
            ff = (spread_order.futures_leg.filled_qty / spread_order.futures_leg.quantity
                  if spread_order.futures_leg.quantity else 0.0)
            logger.error(
                "SIZE IMBALANCE at entry timeout — hedge broken (spot %.0f%%, "
                "futures %.0f%% filled); flattening residual to flat to avoid a "
                "naked-leg orphan", sf * 100, ff * 100,
            )
            await self._flatten_residual_fills(spread_order)

        if spread_order.is_complete and self.on_fill:
            self.on_fill(spread_order)

        return spread_order

    def _price_leg(self, leg: LegOrder, tick: MarketTick, extra_buffer_bps: float = 0.0) -> None:
        """
        Compute and set target_price on a single leg from a live tick.

        BUY:  rest at/just inside the bid, kept a safety buffer clear of the ask.
        SELL: rest at/just inside the ask, kept a safety buffer clear of the bid.

        Two design points that fix the chronic cancelSource=31 rejections:
        - The resting price is FLOORED at the own-side touch, so a wide buffer on
          a tight book still rests ON the book (maker, fillable) instead of being
          pushed below the bid (or above the ask) where it never fills.
        - The buffer keeps the price away from the OPPOSITE touch so a stale/async
          tick can't cross it into a taker fill — the reject that orphaned one leg.
          The old hard-coded 1 bp is only ~$6 on BTC (less than one fast tick),
          so it was routinely crossed between price-snap and order arrival.
        extra_buffer_bps widens the gap further on the retry after a rejection.
        """
        offset_bps = self.config.limit_order_price_offset_bps / 10000.0
        # Default 3 bp (override via config.post_only_safety_buffer_bps if set).
        base_buf_bps = getattr(self.config, 'post_only_safety_buffer_bps', 0.0) or 3.0
        buf = (base_buf_bps + extra_buffer_bps) / 10000.0
        if leg.side == "BUY":
            target = min(tick.bid * (1 + offset_bps), tick.ask * (1 - buf))
            target = max(target, tick.bid)            # never below the bid (stay fillable)
            if target >= tick.ask:                    # crossed/locked tick — fall back
                target = tick.ask * (1 - buf)
            leg.target_price = round(target, 2)
        else:
            target = max(tick.ask * (1 - offset_bps), tick.bid * (1 + buf))
            target = min(target, tick.ask)            # never above the ask (stay fillable)
            if target <= tick.bid:                    # crossed/locked tick — fall back
                target = tick.bid * (1 + buf)
            leg.target_price = round(target, 2)

    def _update_target_prices(
        self,
        spread_order: SpreadOrder,
        spot_tick: MarketTick,
        futures_tick: MarketTick,
        extra_buffer_bps: float = 0.0,
    ) -> None:
        """
        Calculate target prices for POST_ONLY limit orders.

        For MAKER orders:
        - BUY: price at bid-level, capped safely below the ask
        - SELL: price at ask-level, floored safely above the bid

        The safety buffer keeps the resting price away from the opposite side
        of the spread to avoid POST_ONLY rejection. The main risk is the async
        gap: price is calculated from tick-at-T, but place_order is an await
        that takes ~50 ms. In volatile conditions BTC can move $10+ in that
        window, so the "safe" price at T may cross the new ask at T+50ms.

        extra_buffer_bps should be set to 1.0 after a POST_ONLY rejection so
        the retry rests further from the opposite touch, reducing re-rejection
        probability while the book is moving fast.
        """
        self._price_leg(spread_order.spot_leg, spot_tick, extra_buffer_bps)
        self._price_leg(spread_order.futures_leg, futures_tick, extra_buffer_bps)

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
        Place initial POST_ONLY limit orders for both legs.

        POST_ONLY = OKX cancels the order (cancelSource=31/20) if it would
        immediately match as a taker, guaranteeing the maker fee (0.8 bps vs
        2.7 bps taker at VIP4). The reprice loop handles any rejection on the
        next amend cycle; after one rejection the buffer widens to 2 bp so
        the retry rests further from the opposite touch.
        """
        order_type = "POST_ONLY"

        # Skip legs already marked FILLED by the pre-flight reconcile — those
        # are positions the exchange says we no longer hold, so placing a fresh
        # order would open a new naked position in the wrong direction.
        spot_skip = spread_order.spot_leg.status == LegStatus.FILLED
        futures_skip = spread_order.futures_leg.status == LegStatus.FILLED

        # Place spot limit order first (unless reconcile says it's already done)
        if not spot_skip:
            # Per-leg snap: fetch the freshest price right before this placement so
            # the order lands with a price that's only the HTTP round-trip stale (~50ms),
            # not the cumulative latency of both placements (~800ms). This is the fix for
            # cancelSource=31 rejections caused by stale prices at order arrival.
            try:
                _fresh_spot = await self._snapshot_tick(self.config.spot_symbol, self.spot_adapter)
                if _fresh_spot:
                    self._price_leg(spread_order.spot_leg, _fresh_spot,
                                    1.0 if spread_order.throttled else 0.0)
                    logger.debug("Per-leg spot snap: target_price=%.4f", spread_order.spot_leg.target_price)
            except Exception as _se:
                logger.debug("Per-leg spot snap failed (%s) — using existing target price", _se)

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

        # Per-leg futures snap: spot placement took ~600ms; re-snap futures now so
        # its price is only the current HTTP round-trip stale at placement time.
        try:
            _fresh_fut = await self._snapshot_tick(self.config.futures_symbol, self.futures_adapter)
            if _fresh_fut:
                self._price_leg(spread_order.futures_leg, _fresh_fut,
                                1.0 if spread_order.throttled else 0.0)
                logger.debug("Per-leg futures snap: target_price=%.4f", spread_order.futures_leg.target_price)
        except Exception as _fe:
            logger.debug("Per-leg futures snap failed (%s) — using existing target price", _fe)

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
                # Check fill status first
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
                    # Try native WS amend first; fall back to cancel+replace for REST
                    amended = False
                    if hasattr(self.spot_adapter, "amend_order"):
                        amended = await self.spot_adapter.amend_order(
                            symbol=spread_order.spot_leg.symbol,
                            order_id=spread_order.spot_leg.order_id,
                            new_price=spread_order.spot_leg.target_price,
                            pos_side=spread_order.spot_leg.pos_side,
                        )
                        if amended:
                            spread_order.spot_leg.placed_price = spread_order.spot_leg.target_price
                            logger.debug("Spot order amended in-place: ordId=%s price=%.2f",
                                        spread_order.spot_leg.order_id,
                                        spread_order.spot_leg.target_price)
                    if not amended:
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
                                    order_type="POST_ONLY",
                                    quantity=remaining_qty,
                                    price=spread_order.spot_leg.target_price,
                                    pos_side=spread_order.spot_leg.pos_side,
                                )
                                if result.success:
                                    spread_order.spot_leg.order_id = result.order_id
                                    spread_order.spot_leg.placed_price = spread_order.spot_leg.target_price
                                    logger.debug("Spot order replaced: new_id=%s, price=%.2f",
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
                    amended = False
                    if hasattr(self.futures_adapter, "amend_order"):
                        amended = await self.futures_adapter.amend_order(
                            symbol=spread_order.futures_leg.symbol,
                            order_id=spread_order.futures_leg.order_id,
                            new_price=spread_order.futures_leg.target_price,
                            pos_side=spread_order.futures_leg.pos_side,
                        )
                        if amended:
                            spread_order.futures_leg.placed_price = spread_order.futures_leg.target_price
                            logger.debug("Futures order amended in-place: ordId=%s price=%.2f",
                                        spread_order.futures_leg.order_id,
                                        spread_order.futures_leg.target_price)
                    if not amended:
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
                                    order_type="POST_ONLY",
                                    quantity=remaining_qty,
                                    price=spread_order.futures_leg.target_price,
                                    pos_side=spread_order.futures_leg.pos_side,
                                )
                                if result.success:
                                    spread_order.futures_leg.order_id = result.order_id
                                    spread_order.futures_leg.placed_price = spread_order.futures_leg.target_price
                                    logger.debug("Futures order replaced: new_id=%s, price=%.2f",
                                               result.order_id, spread_order.futures_leg.target_price)
                                else:
                                    logger.error("Failed to place new futures order after cancel: %s", result.error)
                                    spread_order.futures_leg.status = LegStatus.FAILED
                        else:
                            logger.warning("Failed to cancel futures order for amend - skipping to avoid duplicates")
            except Exception as e:
                logger.error("Failed to amend futures order: %s", e)

    # Map of OKX cancelSource codes → human-readable explanations.
    # OKX confirmed (2026-06): cancelSource=31 means the POST_ONLY order would
    # have taken liquidity — the limit price crossed the opposite side of the book
    # at the moment of placement, so OKX cancelled it rather than let it fill as
    # a taker. It is NOT a rate-limit or cancel-ratio throttle; there is no
    # cooldown and no counter to reset. Each order is evaluated independently.
    # Remedy: retry quickly (10 s) as POST_ONLY; after one rejection switch to
    # MARKET to guarantee the close rather than staying stuck.
    _OKX_CANCEL_REASONS = {
        "0":  "user initiated",
        "1":  "system cancelled",
        "2":  "not matched and cancelled",
        "13": "price limit breach",
        "17": "IOC unfilled portion",
        "20": "post_only rejected — limit price crossed the book (would take liquidity)",
        "21": "self-trade prevented",
        "31": "post_only rejected — limit price crossed the book at placement (would take liquidity)",
    }

    @classmethod
    def _explain_cancel(cls, status: Dict[str, Any]) -> str:
        """Translate an OKX cancelled-order status into a human-readable reason.
        Always prefer OKX's own cancel_source_reason text when supplied — our
        code-to-text map is a fallback, not the source of truth, and OKX has
        proven willing to add new cancelSource codes without docs updates.
        """
        code = str(status.get("cancel_source") or "")
        text = (status.get("cancel_source_reason") or "").strip()
        our_label = cls._OKX_CANCEL_REASONS.get(code)
        if text and our_label and text.lower() != our_label.lower():
            return f"{text} [{our_label}] (cancelSource={code})"
        if text:
            return f"{text} (cancelSource={code or 'n/a'})"
        if our_label:
            return f"{our_label} (cancelSource={code})"
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
                        logger.info(
                            "Spot leg partially filled: %.6f / %.6f @ %.4f (will continue polling for full fill)",
                            status["filled_qty"], spread_order.spot_leg.quantity,
                            status["filled_price"],
                        )
                    elif status["state"] == "canceled":
                        spread_order.spot_leg.status = LegStatus.CANCELLED
                        cancel_reason = self._explain_cancel(status)
                        logger.warning("Spot leg cancelled: %s", cancel_reason)
                        if str(status.get("cancel_source") or "") == "31":
                            spread_order.throttled = True
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
                        logger.info(
                            "Futures leg partially filled: %.6f / %.6f @ %.4f (will continue polling for full fill)",
                            status["filled_qty"], spread_order.futures_leg.quantity,
                            status.get("filled_price", 0.0),
                        )
                        spread_order.futures_leg.filled_price = status["filled_price"]
                    elif status["state"] == "canceled":
                        spread_order.futures_leg.status = LegStatus.CANCELLED
                        cancel_reason = self._explain_cancel(status)
                        logger.warning("Futures leg cancelled: %s", cancel_reason)
                        if str(status.get("cancel_source") or "") == "31":
                            spread_order.throttled = True
            except Exception as e:
                logger.error("Error checking futures order status: %s", e)

        # CRITICAL: If one leg is cancelled and other is still OPEN, cancel the other immediately
        # This prevents orphan positions from POST_ONLY rejections
        await self._cancel_if_one_leg_failed(spread_order)

    async def _cancel_if_one_leg_failed(self, spread_order: SpreadOrder) -> None:
        """
        Settle the remaining open leg when one leg was cancelled/failed.

        CRITICAL for preventing orphan positions when POST_ONLY orders are rejected
        (cancelled because they would cross the spread). Delegates to
        _settle_failed_leg_counterpart, which cancels the still-open leg AND verifies
        the real fill afterwards so a partial fill can't silently leak.
        """
        failed_states = (LegStatus.FAILED, LegStatus.CANCELLED)

        # If spot failed but futures is still open, settle futures
        if spread_order.spot_leg.status in failed_states and spread_order.futures_leg.status == LegStatus.OPEN:
            logger.warning("Spot leg failed/cancelled - settling futures leg to prevent orphan")
            await self._settle_failed_leg_counterpart(
                spread_order.futures_leg, self.futures_adapter, "futures", "spot",
            )

        # If futures failed but spot is still open, settle spot
        if spread_order.futures_leg.status in failed_states and spread_order.spot_leg.status == LegStatus.OPEN:
            logger.warning("Futures leg failed/cancelled - settling spot leg to prevent orphan")
            await self._settle_failed_leg_counterpart(
                spread_order.spot_leg, self.spot_adapter, "spot", "futures",
            )

    async def _settle_failed_leg_counterpart(
        self, leg: LegOrder, adapter: ExchangeAdapter, label: str, other_label: str,
    ) -> None:
        """
        The `other_label` leg failed/cancelled (e.g. a POST_ONLY reject), so this
        `leg` must not stay live. Cancel it — then ALWAYS re-check the actual fill.

        A POST_ONLY order can rest and PARTIALLY fill in the ~tens-of-ms gap before
        our cancel lands; cancel_order then cancels only the *remainder* and returns
        success. The old code trusted that success and declared "no orphan", so the
        filled sliver leaked as a naked, unhedged 20x position — the root cause of the
        stuck-orphan incidents (a leg leaked ~1 contract per rejection storm).

        We now verify filled_qty (state=="filled" alone misses partials: a partly
        filled order reads state=="canceled" with accFillSz>0). If anything leaked,
        flatten it immediately via close-position (reduce-only, whole-position,
        units-agnostic) and fail the entry so it retries flat — never carry an
        unintended sliver forward. The engine's orphan reconciler is the backstop if
        the flatten races the position becoming visible.
        """
        try:
            cancelled = await adapter.cancel_order(leg.symbol, leg.order_id)
        except Exception as e:
            logger.error("Error cancelling %s leg after %s failure: %s", label, other_label, e)
            cancelled = False

        # Cancel success does NOT imply zero fill — re-read the real state.
        filled_qty = 0.0
        state = None
        try:
            status = await adapter.get_order_status(leg.symbol, leg.order_id)
            if status:
                filled_qty = float(status.get("filled_qty") or 0)
                state = status.get("state")
        except Exception as e:
            logger.error("Could not read %s leg status after cancel: %s", label, e)

        if filled_qty > 0:
            logger.error(
                "LEG LEAK: %s leg filled %.6f (state=%s) despite %s failing — "
                "flattening the naked sliver reduce-only and failing the entry",
                label, filled_qty, state, other_label,
            )
            try:
                pos_side_arg = leg.pos_side.upper() if leg.pos_side else None
                result = await adapter.close_position(leg.symbol, pos_side=pos_side_arg)
                if result.success:
                    logger.warning("LEG LEAK flattened: %s %s closed reduce-only", label, leg.symbol)
                else:
                    logger.error(
                        "LEG LEAK flatten FAILED for %s %s: %s — orphan reconciler will retry",
                        label, leg.symbol, result.error,
                    )
            except Exception as e:
                logger.error("LEG LEAK flatten error for %s %s: %s", label, leg.symbol, e)
            # Mark terminal so the entry fails cleanly (both legs failed → retry flat).
            # Leave filled_qty=0 so the engine sees 0% fill and stays flat — which
            # matches reality after the flatten.
            leg.status = LegStatus.CANCELLED
        elif cancelled:
            leg.status = LegStatus.CANCELLED
            logger.info("%s leg cancelled cleanly (no fill) - no orphan", label.capitalize())
        else:
            # Cancel unconfirmed and no fill seen — order may still be live. Leave it
            # OPEN so the status loop / timeout handler retries rather than falsely
            # declaring it dead (which would itself risk an orphan).
            logger.warning(
                "%s leg cancel unconfirmed and no fill detected — leaving OPEN for retry",
                label.capitalize(),
            )

    async def _handle_timeout(self, spread_order: SpreadOrder) -> None:
        """Handle timeout - cancel unfilled orders and close any partial fills."""
        logger.debug("Handling limit order timeout")

        # Cancel any resting order. A PARTIAL leg still has an unfilled REMAINDER
        # resting on the book — cancel that too, not just OPEN legs, or it keeps
        # dribbling fills after timeout (live #136: the uncancelled futures
        # remainder re-orphaned twice and blocked new entries for ~50 min). A
        # PARTIAL leg keeps its status (its fills stand, remainder now cancelled);
        # a zero-fill OPEN leg becomes CANCELLED.
        for leg, adapter in (
            (spread_order.spot_leg, self.spot_adapter),
            (spread_order.futures_leg, self.futures_adapter),
        ):
            if leg.status in (LegStatus.OPEN, LegStatus.PARTIAL) and leg.order_id:
                try:
                    await adapter.cancel_order(leg.symbol, leg.order_id)
                    if leg.status == LegStatus.OPEN:
                        leg.status = LegStatus.CANCELLED
                except Exception as e:
                    logger.error("Failed to cancel %s order: %s", leg.symbol, e)

        # Handle partial fills
        if spread_order.has_partial_fill:
            await self._handle_leg_risk(spread_order)

    async def _refresh_partial_legs(self, spread_order: SpreadOrder) -> None:
        """Re-poll any leg currently in PARTIAL state to catch fills the
        polling loop missed (state-transition lag between matching engine
        and our 250ms poll). Upgrades PARTIAL → FILLED when the exchange
        confirms the leg is complete.
        """
        for label, leg, adapter in (
            ("spot",    spread_order.spot_leg,    self.spot_adapter),
            ("futures", spread_order.futures_leg, self.futures_adapter),
        ):
            if leg.status != LegStatus.PARTIAL or not leg.order_id:
                continue
            try:
                status = await adapter.get_order_status(leg.symbol, leg.order_id)
                if not status:
                    continue
                state = status.get("state")
                if state == "filled":
                    leg.status = LegStatus.FILLED
                    leg.filled_qty = status["filled_qty"]
                    leg.filled_price = status["filled_price"]
                    logger.info(
                        "Re-poll caught up: %s leg fully filled now (qty=%.6f @ %.4f) — upgrading PARTIAL → FILLED",
                        label, leg.filled_qty, leg.filled_price,
                    )
                elif state == "partially_filled":
                    # Stay PARTIAL but update the qty in case it grew.
                    leg.filled_qty = status["filled_qty"]
                    leg.filled_price = status["filled_price"]
            except Exception as e:
                logger.warning("Re-poll for %s PARTIAL leg failed: %s", label, e)

    async def _flatten_residual_fills(self, spread_order: SpreadOrder) -> None:
        """Flatten any leg that ended with a real fill when the spread could not
        complete — returns to flat immediately (reduce-only MARKET) instead of
        leaving a naked leg for the 60s orphan-guard to market-flatten at a loss
        (#136). Marks both legs FAILED so the engine rejects the entry cleanly.

        Best-effort: if a flatten call fails we log CRITICAL and leave the 60s
        orphan-guard as the backstop — this can only ever REDUCE exposure faster
        than before, never make it worse.
        """
        for label, leg, adapter in (
            ("spot", spread_order.spot_leg, self.spot_adapter),
            ("futures", spread_order.futures_leg, self.futures_adapter),
        ):
            if leg.filled_qty <= 0:
                continue
            close_side = "SELL" if leg.side == "BUY" else "BUY"
            is_deriv = is_derivative(leg.symbol)
            kwargs = dict(symbol=leg.symbol, side=close_side,
                          order_type="MARKET", quantity=leg.filled_qty)
            if is_deriv:
                # reduce_only clamps to the live position, so this can only CLOSE
                # the fill — never flip a flat book into a fresh position.
                kwargs["reduce_only"] = True
                if leg.pos_side:
                    kwargs["pos_side"] = leg.pos_side
            elif close_side == "BUY":
                # True-spot MARKET BUY sizes by USDT notional, not base qty.
                try:
                    tick = await adapter.get_tick(leg.symbol)
                    if tick and tick.mid:
                        kwargs["notional_usdt"] = round(leg.filled_qty * tick.mid, 2)
                except Exception as e:
                    logger.warning("residual-flatten: could not size %s notional: %s", label, e)
            try:
                result = await adapter.place_order(**kwargs)
                if result and getattr(result, "success", False):
                    logger.warning(
                        "Residual %s leg flattened reduce-only (qty=%.6f) — "
                        "entry hedge could not complete", label, leg.filled_qty)
                else:
                    logger.error(
                        "CRITICAL: residual %s flatten failed: %s — 60s "
                        "orphan-guard is the backstop", label,
                        getattr(result, "error", "no result"))
            except Exception as e:
                logger.error(
                    "CRITICAL: residual %s flatten exception: %s — 60s "
                    "orphan-guard is the backstop", label, e)
        spread_order.spot_leg.status = LegStatus.FAILED
        spread_order.futures_leg.status = LegStatus.FAILED

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

        # FIRST: re-poll any PARTIAL leg from the exchange. Polling can lag
        # behind the matching engine — a leg that read partially_filled on
        # our last poll may have completed since then. Without this refresh,
        # is_complete stays False (PARTIAL != FILLED) even though both legs
        # are 100% filled on the exchange. The engine then auto-closes the
        # "orphaned" positions at MARKET, eating fees + slippage on every
        # cycle. Root cause of the 07/25-07/37 destructive loop.
        await self._refresh_partial_legs(spread_order)

        spot_filled = spread_order.spot_leg.status in filled_states and spread_order.spot_leg.filled_qty > 0
        futures_filled = spread_order.futures_leg.status in filled_states and spread_order.futures_leg.filled_qty > 0

        if spot_filled and not futures_filled:
            logger.warning("Orphan SPOT filled - attempting LIMIT recovery for futures leg")
            recovered = await self._attempt_maker_recovery(
                adapter=self.futures_adapter,
                leg=spread_order.futures_leg,
                label="futures",
                recovery_timeout_sec=getattr(self.config, 'orphan_recovery_timeout_sec', 5),
                filled_leg=spread_order.spot_leg,
                filled_adapter=self.spot_adapter,
            )
            if not recovered:
                # Last resort: close the spot leg at market
                logger.error("Futures recovery failed - closing spot orphan at MARKET (taker fees apply)")
                close_side = "SELL" if spread_order.spot_leg.side == "BUY" else "BUY"
                spot_is_deriv = is_derivative(spread_order.spot_leg.symbol)
                # Cross-margin TRUE-SPOT MARKET BUY needs notional_usdt (sz must be
                # in USDT). Derivatives close by contract qty, never by notional.
                spot_notional = None
                if close_side == "BUY" and not spot_is_deriv:
                    try:
                        spot_tick = await self.spot_adapter.get_tick(spread_order.spot_leg.symbol)
                        if spot_tick:
                            spot_notional = round(spread_order.spot_leg.filled_qty * spot_tick.mid, 2)
                    except Exception as e:
                        logger.warning("Could not fetch spot tick for notional calc: %s", e)
                close_kwargs = dict(
                    symbol=spread_order.spot_leg.symbol,
                    side=close_side,
                    order_type="MARKET",
                    quantity=spread_order.spot_leg.filled_qty,
                    notional_usdt=spot_notional,
                )
                # When the "spot" leg is actually a derivative (e.g. ETH-USDT-SWAP),
                # close REDUCE-ONLY with the held pos_side. OKX clamps a reduce-only
                # order to the live position size, so this flatten can only REDUCE an
                # existing position — never flip a flat book into a fresh one.
                # Without this, filled_qty (reported in CONTRACTS, not base units)
                # was passed as a base-currency qty, inflating ~10x via ctVal, and
                # with no reduce_only it opened a new position. That is exactly the
                # 06/26 incident: the engine sold 6.3 ETH into an already-flat book,
                # then bought it back at a ~$17 loss.
                if spot_is_deriv:
                    close_kwargs["reduce_only"] = True
                    if spread_order.spot_leg.pos_side:
                        close_kwargs["pos_side"] = spread_order.spot_leg.pos_side
                result = await self.spot_adapter.place_order(**close_kwargs)
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
                recovery_timeout_sec=getattr(self.config, 'orphan_recovery_timeout_sec', 5),
                filled_leg=spread_order.futures_leg,
                filled_adapter=self.futures_adapter,
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
        recovery_timeout_sec: int = 5,
        price_step_bps: float = 3.0,
        max_price_steps: int = 3,
        filled_leg: Optional[LegOrder] = None,
        filled_adapter: Optional[ExchangeAdapter] = None,
    ) -> bool:
        """
        Fill an orphaned leg, prioritising hedge re-establishment over fees.

        When one leg fills and the other is rejected, the position is a naked
        directional punt — every second unhedged is pure market risk that dwarfs
        the ~3 bps taker premium. Strategy (per operator decision):
        - Try a passive maker LIMIT for a SHORT window (~recovery_timeout_sec),
          nudging toward market every (timeout / max_steps) seconds.
        - If still unfilled when the window elapses, CROSS the book (taker) to
          complete the hedge immediately rather than abandoning the entry.
        - ABORT-AND-FLATTEN safety net: if the half-hedged position's unrealized
          loss breaches max_loss_usd at any point, stop chasing and return False
          so the caller flattens the filled leg — capping the orphan bleed.

        filled_leg / filled_adapter: the already-on leg, used for the abort PnL
        check. Optional for backward compatibility.

        Returns True if the missing leg was filled (maker or taker) and the hedge
        is complete; False if the caller should flatten the filled leg to flat.
        """
        logger.info("Starting maker recovery for %s leg: %s %.6f",
                    label, leg.side, leg.quantity)

        step_interval = recovery_timeout_sec / max_price_steps
        price_offset_bps = 0.0  # Start at best bid/ask, move toward market each step
        deadline = datetime.utcnow() + timedelta(seconds=recovery_timeout_sec)
        step_deadline = datetime.utcnow() + timedelta(seconds=step_interval)
        current_order_id = None
        abort_usd = getattr(self.config, 'max_loss_usd', 0.0) or 0.0

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
                await asyncio.sleep(0.5)

                # Abort-and-flatten safety net: if the half-hedged position is
                # already past the dollar-stop, stop chasing and let the caller
                # flatten the filled leg — capping the naked-leg bleed at the stop.
                if abort_usd > 0 and filled_leg is not None and filled_adapter is not None:
                    pnl = await self._half_hedged_pnl(filled_leg, filled_adapter)
                    if pnl is not None and pnl <= -abort_usd:
                        logger.error(
                            "Orphan ABORT: half-hedged exposure unrealized $%.2f <= -$%.2f "
                            "max_loss — cancelling %s recovery, flattening filled leg",
                            pnl, abort_usd, label)
                        if current_order_id:
                            await adapter.cancel_order(leg.symbol, current_order_id)
                        return False

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

            # Maker window elapsed — cross the book (taker) to complete the hedge
            # immediately rather than leaving the position naked any longer. The
            # ~3 bps taker premium is trivial next to seconds of directional risk.
            logger.warning("Recovery maker window (%ds) elapsed for %s — crossing book (taker) to re-hedge",
                          recovery_timeout_sec, label)
            if current_order_id:
                await adapter.cancel_order(leg.symbol, current_order_id)

            return await self._cross_to_fill(adapter, leg, label)

        except Exception as e:
            logger.exception("Error during maker recovery for %s: %s", label, e)
            # Try to cancel any open recovery order
            if current_order_id:
                try:
                    await adapter.cancel_order(leg.symbol, current_order_id)
                except Exception:
                    pass
            return False

    async def _cross_to_fill(self, adapter: ExchangeAdapter, leg: LegOrder, label: str) -> bool:
        """
        Cross the book with an aggressive LIMIT (taker) to fill the orphan leg now.
        A crossing LIMIT (not MARKET) keeps spot BUY sizing in base units while
        still taking liquidity immediately. Returns True only on a confirmed fill.
        """
        try:
            tick = await adapter.get_tick(leg.symbol)
            if not tick:
                logger.error("Cross-to-fill: no tick for %s — cannot re-hedge", leg.symbol)
                return False
            cross_buf = 0.0010  # 10 bps through the touch to guarantee the cross
            if leg.side == "BUY":
                px = round(tick.ask * (1 + cross_buf), 2)
            else:
                px = round(tick.bid * (1 - cross_buf), 2)
            remaining = leg.quantity - leg.filled_qty
            qty = remaining if remaining > 0 else leg.quantity
            result = await adapter.place_order(
                symbol=leg.symbol, side=leg.side, order_type="LIMIT",
                quantity=qty, price=px, pos_side=leg.pos_side,
            )
            if not result.success:
                logger.error("Cross-to-fill order rejected for %s: %s", label, result.error)
                return False
            order_id = result.order_id
            logger.info("Cross-to-fill: %s crossing LIMIT %s qty=%.6f @ %.4f (taker)",
                        label, leg.side, qty, px)
            # Poll briefly for the taker fill (should be near-instant)
            for _ in range(6):
                await asyncio.sleep(0.5)
                status = await adapter.get_order_status(leg.symbol, order_id)
                if status and status["state"] == "filled":
                    leg.status = LegStatus.FILLED
                    leg.filled_qty = status["filled_qty"]
                    leg.filled_price = status["filled_price"]
                    leg.order_id = order_id
                    logger.info("Cross-to-fill SUCCESS: %s leg hedged as taker @ %.4f",
                                label, leg.filled_price)
                    return True
            # Did not confirm — cancel and hand back to caller to flatten
            await adapter.cancel_order(leg.symbol, order_id)
            logger.error("Cross-to-fill did not confirm for %s — abandoning to flat", label)
            return False
        except Exception as e:
            logger.exception("Cross-to-fill error for %s: %s", label, e)
            return False

    async def _half_hedged_pnl(self, filled_leg: LegOrder, adapter: ExchangeAdapter) -> Optional[float]:
        """
        USD unrealized PnL of the already-filled (currently unhedged) leg.
        Uses base-unit quantity × price so it is unit-consistent with the rest
        of the executor. Negative = losing. Returns None if no mark available.
        """
        try:
            tick = await adapter.get_tick(filled_leg.symbol)
            if not tick:
                return None
            mark = getattr(tick, "mid", 0.0) or 0.0
            if mark <= 0:
                mark = ((tick.bid + tick.ask) / 2.0) if (tick.bid and tick.ask) else tick.last
            entry = filled_leg.filled_price or filled_leg.target_price
            if not mark or not entry:
                return None
            qty_base = filled_leg.quantity  # base units (e.g. ETH/BTC)
            if filled_leg.side == "BUY":
                return (mark - entry) * qty_base
            return (entry - mark) * qty_base
        except Exception:
            return None

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
