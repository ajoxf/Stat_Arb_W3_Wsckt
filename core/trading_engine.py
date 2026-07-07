"""
Trading engine for crypto statistical arbitrage.
Manages the main trading loop, position management, and order execution.
"""

import asyncio
import logging
import math
from collections import deque
from datetime import datetime, timedelta, date
from typing import Optional, Callable, Dict, Any, List, Tuple
from dataclasses import dataclass

from models import (
    TradingConfig, Trade, MarketTick, Signal, Position,
    OrderResult, CRYPTO_ASSETS, get_symbols_for_asset
)
from core.signals import SignalGenerator
from core.order_executor import OrderExecutor
from core.trade_logger import get_trade_logger
from core.telegram_bot import get_notifier
from core.ai_monitor import AIMonitor
from adapters.base import ExchangeAdapter, is_derivative
from adapters.okx_websocket import OKXWebSocketManager

logger = logging.getLogger(__name__)

# Hard safety cap on futures leverage to prevent accidental over-leveraging.
# OKX caps Expiry (dated) Futures at 20x and that's the contract type this
# bot targets; perpetuals go higher but we use the more conservative ceiling.
MAX_SAFE_FUTURES_LEVERAGE = 20


@dataclass
class EngineState:
    """Current engine state."""
    is_running: bool = False
    algo_enabled: bool = False
    paper_trading: bool = True
    current_position: str = "NONE"  # NONE, LONG, SHORT
    last_tick_time: Optional[datetime] = None
    last_signal: Optional[Signal] = None
    current_trade: Optional[Trade] = None
    error: str = ""


def exit_spread_levels(entry_spread: float, quantity: float, position_type: str,
                       fees_usd: float, target_usd: float,
                       stop_usd: float, gate_usd: float = 0.0) -> Optional[Dict[str, Any]]:
    """Absolute spread values at which the trade breaks even, takes profit and
    stops out. This is the trade's whole geometry in the one variable that pays:
        net(S) = d × (S − entry_spread) × qty − fees,  d = −1 LONG / +1 SHORT
    (matches _live_net_pnl exactly). Solving:
        break-even   net = 0            →  S = entry + d × fees/qty
        gate-release net = gate         →  S = entry + d × (gate+fees)/qty
        take-profit  net = target       →  S = entry + d × (target+fees)/qty
        stop         gross = −stop      →  S = entry − d × stop/qty
    (the dollar stop fires on GROSS move ≥ stop, see _check_override_exit).
    gate_release is where a reversion EXIT actually closes under the exit
    profit gate — it equals break_even when gate_usd is 0. Levels are in
    spread-price units — they do not drift with the rolling mean, unlike the
    in-trade z-score. Returns None if quantity is unusable; target/stop levels
    are None when that override is disabled (<= 0).
    """
    if not quantity or quantity <= 0:
        return None
    d = -1.0 if (position_type or "").upper() == "LONG" else 1.0
    be = entry_spread + d * (fees_usd / quantity)
    gate = entry_spread + d * ((max(gate_usd, 0.0) + fees_usd) / quantity)
    tp = entry_spread + d * ((target_usd + fees_usd) / quantity) if target_usd > 0 else None
    sl = entry_spread - d * (stop_usd / quantity) if stop_usd > 0 else None
    return {
        'entry': entry_spread,
        'break_even': be,
        'gate_release': gate,
        'take_profit': tp,
        'stop': sl,
        'favorable': 'down' if d < 0 else 'up',   # profitable spread direction
    }


def per_leg_gross_pnl(position_type: str, spot_qty: float, futures_qty: float,
                      entry_spot: float, entry_fut: float,
                      exit_spot: float, exit_fut: float) -> float:
    """Gross P&L computed per leg from actual quantities and prices — the same
    accounting the exchange does, so it matches OKX to the cent.

    LONG spread = long the spot leg, short the futures leg (entry: BUY spot,
    SELL futures); SHORT is the mirror. Identical to the legacy
    spread_change × quantity formula when spot_qty == beta × futures_qty; it
    diverges exactly when contract rounding made the executed hedge ≠ beta —
    which is the real position, so this is the number that's right.
    """
    if (position_type or "").upper() == "LONG":
        return (spot_qty * (exit_spot - entry_spot)
                + futures_qty * (entry_fut - exit_fut))
    return (spot_qty * (entry_spot - exit_spot)
            + futures_qty * (exit_fut - entry_fut))


def lattice_leg_sizes(spot_qty: float, futures_qty: float, beta: float,
                      ct_val_a: float, ct_val_b: float,
                      spot_price: float, futures_price: float,
                      budget_tol_pct: float = 12.0) -> Optional[Tuple[float, float, int, int]]:
    """Choose whole-contract sizes for BOTH legs together so the executed
    ratio spot/futures lands as close to beta (dollar-neutral) as possible.

    Flooring each leg independently distorts the hedge by up to a full
    contract on the small leg — e.g. ideal 10.08 / 2.71 ETH/BTC contracts
    floors to 10/2 = ratio 50 when beta is 37.2 (a 34% naked overhang),
    while this picks 11/3 = ratio 36.7 (1.5% error). Candidates are searched
    ±1 contract around the ideals, must keep each leg >= 1 contract, and may
    not exceed the ideal TOTAL notional by more than budget_tol_pct (the
    small leg's +1-contract granularity needs ~10% headroom at tiny sizes).

    Returns (spot_qty', futures_qty', a_contracts, b_contracts), or None when
    no candidate fits (caller keeps the original quantities and the exchange
    minimums have the final word).
    """
    if min(ct_val_a, ct_val_b, spot_qty, futures_qty, beta) <= 0:
        return None
    if spot_price <= 0 or futures_price <= 0:
        return None
    a_ideal = spot_qty / ct_val_a
    b_ideal = futures_qty / ct_val_b
    ideal_notional = spot_qty * spot_price + futures_qty * futures_price
    max_notional = ideal_notional * (1.0 + budget_tol_pct / 100.0)
    best = None
    for a_ct in range(max(1, int(a_ideal) - 1), int(a_ideal) + 2):
        for b_ct in range(max(1, int(b_ideal) - 1), int(b_ideal) + 2):
            sq = a_ct * ct_val_a
            fq = b_ct * ct_val_b
            if sq * spot_price + fq * futures_price > max_notional:
                continue
            ratio_err = abs(sq / fq - beta) / beta
            key = (round(ratio_err, 6), sq * spot_price + fq * futures_price)
            if best is None or key < best[0]:
                best = (key, (sq, fq, a_ct, b_ct))
    return best[1] if best else None


class TradingEngine:
    """
    Main trading engine that coordinates price feeds, signal generation,
    and order execution for crypto statistical arbitrage.
    """

    def __init__(self, config: TradingConfig):
        self.config = config
        self.signal_generator = SignalGenerator(config)
        self.state = EngineState(paper_trading=config.paper_trading)

        # Exchange adapters (REST)
        self.spot_adapter: Optional[ExchangeAdapter] = None
        self.futures_adapter: Optional[ExchangeAdapter] = None

        # Order executor for spread trades
        self.order_executor: Optional[OrderExecutor] = None

        # WebSocket manager (optional, for real-time streaming)
        self.ws_manager: Optional[OKXWebSocketManager] = None
        self._use_websocket: bool = False
        self._pending_leverage_setup: bool = False

        # Current market data
        self.spot_tick: Optional[MarketTick] = None
        self.futures_tick: Optional[MarketTick] = None

        # Current open trade
        self.open_trade: Optional[Trade] = None

        # Callbacks for UI updates
        self.on_tick: Optional[Callable[[MarketTick, MarketTick], None]] = None
        self.on_signal: Optional[Callable[[Signal], None]] = None
        self.on_trade: Optional[Callable[[Trade], None]] = None
        # Fired when money moves OUTSIDE a recorded trade (orphan auto-close),
        # so the app can persist it to the untracked-close ledger.
        self.on_untracked_close: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_status: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

        # Control flags
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Post-stop-loss cooldown: prevent re-entry for this many seconds after a stop-loss.
        # 300s (5 min) gives the market time to stabilise before re-entering.
        self._stop_loss_cooldown_sec = 300
        self._stop_loss_cooldown_until: Optional[datetime] = None

        # General entry cooldown: prevent rapid re-entry after any trade
        self._entry_cooldown_until: Optional[datetime] = None

        # Set by _check_override_exit when a fast-exit override fires, read once
        # by _close_position to stamp the granular reason onto the trade record.
        self._override_exit_reason: Optional[str] = None

        # signal_generator.total_ticks at the moment the open trade was entered.
        # Lets the half-life-multiple max-hold measure periods-held in the same
        # unit as the half-life. None = no open trade (or reset after restart).
        self._entry_tick_count: Optional[int] = None

        # Small cache for the live balance check so a stream of fast signals
        # doesn't hammer the exchange's account endpoint. 3s is short enough
        # that withdrawals / external trades won't go undetected for long.
        self._balance_cache: Optional[Tuple[datetime, float]] = None
        self._BALANCE_CACHE_TTL_SEC = 3.0
        # M2M buffer multiplier is derived from config.m2m_buffer_pct at check
        # time so the user can change it without restarting.

        # Daily loss tracking
        self._daily_loss_usd: float = 0.0
        self._daily_reset_date: Optional[date] = None

        # Execution lock to prevent new trades while one is being executed
        self._executing_trade = False
        # Set True by _execute_entry_orders when a cancelSource=31 POST_ONLY
        # rejection was the reason for the entry failure; read by _open_position
        # to choose cooldown.  Reset to False at the start of each attempt.
        # cancelSource=31 is NOT a rate-limit throttle — it means the POST_ONLY
        # limit price crossed the spread at placement.  No special cooldown needed.
        self._last_entry_throttled: bool = False

        # Exit POST_ONLY rejection tracking (cancelSource=31 / cancelSource=20).
        # These are NOT rate-limit throttles: each order is evaluated independently.
        # Retry quickly (10s); fall back to MARKET after the first rejection so the
        # position is guaranteed to close rather than sitting open for minutes.
        self._last_exit_attempt: Optional[datetime] = None
        self._exit_retry_interval_sec = 10
        self._last_exit_postonly_rejected: bool = False
        self._exit_postonly_reject_count: int = 0
        self._EXIT_POSTONLY_RETRY_SEC = 10       # retry quickly — no exchange cooldown needed
        self._EXIT_POSTONLY_MARKET_AFTER = 1     # fall back to MARKET after just 1 rejection
        # DOLLAR_STOP only: fire ONE quick maker probe (saves the taker fee IF the book
        # is momentarily calm), then MARKET to guarantee the close. Live evidence showed
        # that on a real stop the spread is diverging — exactly when a maker exit crosses
        # the book and is post-only-rejected — so extra attempts saved $0 and just delayed
        # the close ~30s while the loss swung. One probe keeps the upside with a bounded
        # ~2s delay. Counts any non-filling probe (rejection OR rest-timeout).
        self._dollar_stop_maker_attempts: int = 0
        self._DOLLAR_STOP_MAKER_ATTEMPTS = 1     # one quick probe, then market
        self._DOLLAR_STOP_RETRY_SEC = 2          # don't linger between probe and market fallback
        self._ENTRY_POSTONLY_RETRY_SEC = 10      # POST_ONLY price-crossed rejection — no exchange cooldown, retry quickly
        # Set True by _execute_exit_orders when the exit actually used MARKET (taker fee).
        # Read by _close_position fee calc and _round_trip_fees for accurate fee accounting.
        self._last_exit_was_market: bool = False
        # Throttle for the exit-profit-gate "holding" log line (one per minute max).
        self._exit_gate_last_log: Optional[datetime] = None

        # ── Post-entry exit tracking ────────────────────────────────────────
        # Consecutive ticks where H > hurst_exit_threshold (reset on open/close)
        self._hurst_exit_count: int = 0
        # Consecutive ticks where adverse spread velocity > threshold
        self._velocity_exit_count: int = 0
        # Rolling window of recent spreads for velocity calc (maxlen=120 = 60s at 0.5s/tick)
        self._spread_velocity_window: deque = deque(maxlen=120)
        # Peak P&L (net, USD) observed since the trade was opened — used by the
        # trailing stop to measure how far the trade has pulled back from its high.
        self._peak_pnl: float = 0.0
        # Trade lifecycle telemetry (reset on open): trough net P&L (MAE), the
        # z-score extremes seen during the hold, and how often/long the exit
        # profit gate held a reversion exit. Feeds the crisp post-trade
        # scorecard on Telegram and in the AI review.
        self._trough_pnl: float = 0.0
        self._peak_at: Optional[datetime] = None
        self._trough_at: Optional[datetime] = None
        self._z_seen_min: Optional[float] = None
        self._z_seen_max: Optional[float] = None
        self._gate_hold_count: int = 0
        self._gate_first_hold: Optional[datetime] = None
        # Throttle for the "z-stop suppressed" log line (one per minute max).
        self._z_stop_log_at: Optional[datetime] = None

        # Z-score reset gate: after a STOP_LOSS in direction X, block new X entries
        # until z-score crosses back through ±exit_threshold (spread must genuinely
        # return to normal before the same side is re-entered).
        # None = no gate active.  "SHORT" or "LONG" = gate for that direction.
        self._z_reset_block_direction: Optional[str] = None

        # Optional callback invoked when the engine self-corrects config values
        # (e.g. leverage capped by exchange). Register in app.py to persist to DB.
        self.on_config_corrected = None

        # Tick processing lock: prevents concurrent _process_tick_pair tasks
        # Critical for WebSocket mode where ticks arrive faster than processing
        self._processing_tick = False

        # WS eval throttle: bound how often we run the (relatively heavy) tick
        # pipeline. WS streams ticks far faster than we need to evaluate, and
        # running _process_tick_pair back-to-back saturates the single async
        # loop — starving the REST account/position calls the dashboard
        # schedules onto it (they were timing out at 10s). spot_tick/futures_tick
        # still update on EVERY ws tick (so prices stay fresh for order pricing);
        # this only caps signal/order evaluation cadence so the loop can breathe.
        self._last_tick_eval: Optional[datetime] = None
        self._tick_eval_min_interval: float = 0.25  # seconds (4 evals/s max)

        # Position reconciliation tracking
        self._last_position_verify: Optional[datetime] = None
        self._position_verify_interval = 20  # seconds between checks (was 60 — live needs faster)
        self._position_mismatch: Optional[Dict[str, Any]] = None
        self._orphan_mismatch_count: int = 0  # consecutive detections of orphan futures
        self._orphan_auto_close_threshold: int = 3  # close after ~60s (3 × 20s intervals)

        # Order execution tracking for pattern detection
        self._spot_order_attempts = 0
        self._spot_order_failures = 0
        self._futures_order_attempts = 0
        self._futures_order_failures = 0
        self._last_order_stats_log: Optional[datetime] = None
        self._order_stats_log_interval = 300  # Log stats every 5 minutes

        # Tick interval in seconds
        self.tick_interval = 0.5  # 500ms

        # Periodic AI health monitor (self-disables without ANTHROPIC_API_KEY)
        self.ai_monitor = AIMonitor(self)

    def update_config(self, config: TradingConfig) -> None:
        """Update trading configuration."""
        self.config = config
        self.signal_generator.update_config(config)
        self.state.paper_trading = config.paper_trading
        self.state.algo_enabled = config.algo_enabled
        if self.order_executor:
            self.order_executor.update_config(config)

        # Push updated Telegram settings to notifier
        get_notifier().update_config(config)

        # Apply leverage settings if adapters are configured
        # Note: This may be called from Flask thread without an event loop
        if self.futures_adapter and not config.paper_trading:
            try:
                loop = asyncio.get_running_loop()
                asyncio.create_task(self._apply_leverage_settings())
            except RuntimeError:
                # No running loop - leverage will be applied on next trade or engine restart
                logger.debug("Skipping leverage update (no event loop) - will apply on next trade")

        logger.debug("Trading config updated: asset=%s, paper=%s, algo=%s, exec_mode=%s",
                     config.asset, config.paper_trading, config.algo_enabled,
                     config.order_execution_mode)

    async def _cleanup_orphan_orders(self) -> None:
        """
        Cancel any pending orders from previous sessions.

        This prevents orphan orders from accumulating if the app crashes or restarts.
        """
        if self.state.paper_trading:
            return  # No cleanup needed for paper trading

        try:
            cancelled_count = 0

            # Clean up Leg A orders — adapter derives instType from the symbol
            # (covers spot, swap, and dated futures correctly).
            if self.spot_adapter and hasattr(self.spot_adapter, 'cancel_all_orders'):
                count = await self.spot_adapter.cancel_all_orders(
                    symbol=self.config.spot_symbol,
                )
                cancelled_count += count

            # Clean up Leg B orders
            if self.futures_adapter and hasattr(self.futures_adapter, 'cancel_all_orders'):
                count = await self.futures_adapter.cancel_all_orders(
                    symbol=self.config.futures_symbol,
                )
                cancelled_count += count

            if cancelled_count > 0:
                logger.info("Cleaned up %d orphan orders from previous session", cancelled_count)

        except Exception as e:
            logger.error("Error cleaning up orphan orders: %s", e)

    async def _apply_leverage_settings(self) -> None:
        """Apply leverage settings to exchange."""
        try:
            # Enforce safety cap before touching the exchange
            if self.config.futures_leverage > MAX_SAFE_FUTURES_LEVERAGE:
                logger.warning("Futures leverage %dx exceeds safety cap of %dx — capping",
                               self.config.futures_leverage, MAX_SAFE_FUTURES_LEVERAGE)
                self.config.futures_leverage = MAX_SAFE_FUTURES_LEVERAGE
                if self.on_config_corrected:
                    self.on_config_corrected(self.config)

            # Set futures leverage
            if self.futures_adapter and hasattr(self.futures_adapter, 'set_leverage'):
                success = await self.futures_adapter.set_leverage(
                    self.config.futures_symbol,
                    self.config.futures_leverage,
                )
                if success:
                    logger.info("Futures leverage set to %dx", self.config.futures_leverage)
                else:
                    # Set failed - read back what the exchange actually has
                    logger.warning("Failed to set futures leverage to %dx - reading actual exchange value",
                                  self.config.futures_leverage)
                    if hasattr(self.futures_adapter, 'get_leverage'):
                        actual = await self.futures_adapter.get_leverage(self.config.futures_symbol)
                        if actual and actual != self.config.futures_leverage:
                            logger.warning("Exchange caps leverage at %dx (config=%dx) - adjusting config",
                                          actual, self.config.futures_leverage)
                            self.config.futures_leverage = actual
                            if self.on_config_corrected:
                                self.on_config_corrected(self.config)

            # Note: Spot margin leverage may require different API calls
            # depending on exchange implementation

        except Exception as e:
            logger.error("Error applying leverage settings: %s", e)

    def set_rfq_executor(self, rfq_executor) -> None:
        """Register the RFQ executor on the order executor (call after set_adapters)."""
        if self.order_executor is not None:
            self.order_executor.set_rfq_executor(rfq_executor)
        else:
            logger.warning("set_rfq_executor called before set_adapters — RFQ not registered")

    def set_adapters(self, spot: Optional[ExchangeAdapter], futures: Optional[ExchangeAdapter]) -> None:
        """Set exchange adapters (used for orders / account / REST fallback).

        These adapters are orthogonal to the market-data feed. Do NOT force REST
        polling here: app.py calls set_websocket_manager() BEFORE set_adapters(),
        so unconditionally clearing _use_websocket clobbered the already-wired
        public market-data WS and silently forced the engine onto 0.5s REST
        polling. Reflect reality instead — stream if a WS manager is registered.
        """
        self.spot_adapter = spot
        self.futures_adapter = futures
        self._use_websocket = self.ws_manager is not None

        # Initialize order executor if we have both adapters
        if spot and futures:
            self.order_executor = OrderExecutor(self.config, spot, futures)
            # Let the executor price limit legs off the freshest in-memory WS
            # tick (zero latency) instead of a REST get_tick — cuts POST_ONLY
            # (cancelSource=31) rejections from stale order prices.
            self.order_executor.set_live_tick_provider(self._live_tick_for)
            logger.debug("Order executor initialized (mode=%s)", self.config.order_execution_mode)

            # Mark that we need to apply leverage settings when engine starts
            self._pending_leverage_setup = True

        logger.debug("Adapters set (REST mode): spot=%s, futures=%s",
                     type(spot).__name__ if spot else None,
                     type(futures).__name__ if futures else None)

    def set_websocket_manager(self, ws_manager: OKXWebSocketManager) -> None:
        """Set WebSocket manager for real-time streaming."""
        self.ws_manager = ws_manager
        self._use_websocket = True

        # Set up tick callback
        ws_manager.add_tick_callback(self._on_websocket_tick)

        logger.debug("WebSocket manager set (streaming mode)")

    def _live_tick_for(self, symbol: str) -> Optional[MarketTick]:
        """Latest in-memory tick for a symbol — updated on every WS message (or
        REST poll). Used by the order executor to price limit legs with no REST
        round-trip. Returns None for an unknown symbol so the executor falls back."""
        if symbol == self.config.spot_symbol:
            return self.spot_tick
        if symbol == self.config.futures_symbol:
            return self.futures_tick
        return None

    def _on_websocket_tick(self, symbol: str, tick: MarketTick) -> None:
        """Handle incoming WebSocket tick."""
        # Update the appropriate tick based on symbol
        if symbol == self.config.spot_symbol:
            self.spot_tick = tick
        elif symbol == self.config.futures_symbol:
            self.futures_tick = tick

        # Process tick if we have both AND no tick is currently being processed
        # Without this guard, rapid WebSocket ticks spawn concurrent tasks that
        # all see current_position=NONE and place duplicate orders simultaneously
        if not (self.spot_tick and self.futures_tick) or self._processing_tick:
            return

        # Throttle: cap evaluation cadence so back-to-back WS ticks don't starve
        # the event loop of time to drive concurrent REST calls. Ticks arriving
        # inside the window just refresh the cached prices above and return.
        now = datetime.utcnow()
        if (self._last_tick_eval is not None
                and (now - self._last_tick_eval).total_seconds() < self._tick_eval_min_interval):
            return
        self._last_tick_eval = now
        asyncio.create_task(self._run_tick_guarded())

    async def _run_tick_guarded(self) -> None:
        """Process a tick with a guard to prevent concurrent execution."""
        if self._processing_tick:
            return  # Already processing, skip this tick
        self._processing_tick = True
        try:
            await self._process_tick_pair()
        finally:
            self._processing_tick = False

    def toggle_algo(self, enabled: bool) -> None:
        """Enable or disable algorithmic trading."""
        self.state.algo_enabled = enabled
        self.config.algo_enabled = enabled
        logger.info("Algo trading %s", "enabled" if enabled else "disabled")

    def _check_daily_loss(self) -> bool:
        """Reset daily counter at UTC midnight; return True if limit exceeded."""
        today = datetime.utcnow().date()
        if self._daily_reset_date != today:
            self._daily_reset_date = today
            self._daily_loss_usd = 0.0
        limit = self.config.daily_max_loss_usd
        if limit > 0 and self._daily_loss_usd <= -limit:
            return True
        return False

    async def start(self) -> None:
        """Start the trading engine."""
        if self._running:
            logger.warning("Engine already running")
            return

        self._running = True
        self.state.is_running = True
        self.state.error = ""

        logger.info("Starting trading engine for %s (websocket=%s)",
                    self.config.asset, self._use_websocket)

        # Log comprehensive startup summary for monitoring
        self._log_startup_summary()

        # Clean up any orphan orders from previous sessions
        try:
            logger.info("Cleaning up orphan orders...")
            await self._cleanup_orphan_orders()
            logger.info("Orphan cleanup complete")
        except Exception as e:
            logger.error("Error during orphan cleanup (continuing): %s", e)

        # Apply pending leverage settings (deferred from set_adapters)
        if self._pending_leverage_setup:
            self._pending_leverage_setup = False
            try:
                logger.info("Applying leverage settings...")
                await self._apply_leverage_settings()
                logger.info("Leverage settings applied")
            except Exception as e:
                logger.error("Error applying leverage (continuing): %s", e)

        # Start WebSocket if configured
        if self._use_websocket and self.ws_manager:
            success = await self.ws_manager.start(
                self.config.spot_symbol,
                self.config.futures_symbol
            )
            if success:
                logger.debug("WebSocket streaming started for %s, %s",
                             self.config.spot_symbol, self.config.futures_symbol)
            else:
                logger.warning("WebSocket start failed, falling back to REST polling")
                self._use_websocket = False

        # Start main loop (for REST polling or as a fallback)
        if not self._use_websocket:
            logger.info("Creating REST polling task...")
            self._task = asyncio.create_task(self._main_loop())
            logger.info("REST polling task created successfully")

        # Spin up the periodic AI health monitor only if explicitly enabled.
        # Off by default: it watches ops metrics (WS/position/fees), never code
        # logic, so it can't catch bugs like a mis-gated exit — and it calls the
        # Anthropic API every interval.
        if getattr(self.config, 'ai_monitor_enabled', False):
            try:
                self.ai_monitor.start()
            except Exception:
                logger.exception("AI monitor failed to start (continuing)")
        else:
            logger.info("AI monitor disabled (ai_monitor_enabled=False)")

    async def stop(self) -> None:
        """Stop the trading engine."""
        self._running = False
        self.state.is_running = False

        try:
            self.ai_monitor.stop()
        except Exception:
            logger.debug("AI monitor stop raised (ignored)")

        # Stop WebSocket if running
        if self.ws_manager:
            await self.ws_manager.stop()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        logger.info("Trading engine stopped")

    async def _main_loop(self) -> None:
        """Main trading loop."""
        logger.info("REST polling main loop started (interval=%.1fs, spot=%s, futures=%s)",
                   self.tick_interval, self.config.spot_symbol, self.config.futures_symbol)

        while self._running:
            try:
                await self._tick()
                await asyncio.sleep(self.tick_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                error_msg = f"Error in main loop: {str(e)}"
                logger.exception(error_msg)
                self.state.error = error_msg
                get_notifier().notify_error(error_msg)
                if self.on_error:
                    self.on_error(error_msg)
                await asyncio.sleep(1)  # Wait before retrying

        logger.debug("Main loop ended")

    async def _tick(self) -> None:
        """Process one tick (REST polling mode)."""
        # Fetch current prices
        spot_tick = await self._get_spot_tick()
        futures_tick = await self._get_futures_tick()

        if not spot_tick or not futures_tick:
            # Log why we're not processing (only occasionally to avoid spam)
            if not hasattr(self, '_tick_fail_count'):
                self._tick_fail_count = 0
            self._tick_fail_count += 1
            if self._tick_fail_count <= 3 or self._tick_fail_count % 100 == 0:
                logger.warning("Tick fetch failed (count=%d): spot=%s, futures=%s, symbols=(%s, %s)",
                              self._tick_fail_count,
                              "OK" if spot_tick else "NONE",
                              "OK" if futures_tick else "NONE",
                              self.config.spot_symbol, self.config.futures_symbol)
            return

        # Reset fail count on success
        if hasattr(self, '_tick_fail_count') and self._tick_fail_count > 0:
            logger.info("Tick fetch recovered after %d failures", self._tick_fail_count)
            self._tick_fail_count = 0

        self.spot_tick = spot_tick
        self.futures_tick = futures_tick

        await self._process_tick_pair()

    async def _process_tick_pair(self) -> None:
        """Process a pair of spot/futures ticks (shared by REST and WebSocket modes)."""
        if not self.spot_tick or not self.futures_tick:
            return

        self.state.last_tick_time = datetime.utcnow()

        # Periodic position reconciliation (every 60 seconds)
        if not self.state.paper_trading:
            await self._periodic_position_check()

        # Update signal generator with position
        self.signal_generator.set_position(self.state.current_position)

        # Add tick to signal generator
        self.signal_generator.add_tick(self.spot_tick, self.futures_tick)

        # Notify tick callback
        if self.on_tick:
            self.on_tick(self.spot_tick, self.futures_tick)

        # Generate signal
        signal = self.signal_generator.generate_signal()
        self.state.last_signal = signal

        # Z-reset gate auto-clear (runs EVERY tick). Once z has returned to the
        # ±exit_threshold band the spread has genuinely reverted, so release the
        # post-stop same-side block. This MUST live here, not in _open_position:
        # that block-check only runs when a same-side entry signal is present —
        # i.e. z at the extreme (|z| >= entry_threshold) — where z can never be
        # inside ±exit_threshold. Without this, the gate would arm on the first
        # stop and NEVER clear for the rest of the session, silently killing all
        # same-side entries even after the spread fully reverted (and crossed 0).
        if self._z_reset_block_direction:
            _reset_z = getattr(self.config, 'exit_threshold', 0.5) or 0.5
            _zc = signal.zscore
            _cleared = ((_zc >= -_reset_z) if self._z_reset_block_direction == "SHORT"
                        else (_zc <= _reset_z))
            if _cleared:
                logger.info(
                    "Z-reset gate cleared for %s (z=%.4f back inside ±%.2f) — same-side entry allowed",
                    self._z_reset_block_direction, _zc, _reset_z)
                self._z_reset_block_direction = None

        # Engine-level fast-exit overrides (profit target / max hold / dollar
        # stop). These are dollar/time based and independent of the z-score, so
        # they must be evaluated even when the generator returns NONE. Skipped
        # while an order is already in flight to avoid double-firing.
        # NOT gated on algo_enabled: a held position must keep its stop/target
        # even when the algo is toggled off — disabling the algo stops NEW
        # entries, it must never strand an open position without its stop.
        if (self.open_trade
                and self.state.current_position != "NONE"
                and not self._executing_trade):
            override = self._check_override_exit(signal)
            if override is not None:
                signal = override
                self.state.last_signal = signal

        # Notify signal callback
        if self.on_signal:
            self.on_signal(signal)

        # Execute trading logic. An EXIT/STOP_LOSS on an OPEN position always
        # runs — even with the algo off — so a held position is never left
        # without its stop or profit-take. NEW entries (LONG/SHORT) still
        # require the algo to be enabled.
        in_position = (self.open_trade is not None
                       and self.state.current_position != "NONE")
        if signal.signal_type in ("EXIT", "STOP_LOSS") and in_position:
            # Cost-aware floor: a reversion EXIT below break-even holds instead
            # of closing at a loss (stops/overrides are never gated inside).
            # Separately, the generator's z-based STOP_LOSS can be demoted to
            # entry-ceiling-only duty (z_stop_exit_enabled=False) — the
            # %-of-capital DOLLAR_STOP then owns the in-trade stop.
            if self._z_stop_exit_suppressed(signal):
                pass
            elif not self._signal_exit_gated(signal):
                await self._process_signal(signal)
        elif signal.signal_type in ("LONG", "SHORT"):
            if self.state.algo_enabled:
                await self._process_signal(signal)
            else:
                # Entry signal while algo off — show once in the blocked panel.
                prev = self.signal_generator.last_blocked_signal
                if not prev or prev.get('reason') != 'Algo disabled':
                    self.signal_generator.last_blocked_signal = {
                        'timestamp': datetime.utcnow().isoformat(),
                        'would_be_signal': signal.signal_type,
                        'zscore': round(signal.zscore, 4),
                        'reason': 'Algo disabled',
                    }

    async def _get_spot_tick(self) -> Optional[MarketTick]:
        """Get current spot price."""
        tick = None
        if self.spot_adapter:
            try:
                tick = await self.spot_adapter.get_tick(self.config.spot_symbol)
            except Exception as e:
                logger.error("Error fetching spot tick: %s", e)

        # Paper trading fallback - simulate price if adapter failed or not available
        if tick is None and self.state.paper_trading:
            tick = self._simulate_tick(self.config.spot_symbol, is_spot=True)

        return tick

    async def _get_futures_tick(self) -> Optional[MarketTick]:
        """Get current futures price."""
        tick = None
        if self.futures_adapter:
            try:
                tick = await self.futures_adapter.get_tick(self.config.futures_symbol)
            except Exception as e:
                logger.error("Error fetching futures tick: %s", e)

        # Paper trading fallback - simulate price if adapter failed or not available
        if tick is None and self.state.paper_trading:
            tick = self._simulate_tick(self.config.futures_symbol, is_spot=False)

        return tick

    def _simulate_tick(self, symbol: str, is_spot: bool) -> MarketTick:
        """Simulate a market tick for paper trading."""
        import random

        # Base prices for different assets
        base_prices = {
            'BTC': 65000.0,
            'ETH': 3500.0,
            'SOL': 150.0,
            'XRP': 0.55,
            'DOGE': 0.12,
            'AVAX': 35.0,
            'LINK': 15.0,
        }

        asset = self.config.asset
        base = base_prices.get(asset, 100.0)

        # Add some randomness
        noise = random.gauss(0, base * 0.0001)
        price = base + noise

        # Futures typically trade at slight premium/discount
        if not is_spot:
            # Random basis between -0.1% and +0.3%
            basis = random.uniform(-0.001, 0.003)
            price = price * (1 + basis)

        spread_bps = random.uniform(1, 5)
        half_spread = (spread_bps / 10000) * price / 2

        return MarketTick(
            symbol=symbol,
            bid=price - half_spread,
            ask=price + half_spread,
            last=price,
            volume_24h=random.uniform(1000000, 10000000),
            timestamp=datetime.utcnow(),
        )

    def _live_net_pnl(self, trade: Trade) -> Optional[float]:
        """Estimate the open trade's current NET P&L (USD) from live mid prices,
        minus the same round-trip fee estimate used at the realized close.

        Mirrors the realized-P&L formula in _close_position exactly, but uses
        current mids for the exit legs instead of fills. Returns None when ticks
        aren't available yet.
        """
        if not trade or not self.spot_tick or not self.futures_tick:
            return None
        beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        cur_spot = self.spot_tick.mid
        cur_fut = self.futures_tick.mid
        # Per-leg on the ACTUAL executed quantities (falls back to the
        # beta-derived spot size for legacy/paper trades) — so the dollar stop,
        # profit target and exit gate act on the position we really hold, not
        # the pre-rounding request.
        spot_qty = trade.spot_qty if getattr(trade, 'spot_qty', 0) > 0 else trade.quantity * beta
        pnl_gross = per_leg_gross_pnl(
            trade.position_type, spot_qty, trade.quantity,
            trade.entry_spot_price, trade.entry_futures_price,
            cur_spot, cur_fut,
        )
        # If the exit will definitely be MARKET (already had a POST_ONLY rejection
        # that exhausted the retry budget), use taker fee for the exit estimate.
        exit_override = "MARKET" if self._exit_postonly_reject_count >= self._EXIT_POSTONLY_MARKET_AFTER else None
        fees_usd = self._round_trip_fees(trade, cur_spot, cur_fut, exit_mode_override=exit_override)
        return pnl_gross - fees_usd

    def _round_trip_fees(self, trade: Trade,
                         exit_spot: Optional[float] = None,
                         exit_fut: Optional[float] = None,
                         exit_mode_override: Optional[str] = None) -> float:
        """Total entry+exit fees (USD) for the trade's full round trip, using
        current mids for the exit legs by default.

        exit_mode_override: pass "MARKET" when the next exit is known to use
        taker (e.g. after a POST_ONLY rejection that will trigger MARKET retry)
        so live P&L reflects the actual fee that will be charged.
        """
        beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        if exit_spot is None:
            exit_spot = self.spot_tick.mid if self.spot_tick else trade.entry_spot_price
        if exit_fut is None:
            exit_fut = self.futures_tick.mid if self.futures_tick else trade.entry_futures_price
        spot_maker = getattr(self.config, 'spot_maker_fee_bps', self.config.maker_fee_bps)
        spot_taker = getattr(self.config, 'spot_taker_fee_bps', self.config.taker_fee_bps)
        fut_maker  = getattr(self.config, 'futures_maker_fee_bps', self.config.maker_fee_bps)
        fut_taker  = getattr(self.config, 'futures_taker_fee_bps', self.config.taker_fee_bps)
        entry_mode = getattr(self.config, 'entry_execution_mode', self.config.order_execution_mode)
        exit_mode  = exit_mode_override or getattr(self.config, 'exit_execution_mode', self.config.order_execution_mode)
        leg_a_deriv = is_derivative(self.config.spot_symbol)
        leg_b_deriv = is_derivative(self.config.futures_symbol)
        def _bps(deriv, mode):
            if mode == "LIMIT":
                return fut_maker if deriv else spot_maker
            return fut_taker if deriv else spot_taker
        a_entry, b_entry = _bps(leg_a_deriv, entry_mode), _bps(leg_b_deriv, entry_mode)
        a_exit,  b_exit  = _bps(leg_a_deriv, exit_mode),  _bps(leg_b_deriv, exit_mode)
        spot_qty = trade.spot_qty if getattr(trade, 'spot_qty', 0) > 0 else trade.quantity * beta
        return (
            a_entry / 10000.0 * spot_qty       * trade.entry_spot_price +
            b_entry / 10000.0 * trade.quantity * trade.entry_futures_price +
            a_exit  / 10000.0 * spot_qty       * exit_spot +
            b_exit  / 10000.0 * trade.quantity * exit_fut
        )

    def _exit_spread_levels(self, trade: Trade) -> Optional[Dict[str, Any]]:
        """Live BE/TP/SL spread levels for the open trade, using the SAME
        target/stop/fee sources as the fast-exit overrides — so these are
        exactly the spread values at which those overrides fire. Display and
        logging only; nothing reads these to make decisions."""
        if not trade:
            return None
        try:
            beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
            entry_spread = trade.entry_futures_price - beta * trade.entry_spot_price
            t = self._effective_exit_targets(trade)
            exit_override = ("MARKET" if self._exit_postonly_reject_count
                             >= self._EXIT_POSTONLY_MARKET_AFTER else None)
            fees = self._round_trip_fees(trade, exit_mode_override=exit_override)
            gate = self._exit_gate_floor(trade) or 0.0
            return exit_spread_levels(entry_spread, trade.quantity,
                                      trade.position_type, fees,
                                      t['target_usd'], t['stop_usd'],
                                      gate_usd=gate)
        except Exception:
            return None

    def _z_stop_exit_suppressed(self, signal: Signal) -> bool:
        """True when the generator's z-based STOP_LOSS should NOT close the
        trade (z_stop_exit_enabled=False): post-entry, the rolling z is a
        drifting statistic — mean and σ move during the hold, so the z-stop's
        dollar meaning wanders (live #78 fired at z −5.7 while gross was still
        inside the dollar line). With the switch off, the %-of-capital
        DOLLAR_STOP owns the in-trade stop.

        FAIL-SAFES: never suppress override stops (DOLLAR_STOP / DAILY_LOSS
        arrive as STOP_LOSS but carry _override_exit_reason), and never
        suppress when no dollar stop is armed — a trade must always have a
        stop. The z threshold's entry-ceiling role is untouched either way.
        """
        if signal.signal_type != "STOP_LOSS":
            return False
        if self._override_exit_reason:
            return False                    # dollar/daily stop — never suppress
        if getattr(self.config, 'z_stop_exit_enabled', True):
            return False
        trade = self.open_trade
        if not trade:
            return False
        try:
            stop_usd = self._effective_exit_targets(trade)['stop_usd']
        except Exception:
            return False                    # can't verify a dollar stop — keep z
        if stop_usd <= 0:
            return False                    # no dollar stop armed — keep z backstop
        now = datetime.utcnow()
        if (self._z_stop_log_at is None
                or (now - self._z_stop_log_at).total_seconds() >= 60):
            self._z_stop_log_at = now
            logger.info(
                "z-stop suppressed (z=%.2f): in-trade stop is %%-of-capital "
                "only — dollar stop -$%.2f armed", signal.zscore, stop_usd,
            )
        return True

    def _max_hold_minutes_for(self, trade: Trade) -> float:
        """The trade's max-hold horizon in minutes (half-life form preferred,
        fixed-minutes fallback; 0 = no max-hold configured)."""
        try:
            t = self._effective_exit_targets(trade)
            if t['max_hold_periods'] > 0:
                return t['max_hold_periods'] * 0.5 / 60.0   # 0.5s per tick/period
            return t.get('max_hold_minutes', 0.0) or 0.0
        except Exception:
            return 0.0

    def _exit_gate_floor(self, trade: Trade) -> Optional[float]:
        """Resolved exit-profit-gate floor (USD) for this trade, or None when
        the gate is disabled. The scale-invariant %-of-capital form wins when
        set (house convention, same as stop_loss_capital_pct vs max_loss_usd);
        otherwise the fixed-USD form; a negative USD value disables the gate."""
        pct = getattr(self.config, 'exit_profit_gate_pct', 0.0) or 0.0
        if pct > 0:
            return pct / 100.0 * self._capital_at_risk(trade)
        usd = getattr(self.config, 'exit_profit_gate_usd', 0.0)
        if usd is None or usd < 0:
            return None
        return usd

    def _signal_exit_gated(self, signal: Signal) -> bool:
        """Cost-aware gate on the signal-generator's reversion EXIT.

        The rolling z can revert while the spread hasn't actually paid for the
        trade (the mean drifts toward the spread during the hold), producing
        "EXIT" closes below break-even. With the gate on, a reversion EXIT only
        closes the trade once live net P&L (after ALL fees) >= the configured
        floor — i.e. the spread has genuinely crossed the break-even level.
        Until then the trade holds, still fully protected: DOLLAR_STOP, the z
        stop-loss, PROFIT_TARGET and MAX_HOLD overrides are never gated (they
        are either safety exits or already cost-aware by construction).

        Returns True when the EXIT should be suppressed (keep holding).
        """
        if signal.signal_type != "EXIT":
            return False                    # never gate STOP_LOSS
        if self._override_exit_reason:
            return False                    # override exit (already cost-aware)
        trade = self.open_trade
        if not trade:
            return False
        floor = self._exit_gate_floor(trade)
        if floor is None:
            return False                    # gate disabled
        # The gate defers to max-hold — otherwise gate (needs net >= floor) +
        # max-hold (needs net > 0) + an unreachable target can DEADLOCK a fully
        # reverted trade until a stop (live trade #78: +$1.19 held for being 2
        # cents under the floor, then bled to −$4.46 over 80 min). Past 1× the
        # trade's max-hold the floor decays to break-even; past 2× the gate
        # releases entirely — the reversion edge is spent, take what's there.
        mh_min = self._max_hold_minutes_for(trade)
        if mh_min > 0 and trade.entry_time:
            held_min = (datetime.utcnow() - trade.entry_time).total_seconds() / 60.0
            if held_min >= 2.0 * mh_min:
                logger.info(
                    "Exit gate released: held %.0fm >= 2x max-hold %.0fm — "
                    "reversion edge spent, taking the exit", held_min, mh_min,
                )
                return False
            if held_min >= mh_min:
                floor = min(floor, 0.0)     # break-even only past max-hold
        net = self._live_net_pnl(trade)
        if net is None:
            return False                    # can't price it — fail open, allow the exit
        if net >= floor:
            return False                    # past break-even (+floor) — take the exit
        self._gate_hold_count += 1
        if self._gate_first_hold is None:
            self._gate_first_hold = datetime.utcnow()
        now = datetime.utcnow()
        if (self._exit_gate_last_log is None
                or (now - self._exit_gate_last_log).total_seconds() >= 60):
            self._exit_gate_last_log = now
            lv = self._exit_spread_levels(trade)
            be_txt = (f" — holding until spread clears BE @ {lv['break_even']:.2f}"
                      if lv else "")
            logger.info(
                "EXIT held by profit gate: reversion fired (z=%.4f) but net "
                "$%.2f < $%.2f floor after costs%s",
                signal.zscore, net, floor, be_txt,
            )
        return True

    def _round_trip_cost_usd(self, trade: Trade,
                             exit_spot: Optional[float] = None,
                             exit_fut: Optional[float] = None) -> float:
        """Full round-trip cost in USD = fees + a slippage allowance, matching
        the entry STD filter's cost basis (fees + slippage across 4 legs).

        This — not fees alone — is what the profit-target floor must clear: the
        live NET P&L is measured at the mid, but the real exit crosses the
        spread, and slippage is exactly that mid-to-fill gap. (kept out of
        _round_trip_fees / _live_net_pnl, where real fills already embed
        slippage and adding it would double-count.)
        """
        beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        if exit_spot is None:
            exit_spot = self.spot_tick.mid if self.spot_tick else trade.entry_spot_price
        if exit_fut is None:
            exit_fut = self.futures_tick.mid if self.futures_tick else trade.entry_futures_price
        fees = self._round_trip_fees(trade, exit_spot, exit_fut)
        slip_bps = getattr(self.config, 'slippage_bps', 0.0) or 0.0
        spot_qty = trade.spot_qty if getattr(trade, 'spot_qty', 0) > 0 else trade.quantity * beta
        slippage = slip_bps / 10000.0 * (
            spot_qty       * trade.entry_spot_price +
            trade.quantity * trade.entry_futures_price +
            spot_qty       * exit_spot +
            trade.quantity * exit_fut
        )
        return fees + slippage

    def _capital_at_risk(self, trade: Trade) -> float:
        """Capital actually locked by the open trade: per-leg margin + M2M
        buffer, computed from entry fills. Same formula as the realized close,
        used as the denominator for the %-of-capital dollar stop."""
        beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        spot_qty = trade.spot_qty if getattr(trade, 'spot_qty', 0) > 0 else trade.quantity * beta
        leg_a_deriv = is_derivative(self.config.spot_symbol)
        leg_b_deriv = is_derivative(self.config.futures_symbol)
        leg_a_notional = abs(trade.entry_spot_price * spot_qty)
        leg_b_notional = abs(trade.entry_futures_price * trade.quantity)
        leg_a_lev = max(self.config.spot_leverage    if leg_a_deriv else 1, 1)
        leg_b_lev = max(self.config.futures_leverage if leg_b_deriv else 1, 1)
        margin = leg_a_notional / leg_a_lev + leg_b_notional / leg_b_lev
        buf = getattr(self.config, 'm2m_buffer_pct', 0.0) or 0.0
        return margin * (1 + buf / 100.0)

    def _effective_exit_targets(self, trade: Trade) -> Dict[str, Any]:
        """Resolve the active profit-target $, dollar-stop $, and max-hold
        periods for the open trade, preferring the scale-invariant config form
        (sigma fraction / capital % / half-life multiple) and falling back to
        the fixed-dollar/minute form. Values <= 0 mean that override is off.
        """
        cfg = self.config
        # Profit target $ — precedence: σ-fraction (volatility-aware) >
        # %-of-capital (fires on P&L alone: net >= BE + pct of capital, the
        # "always bank the win" form) > fixed USD. All feed the UNGATED
        # PROFIT_TARGET override — no z required to fire.
        sigma_frac = getattr(cfg, 'profit_target_sigma_frac', 0.0) or 0.0
        cap_tp_pct = getattr(cfg, 'profit_target_capital_pct', 0.0) or 0.0
        if sigma_frac > 0:
            target_usd = (sigma_frac * abs(trade.entry_zscore)
                          * trade.entry_spread_std * trade.quantity)
        elif cap_tp_pct > 0:
            target_usd = cap_tp_pct / 100.0 * self._capital_at_risk(trade)
        else:
            target_usd = getattr(cfg, 'profit_target_usd', 0.0) or 0.0
        # Cost floor: an active profit target must clear the full round-trip
        # cost (fees + slippage) by a safety margin. _live_net_pnl is computed
        # off mids, but a real exit crosses the spread — so a statistically-small
        # target could "hit" on paper yet fill at an actual net loss. Raise the
        # target to the floor so a profit-target exit is always +ve after costs.
        # Uses the same fees+slippage cost basis as the entry STD filter, so the
        # entry gate and this floor are the same economic test (see signals.py).
        rt_cost = 0.0
        if target_usd > 0:
            cost_mult = getattr(cfg, 'profit_target_min_cost_mult', 0.0) or 0.0
            if cost_mult > 0:
                rt_cost = self._round_trip_cost_usd(trade)
                target_usd = max(target_usd, cost_mult * rt_cost)
        # Dollar stop $
        # Primary: derive from target and R:R multiple — stop = target / rr_multiple.
        # This is the correct flow: set the target, R:R determines the stop.
        # Secondary: capital-% or flat max_loss_usd act as a hard backstop.
        # If both are active, use the TIGHTER (smaller) stop.
        rr_mult = getattr(cfg, 'min_entry_rr_multiple', 0.0) or 0.0
        rr_stop = (target_usd / rr_mult) if (rr_mult > 0 and target_usd > 0) else 0.0

        cap_pct = getattr(cfg, 'stop_loss_capital_pct', 0.0) or 0.0
        if cap_pct > 0:
            backstop_usd = cap_pct / 100.0 * self._capital_at_risk(trade)
        else:
            backstop_usd = getattr(cfg, 'max_loss_usd', 0.0) or 0.0

        if rr_stop > 0 and backstop_usd > 0:
            stop_usd = min(rr_stop, backstop_usd)   # tighter stop wins
        elif rr_stop > 0:
            stop_usd = rr_stop
        else:
            stop_usd = backstop_usd
        # Max hold (in periods; half-life is measured in periods)
        hl_mult = getattr(cfg, 'max_hold_halflife_mult', 0.0) or 0.0
        max_hold_periods = 0.0
        max_hold_minutes = getattr(cfg, 'max_hold_minutes', 0.0) or 0.0
        if hl_mult > 0:
            hl = self.signal_generator.current_half_life
            if hl and hl != float('inf'):
                max_hold_periods = hl_mult * hl
        return {
            'target_usd': target_usd,
            'stop_usd': stop_usd,
            'max_hold_periods': max_hold_periods,   # 0 = half-life form off/unavailable
            'max_hold_minutes': max_hold_minutes,   # fixed-minutes fallback
            'round_trip_cost': rt_cost,             # fees+slippage; 0 when floor inactive
        }

    def _check_override_exit(self, signal: Signal) -> Optional[Signal]:
        """Return a synthesized EXIT/STOP_LOSS Signal if a fast-exit override
        (dollar stop / profit target / max hold) fires for the open trade, else
        None. Dollar stop is checked first (risk before reward). Each override
        uses its scale-invariant form when set, else the fixed-$ fallback.
        """
        # Clear any stale reason first so a value left over from a throttled
        # close attempt can't get stamped onto a later unrelated exit.
        self._override_exit_reason = None
        trade = self.open_trade
        if not trade:
            return None

        t = self._effective_exit_targets(trade)
        target_usd = t['target_usd']
        stop_usd = t['stop_usd']
        max_hold_periods = t['max_hold_periods']
        max_hold_minutes = t['max_hold_minutes']
        if (target_usd <= 0 and stop_usd <= 0
                and max_hold_periods <= 0 and max_hold_minutes <= 0):
            return None  # all overrides disabled — pure z-score behaviour

        net_pnl = self._live_net_pnl(trade)
        if net_pnl is None:
            return None

        # Always track the high-water mark so the trailing stop has an accurate peak.
        if net_pnl > self._peak_pnl:
            self._peak_pnl = net_pnl
            self._peak_at = datetime.utcnow()
        if net_pnl < self._trough_pnl:
            self._trough_pnl = net_pnl
            self._trough_at = datetime.utcnow()
        _z = signal.zscore
        if _z is not None:
            if self._z_seen_min is None or _z < self._z_seen_min:
                self._z_seen_min = _z
            if self._z_seen_max is None or _z > self._z_seen_max:
                self._z_seen_max = _z

        exit_type = None
        reason_tag = None
        reason_detail = None
        if stop_usd > 0:
            # _live_net_pnl deducts the full round-trip fee estimate (entry AND
            # exit legs) so the trade "starts" already in a fee hole of ~$2-3.
            # Adding that hole back makes max_loss_usd mean "stop after X of
            # gross spread movement, independent of fees" — which is the natural
            # user expectation when setting a dollar stop.
            fee_hole = self._round_trip_fees(trade)
            gross_stop_threshold = -(abs(stop_usd) + fee_hole)
            if net_pnl <= gross_stop_threshold:
                exit_type, reason_tag = "STOP_LOSS", "DOLLAR_STOP"
                reason_detail = f"gross ${net_pnl + fee_hole:.2f} <= -${stop_usd:.2f} (net ${net_pnl:.2f})"
        elif target_usd > 0 and net_pnl >= target_usd:
            exit_type, reason_tag = "EXIT", "PROFIT_TARGET"
            reason_detail = f"net ${net_pnl:.2f} >= ${target_usd:.2f}"
        elif net_pnl > 0:
            # Max hold fires when already profitable AND the trade is not
            # actively reverting. The Z-progress gate suppresses the exit
            # while Z is more than halfway home — those trades should be left
            # to reach the profit target rather than being cut short.
            if max_hold_periods > 0 and self._entry_tick_count is not None:
                periods_held = self.signal_generator.total_ticks - self._entry_tick_count
                if periods_held >= max_hold_periods:
                    z_progress_min = getattr(self.config, 'max_hold_z_progress_min', 0.5) or 0.0
                    z_suppressed = False
                    if z_progress_min > 0 and trade.entry_zscore:
                        entry_abs = abs(trade.entry_zscore)
                        cur_abs   = abs(signal.zscore)
                        exit_abs  = abs(getattr(self.config, 'exit_threshold', 0.0) or 0.0)
                        journey   = entry_abs - exit_abs
                        if journey > 1e-6:
                            z_progress = (entry_abs - cur_abs) / journey
                            if z_progress >= z_progress_min:
                                z_suppressed = True
                                logger.debug(
                                    "MAX_HOLD suppressed: Z progress %.0f%% >= %.0f%% gate "
                                    "(entry |Z|=%.3f cur |Z|=%.3f), letting trade run to target",
                                    z_progress * 100, z_progress_min * 100,
                                    entry_abs, cur_abs,
                                )
                    if not z_suppressed:
                        exit_type, reason_tag = "EXIT", "MAX_HOLD"
                        reason_detail = (f"{periods_held} periods >= "
                                         f"{max_hold_periods:.0f} (={ getattr(self.config,'max_hold_halflife_mult',0) }×half-life), "
                                         f"net +${net_pnl:.2f}")
            elif max_hold_minutes > 0 and trade.entry_time:
                held_min = (datetime.utcnow() - trade.entry_time).total_seconds() / 60.0
                if held_min >= max_hold_minutes:
                    z_progress_min = getattr(self.config, 'max_hold_z_progress_min', 0.5) or 0.0
                    z_suppressed = False
                    if z_progress_min > 0 and trade.entry_zscore:
                        entry_abs = abs(trade.entry_zscore)
                        cur_abs   = abs(signal.zscore)
                        exit_abs  = abs(getattr(self.config, 'exit_threshold', 0.0) or 0.0)
                        journey   = entry_abs - exit_abs
                        if journey > 1e-6:
                            z_progress = (entry_abs - cur_abs) / journey
                            if z_progress >= z_progress_min:
                                z_suppressed = True
                    if not z_suppressed:
                        exit_type, reason_tag = "EXIT", "MAX_HOLD"
                        reason_detail = f"{held_min:.0f}m >= {max_hold_minutes:.0f}m, net +${net_pnl:.2f}"

        # ── 3. Trailing stop ───────────────────────────────────────────────
        # Once P&L has reached trailing_stop_floor_pct % of the profit target,
        # arm a trailing stop that fires when P&L drops trailing_stop_pct %
        # below the peak. Protects profit that "almost reached target" from
        # fully reverting before any other exit fires.
        if not exit_type:
            trail_pct   = getattr(self.config, 'trailing_stop_pct', 0.0) or 0.0
            floor_pct   = getattr(self.config, 'trailing_stop_floor_pct', 0.0) or 0.0
            if trail_pct > 0 and self._peak_pnl > 0:
                # Floor gate: if floor_pct > 0 the trailing stop only arms once
                # P&L has reached that fraction of the profit target.  This keeps
                # the trailing stop from triggering on tiny early-trade wiggles.
                floor_armed = True
                if floor_pct > 0 and target_usd > 0:
                    floor_armed = self._peak_pnl >= floor_pct / 100.0 * target_usd
                if floor_armed:
                    trail_trigger = self._peak_pnl * (1.0 - trail_pct / 100.0)
                    if net_pnl < trail_trigger:
                        exit_type   = "EXIT"
                        reason_tag  = "TRAILING_STOP"
                        reason_detail = (
                            f"P&L ${net_pnl:.2f} pulled back {trail_pct:.0f}% from peak "
                            f"${self._peak_pnl:.2f} (trigger=${trail_trigger:.2f})"
                        )

        # ── 4. Hurst regime-change exit ────────────────────────────────────
        # H rising above threshold for N consecutive ticks means the spread has
        # flipped from mean-reverting to trending — the core bet is structurally
        # wrong, exit before the dollar stop is hit.
        if (not exit_type
                and getattr(self.config, 'hurst_exit_enabled', False)
                and getattr(self.config, 'hurst_enabled', True)):
            h_thresh = getattr(self.config, 'hurst_exit_threshold', 0.55)
            h_n      = max(1, int(getattr(self.config, 'hurst_exit_n_ticks', 3)))
            if signal.hurst is not None and signal.hurst > h_thresh:
                self._hurst_exit_count += 1
                if self._hurst_exit_count >= h_n:
                    exit_type     = "EXIT"
                    reason_tag    = "HURST_REGIME"
                    reason_detail = (
                        f"H={signal.hurst:.3f} > {h_thresh} for "
                        f"{self._hurst_exit_count} consecutive ticks "
                        f"(regime flipped to trending)"
                    )
            else:
                self._hurst_exit_count = 0

        # ── 5. Spread velocity exit ────────────────────────────────────────
        # If the spread drifts adversely faster than velocity_exit_pts_per_min
        # for N consecutive ticks, the trend is accelerating — cut early.
        # Adverse = spread rising for SHORT position, falling for LONG.
        if not exit_type and getattr(self.config, 'velocity_exit_enabled', False):
            vel_thresh = getattr(self.config, 'velocity_exit_pts_per_min', 2.0)
            vel_n      = max(1, int(getattr(self.config, 'velocity_exit_n_ticks', 5)))
            vel_window = max(2, int(getattr(self.config, 'velocity_exit_window_ticks', 20)))

            self._spread_velocity_window.append(signal.spread)

            if len(self._spread_velocity_window) >= vel_window:
                spread_then      = self._spread_velocity_window[-vel_window]
                window_min       = vel_window * 0.5 / 60.0   # 0.5s per tick → minutes
                raw_velocity     = (signal.spread - spread_then) / window_min
                # Adverse direction depends on position side
                adverse_velocity = (raw_velocity if trade.position_type == "SHORT"
                                    else -raw_velocity)
                if adverse_velocity > vel_thresh:
                    self._velocity_exit_count += 1
                    if self._velocity_exit_count >= vel_n:
                        exit_type     = "EXIT"
                        reason_tag    = "SPREAD_VELOCITY"
                        reason_detail = (
                            f"adverse {adverse_velocity:.2f} pts/min > {vel_thresh} "
                            f"for {self._velocity_exit_count} ticks "
                            f"(spread {spread_then:.1f}→{signal.spread:.1f} "
                            f"over {vel_window * 0.5:.0f}s)"
                        )
                else:
                    self._velocity_exit_count = 0
            else:
                self._velocity_exit_count = 0

        if not exit_type:
            return None

        self._override_exit_reason = reason_tag
        logger.info("Fast-exit override fired: %s (%s)", reason_tag, reason_detail)
        return Signal(
            signal_type=exit_type,
            zscore=signal.zscore,
            spread=signal.spread,
            spread_mean=signal.spread_mean,
            spread_std=signal.spread_std,
            hurst=signal.hurst,
            regime=signal.regime,
            timestamp=datetime.utcnow(),
            current_position=self.state.current_position,
        )

    async def _process_signal(self, signal: Signal) -> None:
        """Process a trading signal."""
        logger.debug("Processing signal: %s (zscore=%.4f, position=%s)",
                     signal.signal_type, signal.zscore, self.state.current_position)

        # Notify Telegram for actionable signals (entry/exit/stop-loss)
        if signal.signal_type in ("LONG", "SHORT", "EXIT", "STOP_LOSS"):
            get_notifier().notify_signal(signal)

        if signal.signal_type in ("LONG", "SHORT"):
            await self._open_position(signal)
        elif signal.signal_type in ("EXIT", "STOP_LOSS"):
            await self._close_position(signal)

    async def _open_position(self, signal: Signal) -> None:
        """Open a new position."""
        if self.state.current_position != "NONE":
            logger.warning("Already in position, ignoring entry signal")
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': f"Already in {self.state.current_position} position",
            }
            return

        # Check if already executing a trade (prevents duplicate orders)
        if self._executing_trade:
            logger.debug("Trade execution in progress, ignoring signal")
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': "Trade execution already in progress",
            }
            return

        # Check post-stop-loss cooldown
        if self._stop_loss_cooldown_until and datetime.utcnow() < self._stop_loss_cooldown_until:
            remaining = (self._stop_loss_cooldown_until - datetime.utcnow()).total_seconds()
            logger.debug("Stop-loss cooldown active, %.0fs remaining", remaining)
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': f"Stop-loss cooldown ({int(remaining)}s remaining)",
            }
            return

        # Z-score reset gate: after a STOP_LOSS, block same-direction re-entry until
        # the z-score has returned to the exit zone (spread genuinely reverted).
        if (getattr(self.config, 'z_reset_gate_enabled', True)
                and self._z_reset_block_direction
                and signal.signal_type == self._z_reset_block_direction):
            reset_z = getattr(self.config, 'exit_threshold', 0.5)
            z = signal.zscore
            # SHORT reset: z must rise above −reset_z  (back to near-mean or above)
            # LONG  reset: z must fall below +reset_z
            if self._z_reset_block_direction == "SHORT":
                cleared = z >= -reset_z
            else:
                cleared = z <= reset_z
            if cleared:
                logger.info(
                    "Z-reset gate cleared for %s (z=%.4f crossed ±%.2f) — same-side entry allowed",
                    self._z_reset_block_direction, z, reset_z,
                )
                self._z_reset_block_direction = None
            else:
                logger.debug(
                    "Z-reset gate blocking %s re-entry: z=%.4f has not returned to ±%.2f",
                    self._z_reset_block_direction, z, reset_z,
                )
                self.signal_generator.last_blocked_signal = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'would_be_signal': signal.signal_type,
                    'zscore': round(z, 4),
                    'reason': f"Z-reset gate: z={z:.2f} must return to ±{reset_z:.2f} before same-side re-entry",
                }
                return

        # Check general entry cooldown (prevents rapid re-entry after any trade)
        if self._entry_cooldown_until and datetime.utcnow() < self._entry_cooldown_until:
            remaining = (self._entry_cooldown_until - datetime.utcnow()).total_seconds()
            logger.debug("Entry cooldown active, %.0fs remaining", remaining)
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': f"Entry cooldown ({int(remaining)}s remaining)",
            }
            return

        # Check daily loss limit
        if self._check_daily_loss():
            self.state.algo_enabled = False
            self.config.algo_enabled = False
            msg = (f"Daily loss limit ${self.config.daily_max_loss_usd:.0f} reached "
                   f"(lost ${abs(self._daily_loss_usd):.2f} today) — algo disabled")
            logger.critical("🛑 %s", msg)
            self.state.error = msg
            get_notifier().notify_error(msg)
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': 'Daily loss limit reached — algo disabled',
            }
            return

        # SAFETY: Verify no existing position on exchange before entering
        if self.config.verify_exchange_position and not self.state.paper_trading:
            existing_position = await self._check_exchange_position()
            if existing_position:
                logger.warning("Exchange has existing position! Blocking entry. Position: %s", existing_position)
                self.signal_generator.last_blocked_signal = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'would_be_signal': signal.signal_type,
                    'zscore': round(signal.zscore, 4),
                    'reason': f"Exchange already has position: {existing_position}",
                }
                return

        # SAFETY: Check for existing open orders before placing new ones
        # This prevents placing duplicate orders when previous ones are still pending
        if not self.state.paper_trading:
            open_order_count = await self._count_open_orders()
            if open_order_count > 0:
                logger.warning("Exchange already has %d open order(s) - blocking new entry to prevent duplicates",
                               open_order_count)
                # Apply a short cooldown to give time for existing orders to resolve
                self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=30)
                self.signal_generator.last_blocked_signal = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'would_be_signal': signal.signal_type,
                    'zscore': round(signal.zscore, 4),
                    'reason': f"Exchange has {open_order_count} open order(s) already pending",
                }
                return

        if not self.spot_tick or not self.futures_tick:
            logger.warning("No tick data available")
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': "No price data available",
            }
            return

        position_type = signal.signal_type  # LONG or SHORT
        spot_price = self.spot_tick.mid
        futures_price = self.futures_tick.mid

        # Guard: position size must not exceed configured max
        max_size = getattr(self.config, 'max_position_size_usd', float('inf'))
        if self.config.position_size_usd > max_size:
            logger.error("Position size $%.0f exceeds max $%.0f — blocking entry",
                         self.config.position_size_usd, max_size)
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': f"position_size_usd (${self.config.position_size_usd:.0f}) > max (${max_size:.0f})",
            }
            return

        # Guard: live balance must cover both legs' margin (skipped in paper mode).
        # Catches insufficient-balance BEFORE the round-trip to the exchange, so
        # we don't burn doomed orders during a balance shortage and trigger the
        # spot-failure-pattern auto-disable.
        balance_block = await self._check_sufficient_balance(
            signal.signal_type, spot_price, futures_price,
        )
        if balance_block:
            logger.warning("Entry blocked: %s", balance_block)
            self._entry_cooldown_until = datetime.utcnow() + timedelta(
                seconds=max(30, self.config.entry_cooldown_seconds),
            )
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': balance_block,
            }
            return

        # Calculate quantity
        # Leg sizing with hedge ratio (beta). The spot leg is anchored to
        # position_size_usd; the futures leg is scaled by 1/beta so the legs
        # stay dollar-hedged (spot_qty = beta * futures_qty). beta=1 -> both
        # legs equal, identical to the classic same-underlying basis trade.
        # trade.quantity holds the FUTURES quantity, because the spread is
        # expressed in futures-price units (spread = F - beta*S), so P&L is
        # spread_change * futures_qty.
        beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        spot_qty = self.config.position_size_usd / spot_price
        quantity = spot_qty / beta  # futures quantity

        # Lattice co-sizing: land both legs on whole contracts NEAR beta instead
        # of letting the adapters floor each leg independently (which distorts
        # the executed hedge by up to a full contract on the small leg). Only
        # applies when both legs are contract-sized derivatives; fails open.
        if getattr(self.config, 'lattice_sizing_enabled', True) \
                and is_derivative(self.config.spot_symbol) \
                and is_derivative(self.config.futures_symbol):
            try:
                info_a = await self.spot_adapter.get_symbol_info(self.config.spot_symbol)
                info_b = await self.futures_adapter.get_symbol_info(self.config.futures_symbol)
                ct_a = float((info_a or {}).get('contract_val') or 0)
                ct_b = float((info_b or {}).get('contract_val') or 0)
                lat = lattice_leg_sizes(spot_qty, quantity, beta, ct_a, ct_b,
                                        spot_price, futures_price)
                if lat:
                    new_sq, new_fq, a_ct, b_ct = lat
                    floor_a = max(1, int(spot_qty / ct_a + 1e-9)) if ct_a > 0 else 0
                    floor_b = max(1, int(quantity / ct_b + 1e-9)) if ct_b > 0 else 0
                    floor_ratio = (floor_a * ct_a) / (floor_b * ct_b) if floor_b > 0 else 0.0
                    logger.info(
                        "Lattice sizing: Leg A %.6f→%.6f (%d ct), Leg B %.6f→%.6f (%d ct) — "
                        "executed ratio %.2f vs β %.2f (err %.1f%%; independent floor "
                        "would give %.2f, err %.1f%%)",
                        spot_qty, new_sq, a_ct, quantity, new_fq, b_ct,
                        new_sq / new_fq, beta, abs(new_sq / new_fq - beta) / beta * 100,
                        floor_ratio, abs(floor_ratio - beta) / beta * 100 if floor_ratio else 0.0,
                    )
                    spot_qty, quantity = new_sq, new_fq
            except Exception as _le:
                logger.warning("Lattice sizing skipped (symbol info unavailable): %s", _le)

        # Guard #11: BOTH legs must clear their exchange minimums BEFORE we place
        # either order. Without this, a leg that rounds below its minimum (e.g. a
        # futures leg under 1 contract) fails AFTER the other leg has already
        # filled — orphaning it. Block the whole entry atomically instead.
        min_size_block = await self._check_min_leg_sizes(
            spot_qty=spot_qty, futures_qty=quantity,
            spot_price=spot_price, beta=beta,
        )
        if min_size_block:
            logger.warning("Entry blocked: %s", min_size_block)
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': min_size_block,
            }
            return

        # Create trade record. notional_usd and margin_usd are TOTALS across
        # both legs — previously only Leg A was counted (position_size_usd) so
        # Telegram, the Position card, and any "leverage = notional/margin"
        # calc were silently under-reporting by ~2× for a dollar-neutral pair.
        beta_for_sizing = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        leg_a_notional = abs(spot_price * quantity * beta_for_sizing)
        leg_b_notional = abs(futures_price * quantity)
        total_notional = leg_a_notional + leg_b_notional
        leg_a_is_deriv = is_derivative(self.config.spot_symbol)
        leg_b_is_deriv = is_derivative(self.config.futures_symbol)
        leg_a_lev = max(self.config.spot_leverage    if leg_a_is_deriv else 1, 1)
        leg_b_lev = max(self.config.futures_leverage if leg_b_is_deriv else 1, 1)
        total_margin = leg_a_notional / leg_a_lev + leg_b_notional / leg_b_lev

        # Guard #12: R:R entry gate.
        # The stop is derived from target / rr_multiple in _effective_exit_targets.
        # Here we only block if rr_multiple is set but no profit target is configured
        # — without a target we can't derive the stop, so the entry is ambiguous.
        _min_rr = getattr(self.config, 'min_entry_rr_multiple', 0.0) or 0.0
        if _min_rr > 0:
            _sig_frac = getattr(self.config, 'profit_target_sigma_frac', 0.0) or 0.0
            _cap_tp = getattr(self.config, 'profit_target_capital_pct', 0.0) or 0.0
            if _sig_frac > 0:
                _expected_target = (_sig_frac * abs(signal.zscore)
                                    * signal.spread_std * quantity)
            elif _cap_tp > 0:
                _buf = getattr(self.config, 'm2m_buffer_pct', 0.0) or 0.0
                _expected_target = _cap_tp / 100.0 * total_margin * (1 + _buf / 100.0)
            else:
                _expected_target = getattr(self.config, 'profit_target_usd', 0.0) or 0.0
            if _expected_target <= 0:
                _rr_block = (
                    f"R:R gate: min_entry_rr_multiple={_min_rr} is set but all "
                    f"profit-target forms (sigma_frac / capital_pct / usd) are 0 — "
                    f"cannot derive stop loss without a profit target"
                )
                logger.info("Entry blocked: %s", _rr_block)
                self.signal_generator.last_blocked_signal = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'would_be_signal': signal.signal_type,
                    'zscore': round(signal.zscore, 4),
                    'reason': _rr_block,
                }
                return

        trade = Trade(
            asset=self.config.asset,
            position_type=position_type,
            entry_time=datetime.utcnow(),
            entry_spot_price=spot_price,
            entry_futures_price=futures_price,
            entry_spread=signal.spread,
            entry_zscore=signal.zscore,
            entry_spread_mean=signal.spread_mean,
            entry_spread_std=signal.spread_std,
            quantity=quantity,
            # REQUESTED spot size (lattice-adjusted). The executor targets this
            # — without it, it re-derives spot as quantity × β, which discards
            # the lattice's spot leg (1.1 vs 1.0674 ETH), floors to one fewer
            # contract, and the fill-ratio guard then rejects a FILLED entry
            # (93.7% < 95%) whose legs the orphan-closer must then unwind.
            # Overwritten with the actually-filled base quantity after entry.
            spot_qty=spot_qty,
            notional_usd=round(total_notional, 2),
            margin_usd=round(total_margin, 2),
            is_open=True,
            is_paper=self.state.paper_trading,
        )

        # Execute orders if not paper trading
        if not self.state.paper_trading:
            self._executing_trade = True
            self._last_entry_throttled = False  # reset before each attempt
            try:
                success = await self._execute_entry_orders(trade, signal)
                if not success:
                    if self._last_entry_throttled:
                        # cancelSource=31: POST_ONLY price crossed the book — not a rate limit.
                        # Per-leg snap on the next attempt uses a fresh price, so retry quickly.
                        cooldown_sec = self._ENTRY_POSTONLY_RETRY_SEC
                        logger.info(
                            "Entry POST_ONLY rejected (cancelSource=31) — retrying in %ds "
                            "with per-leg price snap", cooldown_sec,
                        )
                    else:
                        cooldown_sec = max(30, getattr(self.config, 'entry_cooldown_seconds', 60))
                    self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=cooldown_sec)
                    logger.warning("Entry orders failed - applying %ds cooldown to prevent rapid retry",
                                   cooldown_sec)
                    return
            finally:
                self._executing_trade = False

        self.open_trade = trade
        self._entry_tick_count = self.signal_generator.total_ticks
        self._hurst_exit_count = 0
        self._velocity_exit_count = 0
        self._spread_velocity_window.clear()
        self._peak_pnl = 0.0
        self._trough_pnl = 0.0
        self._peak_at = None
        self._trough_at = None
        self._z_seen_min = None
        self._z_seen_max = None
        self._gate_hold_count = 0
        self._gate_first_hold = None
        self._exit_gate_last_log = None
        self._z_stop_log_at = None
        self.state.current_position = position_type
        self.signal_generator.set_position(
            position_type,
            entry_mean=signal.spread_mean,
            entry_std=signal.spread_std,
        )

        logger.info("Opened %s position: futures_qty=%.6f, spot_qty=%.6f (beta=%.4f, "
                    "executed ratio=%.2f), spot=%.2f, futures=%.2f, spread=%.6f, zscore=%.4f",
                    position_type, trade.quantity,
                    trade.spot_qty if trade.spot_qty > 0 else spot_qty, beta,
                    (trade.spot_qty / trade.quantity) if (trade.spot_qty > 0 and trade.quantity > 0) else beta,
                    spot_price, futures_price, signal.spread, signal.zscore)

        # The trade's geometry in spread units — the absolute levels where it
        # breaks even / takes profit / stops out. These don't drift with the
        # rolling mean, unlike the z-score; watch the spread against them.
        _lv = self._exit_spread_levels(trade)
        if _lv:
            logger.info(
                "Trade geometry (spread units, favorable=%s): entry %.2f | BE @ %.2f | "
                "TP @ %s | SL @ %s",
                _lv['favorable'], _lv['entry'], _lv['break_even'],
                f"{_lv['take_profit']:.2f}" if _lv['take_profit'] is not None else "off",
                f"{_lv['stop']:.2f}" if _lv['stop'] is not None else "off",
            )

        _details = None
        if _lv:
            try:
                _t = self._effective_exit_targets(trade)
                _details = {
                    'levels': _lv,
                    'target_usd': _t['target_usd'],
                    'stop_usd': _t['stop_usd'],
                    'gate_usd': self._exit_gate_floor(trade),
                    'capital': self._capital_at_risk(trade),
                }
            except Exception:
                _details = None
        get_notifier().notify_trade_entry(trade, signal, details=_details)

        if self.on_trade:
            self.on_trade(trade)

    async def _close_position(self, signal: Signal) -> None:
        """Close current position."""
        if self.state.current_position == "NONE" or not self.open_trade:
            logger.warning("No position to close")
            return

        if not self.spot_tick or not self.futures_tick:
            logger.warning("No tick data available")
            return

        # Throttle exit retries — don't hammer exchange on every tick after a failure.
        # POST_ONLY rejection (cancelSource=31): retry in 10s (no exchange cooldown needed).
        # Other failure: use standard _exit_retry_interval_sec.
        if not self.state.paper_trading and self._last_exit_attempt:
            elapsed = (datetime.utcnow() - self._last_exit_attempt).total_seconds()
            # A DOLLAR_STOP is mid-close: don't linger between the maker probe and the
            # market fallback — a diverging stop must not wait the full 10s.
            if self.open_trade and (self.open_trade.exit_reason or "").upper() == "DOLLAR_STOP":
                interval = self._DOLLAR_STOP_RETRY_SEC
            elif self._exit_postonly_reject_count > 0:
                interval = self._EXIT_POSTONLY_RETRY_SEC
            else:
                interval = self._exit_retry_interval_sec
            if elapsed < interval:
                return

        trade = self.open_trade
        # Mid prices used only for the paper-trading fallback and as initial
        # placeholders on the trade record. The executor will overwrite
        # trade.exit_spot_price / exit_futures_price with REAL fill prices
        # below, and realized P&L is then computed from those fills.
        spot_price = self.spot_tick.mid
        futures_price = self.futures_tick.mid

        trade.exit_time = datetime.utcnow()
        trade.exit_spot_price = spot_price
        trade.exit_futures_price = futures_price
        trade.exit_zscore = signal.zscore
        trade.exit_reason = signal.signal_type
        # A fast-exit override (PROFIT_TARGET / MAX_HOLD / DOLLAR_STOP) carries a
        # more specific reason than the bare EXIT/STOP_LOSS signal type.
        if self._override_exit_reason:
            trade.exit_reason = self._override_exit_reason
            self._override_exit_reason = None

        # Execute orders BEFORE marking closed — if orders fail we leave the
        # position open so the engine retries on the next tick rather than
        # silently leaving an unclosed position on the exchange.
        if not self.state.paper_trading:
            self._executing_trade = True
            self._last_exit_attempt = datetime.utcnow()
            try:
                exit_ok = await self._execute_exit_orders(trade, signal)
            except Exception as exc:
                logger.exception("Exit orders raised an exception: %s", exc)
                exit_ok = False
            finally:
                self._executing_trade = False

            if not exit_ok:
                # DOLLAR_STOP maker phase: count every failed MAKER attempt — a
                # cancelSource=31 rejection OR a rest-without-fill timeout (the latter
                # does NOT set _last_exit_postonly_rejected below) — so the stop still
                # escalates to a guaranteed MARKET close after N tries instead of
                # resting forever while the spread runs.
                if (trade.exit_reason or "").upper() == "DOLLAR_STOP" and not self._last_exit_was_market:
                    self._dollar_stop_maker_attempts += 1
                    logger.warning(
                        "DOLLAR_STOP maker attempt %d/%d did not fill — %s",
                        self._dollar_stop_maker_attempts, self._DOLLAR_STOP_MAKER_ATTEMPTS,
                        "MARKET on next attempt"
                        if self._dollar_stop_maker_attempts >= self._DOLLAR_STOP_MAKER_ATTEMPTS
                        else "retrying maker",
                    )
                if self._last_exit_postonly_rejected:
                    self._exit_postonly_reject_count += 1
                    logger.warning(
                        "Exit POST_ONLY rejected (cancelSource=31) — price crossed spread at "
                        "placement (rejection #%d). Retry in %ds%s",
                        self._exit_postonly_reject_count,
                        self._EXIT_POSTONLY_RETRY_SEC,
                        "; switching to MARKET on next attempt"
                        if self._exit_postonly_reject_count >= self._EXIT_POSTONLY_MARKET_AFTER
                        else "",
                    )
                else:
                    self._exit_postonly_reject_count = 0
                logger.error(
                    "Exit orders FAILED for %s position — leaving position open for retry",
                    trade.position_type,
                )
                return  # Do NOT reset state; engine retries after _exit_retry_interval_sec

        self._last_exit_attempt = None           # Clear retry timer on success
        self._exit_postonly_reject_count = 0     # Clear POST_ONLY rejection counter on success
        self._dollar_stop_maker_attempts = 0     # Clear DOLLAR_STOP maker-attempt counter on success
        trade.is_open = False

        # ── Realized P&L from ACTUAL fills (now that the executor has stamped
        # them onto trade.exit_spot_price / exit_futures_price). Computed PER
        # LEG from the actually-executed quantities — the same accounting OKX
        # does, so the dashboard matches the exchange to the cent. Falls back
        # to beta-derived spot qty for legacy/paper trades (identical result
        # when the executed ratio equals beta).
        beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        spot_qty = trade.spot_qty if trade.spot_qty > 0 else trade.quantity * beta
        # Fill-derived spreads (fut − β × spot) — kept for the audit-trail
        # stamps below and the z/spread displays; P&L itself is per-leg.
        entry_spread_fills = trade.entry_futures_price - beta * trade.entry_spot_price
        exit_spread_fills  = trade.exit_futures_price  - beta * trade.exit_spot_price
        pnl_gross = per_leg_gross_pnl(
            trade.position_type, spot_qty, trade.quantity,
            trade.entry_spot_price, trade.entry_futures_price,
            trade.exit_spot_price, trade.exit_futures_price,
        )

        # Per-leg fee bps from the same schedule the signal filter uses, so
        # cost estimates and realized P&L can never silently diverge.
        spot_maker = getattr(self.config, 'spot_maker_fee_bps', self.config.maker_fee_bps)
        spot_taker = getattr(self.config, 'spot_taker_fee_bps', self.config.taker_fee_bps)
        fut_maker  = getattr(self.config, 'futures_maker_fee_bps', self.config.maker_fee_bps)
        fut_taker  = getattr(self.config, 'futures_taker_fee_bps', self.config.taker_fee_bps)
        entry_mode = getattr(self.config, 'entry_execution_mode', self.config.order_execution_mode)
        # Use actual exit execution mode: MARKET (taker) when the exit was forced
        # to MARKET after a POST_ONLY rejection — not the config default.
        exit_mode  = "MARKET" if self._last_exit_was_market else getattr(self.config, 'exit_execution_mode', self.config.order_execution_mode)
        leg_a_deriv = is_derivative(self.config.spot_symbol)
        leg_b_deriv = is_derivative(self.config.futures_symbol)
        def _bps(deriv, mode):
            if mode == "LIMIT":
                return fut_maker if deriv else spot_maker
            return fut_taker if deriv else spot_taker
        a_entry, b_entry = _bps(leg_a_deriv, entry_mode), _bps(leg_b_deriv, entry_mode)
        a_exit,  b_exit  = _bps(leg_a_deriv, exit_mode),  _bps(leg_b_deriv, exit_mode)
        fees_estimated = (
            a_entry / 10000.0 * spot_qty       * trade.entry_spot_price +
            b_entry / 10000.0 * trade.quantity * trade.entry_futures_price +
            a_exit  / 10000.0 * spot_qty       * trade.exit_spot_price +
            b_exit  / 10000.0 * trade.quantity * trade.exit_futures_price
        )
        actual_fees = trade.entry_fees_usd + trade.exit_fees_usd
        if actual_fees > 0:
            fees_usd = actual_fees
            logger.info("Using actual OKX fees: $%.4f (entry=$%.4f exit=$%.4f) vs estimated $%.4f",
                        fees_usd, trade.entry_fees_usd, trade.exit_fees_usd, fees_estimated)
        else:
            fees_usd = fees_estimated
            logger.debug("Using estimated fees: $%.4f (actual not captured)", fees_usd)

        pnl = pnl_gross - fees_usd
        pnl_percent = (pnl / trade.notional_usd) * 100 if trade.notional_usd > 0 else 0

        # Return on actually-locked capital (margin per leg + M2M buffer). Spot
        # legs at 1× lev use full notional; derivative legs use notional/leverage.
        leg_a_notional = abs(trade.entry_spot_price * spot_qty)
        leg_b_notional = abs(trade.entry_futures_price * trade.quantity)
        leg_a_lev = max(self.config.spot_leverage    if leg_a_deriv else 1, 1)
        leg_b_lev = max(self.config.futures_leverage if leg_b_deriv else 1, 1)
        margin_a = leg_a_notional / leg_a_lev
        margin_b = leg_b_notional / leg_b_lev
        buffer_pct = getattr(self.config, 'm2m_buffer_pct', 0.0) or 0.0
        capital_locked = (margin_a + margin_b) * (1 + buffer_pct / 100.0)
        pnl_pct_on_capital = (pnl / capital_locked) * 100 if capital_locked > 0 else 0

        trade.pnl_usd = pnl
        trade.pnl_percent = pnl_percent
        trade.pnl_gross_usd = pnl_gross
        trade.fees_usd = fees_usd
        trade.capital_locked_usd = capital_locked
        trade.pnl_pct_on_capital = pnl_pct_on_capital
        trade.actual_entry_mode = entry_mode
        trade.actual_exit_mode  = exit_mode
        # Audit trail: store fill-derived spreads so the DB matches OKX
        trade.entry_spread = entry_spread_fills
        trade.exit_spread  = exit_spread_fills

        # Daily-loss tracker tracks NET realized P&L
        self._daily_loss_usd += pnl

        logger.info(
            "Closed %s position: net=$%.2f (notional %.2f%% / capital %.2f%% on $%.2f) "
            "= gross $%.2f − fees $%.2f, reason=%s, zscore=%.4f",
            trade.position_type, pnl, pnl_percent, pnl_pct_on_capital, capital_locked,
            pnl_gross, fees_usd, signal.signal_type, signal.zscore,
        )

        # Trade lifecycle scorecard — exact numbers for the crisp post-trade
        # analysis on Telegram and in the AI review. Built BEFORE state reset.
        try:
            held_min = ((trade.exit_time - trade.entry_time).total_seconds() / 60.0
                        if trade.entry_time and trade.exit_time else 0.0)
            avail_usd = (abs(trade.entry_zscore or 0.0)
                         * (trade.entry_spread_std or 0.0) * (trade.quantity or 0.0))
            gate_held_min = ((datetime.utcnow() - self._gate_first_hold).total_seconds() / 60.0
                             if self._gate_first_hold else 0.0)

            def _min_since_entry(ts):
                if ts is None or not trade.entry_time:
                    return None
                return round((ts - trade.entry_time).total_seconds() / 60.0, 1)

            _tg = self._effective_exit_targets(trade)
            trade.lifecycle_stats = {
                'peak_net': round(self._peak_pnl, 2),
                'trough_net': round(self._trough_pnl, 2),
                'peak_min': _min_since_entry(self._peak_at),
                'trough_min': _min_since_entry(self._trough_at),
                'stop_usd': round(_tg['stop_usd'], 2) if _tg['stop_usd'] > 0 else 0.0,
                'stop_z': getattr(self.config, 'stop_loss_threshold', 0.0) or 0.0,
                'z_min': self._z_seen_min,
                'z_max': self._z_seen_max,
                'gate_holds': self._gate_hold_count,
                'gate_held_min': round(gate_held_min, 1),
                'gate_floor': round(self._exit_gate_floor(trade) or 0.0, 2),
                'held_min': round(held_min, 1),
                'max_hold_min': round(self._max_hold_minutes_for(trade), 1),
                'available_usd': round(avail_usd, 2),
                'exit_threshold': getattr(self.config, 'exit_threshold', 0.5) or 0.5,
            }
        except Exception:
            trade.lifecycle_stats = None

        # Persist the extremes on the trade row itself so "did profit come
        # before the loss?" is answerable across history, not just per message.
        _ls = getattr(trade, 'lifecycle_stats', None) or {}
        trade.peak_net_usd = _ls.get('peak_net', 0.0) or 0.0
        trade.trough_net_usd = _ls.get('trough_net', 0.0) or 0.0
        trade.peak_minutes = _ls.get('peak_min')
        trade.trough_minutes = _ls.get('trough_min')

        get_notifier().notify_trade_exit(trade, stats=getattr(trade, 'lifecycle_stats', None))

        # Reset state
        self.state.current_position = "NONE"
        self.signal_generator.set_position("NONE")
        self.open_trade = None
        self._entry_tick_count = None
        self._hurst_exit_count = 0
        self._velocity_exit_count = 0
        self._spread_velocity_window.clear()
        self._peak_pnl = 0.0

        # Apply post-stop-loss cooldown to prevent immediate re-entry
        if signal.signal_type == "STOP_LOSS":
            from datetime import timedelta
            self._stop_loss_cooldown_until = datetime.utcnow() + timedelta(seconds=self._stop_loss_cooldown_sec)
            logger.info("Stop-loss cooldown active for %ds", self._stop_loss_cooldown_sec)
            # Z-reset gate: block same-direction re-entry until z-score returns to
            # the exit zone (±exit_threshold), preventing re-entry into a trending market.
            closed_direction = trade.position_type  # "SHORT" or "LONG"
            if getattr(self.config, 'z_reset_gate_enabled', True):
                self._z_reset_block_direction = closed_direction
            reset_z = getattr(self.config, 'exit_threshold', 0.5)
            logger.info(
                "Z-reset gate armed for %s: waiting for z to return to ±%.2f before same-side re-entry",
                closed_direction, reset_z,
            )

        # Apply general entry cooldown after any trade
        from datetime import timedelta
        cooldown_sec = getattr(self.config, 'entry_cooldown_seconds', 60)
        if cooldown_sec > 0:
            self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=cooldown_sec)
            logger.info("Entry cooldown active for %ds", cooldown_sec)

        if self.on_trade:
            self.on_trade(trade)

    async def _check_min_leg_sizes(self, *, spot_qty: float, futures_qty: float,
                                    spot_price: float, beta: float) -> Optional[str]:
        """Return ``None`` if both legs clear their exchange minimum order size,
        else a human-readable reason string.

        Per-leg minimum (in base units):
          - derivative leg (SWAP / dated FUTURES): 1 contract = ``ctVal``
          - spot leg: the instrument's ``min_qty``

        Also computes the minimum position_size_usd that *would* let both legs
        clear, so the operator knows exactly how much to raise size to. Fails
        open (returns None) if symbol info can't be fetched — better to let the
        exchange be the final arbiter than to block all trades on a metadata
        hiccup.
        """
        legs = [
            ("Leg A", self.config.spot_symbol, spot_qty, self.spot_adapter, 1.0),
            ("Leg B", self.config.futures_symbol, futures_qty, self.futures_adapter, beta),
        ]
        problems = []
        min_viable_usd = 0.0

        for label, symbol, base_qty, adapter, leg_beta in legs:
            if not adapter or not hasattr(adapter, 'get_symbol_info'):
                continue  # fail open — can't determine the minimum
            try:
                info = await adapter.get_symbol_info(symbol)
            except Exception as e:
                logger.warning("%s (%s) symbol-info lookup failed — skipping min-size check: %s",
                               label, symbol, e)
                info = None
            if not info:
                continue  # fail open

            if is_derivative(symbol):
                unit = float(info.get("contract_val") or 0) or 0.0
                unit_desc = f"{unit:g} (1 contract)"
            else:
                unit = float(info.get("min_qty") or 0) or 0.0
                unit_desc = f"{unit:g} (min order)"

            if unit <= 0:
                continue  # unknown minimum → fail open

            # Position size needed for THIS leg to clear: base_qty scales linearly
            # with position_size_usd, so required size = unit * (current size / base_qty).
            if base_qty > 0:
                leg_required_usd = unit * (self.config.position_size_usd / base_qty)
                min_viable_usd = max(min_viable_usd, leg_required_usd)

            if base_qty < unit:
                base_ccy = symbol.split("-")[0]
                problems.append(
                    f"{label} ({symbol}) size {base_qty:.8f} {base_ccy} is below the "
                    f"minimum {unit_desc}"
                )

        if problems:
            hint = ""
            if min_viable_usd > 0:
                # Round UP with a small buffer so the suggested size actually
                # clears the boundary (the raw value is the exact minimum, which
                # rounds down to just under 1 contract).
                suggested = math.ceil(min_viable_usd * 1.02)
                hint = (f" — raise Position Size to at least "
                        f"${suggested:,.0f} for this pair, or pick instruments "
                        f"with smaller minimums")
            return "Below minimum order size: " + "; ".join(problems) + hint
        return None

    async def _check_sufficient_balance(self, signal_type: str,
                                         spot_price: float,
                                         futures_price: float) -> Optional[str]:
        """Return ``None`` if the account has enough balance to fund both legs,
        or a human-readable reason string if the trade should be blocked.

        Compares the live ``available_balance_usd`` from the exchange against the
        sum of per-leg margin requirements:

          margin = notional / leverage   (derivative legs)
          margin = notional              (spot legs — full cash)

        Includes a 10% safety buffer for fees, slippage, and brief price drift
        between this check and order placement. **Fails open** when the adapter
        doesn't support balance fetch, returns no data, or errors — better to
        attempt the trade and let the exchange refuse than to refuse all trades
        because of a transient API blip. Skipped entirely in paper mode.

        Cached for ``_BALANCE_CACHE_TTL_SEC`` so a flurry of signals during
        balance shortage doesn't burn account-info calls.
        """
        if self.state.paper_trading:
            return None
        if not self.futures_adapter or not hasattr(self.futures_adapter, 'get_account_info'):
            return None

        now = datetime.utcnow()
        cached = self._balance_cache
        if cached and (now - cached[0]).total_seconds() < self._BALANCE_CACHE_TTL_SEC:
            available = cached[1]
        else:
            try:
                account = await self.futures_adapter.get_account_info()
                if not account:
                    return None  # fail open
                available = float(getattr(account, 'available_balance_usd', 0.0) or 0.0)
                self._balance_cache = (now, available)
            except Exception as e:
                logger.warning("Balance check failed — failing open: %s", e)
                return None

        # Per-leg margin: notional / leverage for derivative legs, full notional for cash legs.
        # Leg B notional reflects the actual β-scaled exposure, so a misconfigured β
        # (where Leg B notional balloons) gets caught here too.
        notional_a = self.config.position_size_usd
        beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        if spot_price > 0 and futures_price > 0:
            notional_b = self.config.position_size_usd * (futures_price / (beta * spot_price))
        else:
            notional_b = self.config.position_size_usd

        leg_a_lev = max(self.config.spot_leverage    if is_derivative(self.config.spot_symbol)    else 1, 1)
        leg_b_lev = max(self.config.futures_leverage if is_derivative(self.config.futures_symbol) else 1, 1)
        margin_a = notional_a / leg_a_lev
        margin_b = notional_b / leg_b_lev
        buffer_pct = max(getattr(self.config, 'm2m_buffer_pct', 10.0) or 0.0, 0.0)
        required = (margin_a + margin_b) * (1 + buffer_pct / 100)

        if available < required:
            short_by = required - available
            return (
                f"Insufficient balance: ${available:,.2f} available, "
                f"${required:,.2f} needed "
                f"(Leg A margin ${margin_a:,.0f} + Leg B margin ${margin_b:,.0f} "
                f"+ {buffer_pct:.0f}% M2M buffer) "
                f"— short by ${short_by:,.2f}"
            )
        return None

    async def _check_exchange_position(self) -> Optional[str]:
        """
        Check if there's an existing position on the exchange.

        Returns a description of the position if one exists, None otherwise.
        This prevents duplicate entries when engine state is out of sync.
        """
        try:
            existing_positions = []

            # Check futures positions
            if self.futures_adapter and hasattr(self.futures_adapter, 'get_positions'):
                positions = await self.futures_adapter.get_positions(self.config.futures_symbol)
                for pos in positions:
                    if pos.quantity > 0:
                        existing_positions.append(f"Futures {pos.side} {pos.quantity:.6f}")

            # Check spot balance (simplified - just check if we have the asset)
            # Note: For spot, we'd need to track what was bought for arbitrage vs held
            # For now, we focus on futures positions which are clearer indicators

            if existing_positions:
                return ", ".join(existing_positions)

            return None

        except Exception as e:
            logger.warning("Error checking exchange positions: %s", e)
            # On error, allow the trade but log warning
            return None

    async def _count_open_orders(self) -> int:
        """
        Count open/pending orders on the exchange for the current symbols.

        Used to prevent placing new orders when previous ones are still pending.
        Returns the total count across spot and futures.
        On error, returns 0 (fail open - allow trading rather than blocking indefinitely).
        """
        count = 0
        try:
            # Use get_pending_orders which queries /api/v5/trade/orders-pending
            if self.futures_adapter and hasattr(self.futures_adapter, 'get_pending_orders'):
                futures_orders = await self.futures_adapter.get_pending_orders(
                    symbol=self.config.futures_symbol
                )
                count += len(futures_orders) if futures_orders else 0

            if self.spot_adapter and hasattr(self.spot_adapter, 'get_pending_orders'):
                spot_orders = await self.spot_adapter.get_pending_orders(
                    symbol=self.config.spot_symbol
                )
                count += len(spot_orders) if spot_orders else 0

            if count > 0:
                logger.warning("Found %d open/pending order(s) on exchange - blocking new entry", count)

        except Exception as e:
            logger.warning("Error counting open orders: %s", e)

        return count

    async def verify_position_sync(self) -> Dict[str, Any]:
        """
        Verify that engine position state matches exchange positions.

        This is called periodically to detect orphaned/lost positions.
        Returns a dict with mismatch info if any discrepancy is found.
        """
        result = {
            'checked': True,
            'mismatch': False,
            'engine_position': self.state.current_position,
            'exchange_positions': [],
            'mismatch_reason': None,
        }

        try:
            # Get actual exchange positions
            if self.futures_adapter and hasattr(self.futures_adapter, 'get_positions'):
                positions = await self.futures_adapter.get_positions()

                for pos in positions:
                    if pos.quantity > 0:
                        # Only track SWAP/FUTURES positions.
                        # get_positions() returns ALL margin positions including pre-existing
                        # spot margin positions that are not managed by this bot.
                        # Spot margin positions can be $10M+ and must not be auto-closed.
                        # is_derivative covers SWAP *and* dated FUTURES (e.g.
                        # BTC-USDT-260626). The old substring check missed dated
                        # futures entirely, so the monitor classified a real
                        # futures position as 'not futures', ignored it, declared
                        # the exchange flat, and force-cleared the engine — orphaning
                        # the live position. Spot-margin symbols (BTC-USDT) remain
                        # excluded, so they're still never auto-touched.
                        if not is_derivative(pos.symbol):
                            logger.debug(
                                "Ignoring non-futures position in mismatch check: %s %s qty=%.4f",
                                pos.side, pos.symbol, pos.quantity,
                            )
                            continue
                        result['exchange_positions'].append({
                            'symbol': pos.symbol,
                            'side': pos.side,
                            'quantity': pos.quantity,
                            'entry_price': pos.entry_price,
                            'unrealized_pnl': pos.unrealized_pnl,
                        })

            engine_has_position = self.state.current_position != "NONE"
            exchange_has_position = len(result['exchange_positions']) > 0

            # Check for mismatches
            if engine_has_position and not exchange_has_position:
                result['mismatch'] = True
                result['mismatch_reason'] = "Engine shows position but exchange has none (manually closed?)"
                self._orphan_mismatch_count += 1
                logger.warning(
                    "Position mismatch: Engine=%s but exchange has no positions [count=%d/%d]",
                    self.state.current_position,
                    self._orphan_mismatch_count, self._orphan_auto_close_threshold,
                )
                if self._orphan_mismatch_count >= self._orphan_auto_close_threshold:
                    logger.critical(
                        "Exchange flat for %d consecutive checks — force-clearing engine %s state",
                        self._orphan_mismatch_count, self.state.current_position,
                    )
                    if self.open_trade:
                        self.open_trade.is_open = False
                        self.open_trade.exit_reason = "MISMATCH_AUTO_CLEAR"
                    self.state.current_position = "NONE"
                    self.open_trade = None
                    self._entry_tick_count = None
                    self._last_exit_attempt = None
                    self._orphan_mismatch_count = 0
                    get_notifier().notify_error(
                        "Engine position force-cleared: exchange showed flat for "
                        f"{self._orphan_auto_close_threshold} consecutive checks"
                    )

            elif not engine_has_position and exchange_has_position:
                result['mismatch'] = True
                total_size = sum(p['quantity'] for p in result['exchange_positions'])
                total_pnl = sum(p['unrealized_pnl'] for p in result['exchange_positions'])
                result['mismatch_reason'] = f"Exchange has {len(result['exchange_positions'])} position(s) but engine shows FLAT"
                self._orphan_mismatch_count += 1
                logger.warning(
                    "Position mismatch: Engine=FLAT but exchange has %d positions "
                    "(size=%.2f, PnL=%.2f) [orphan_count=%d/%d]",
                    len(result['exchange_positions']), total_size, total_pnl,
                    self._orphan_mismatch_count, self._orphan_auto_close_threshold,
                )
                if self._orphan_mismatch_count >= self._orphan_auto_close_threshold:
                    logger.warning(
                        "Orphan threshold reached (%d checks) — auto-closing %d orphan position(s)",
                        self._orphan_mismatch_count, len(result['exchange_positions']),
                    )
                    await self._auto_close_orphan_positions(result['exchange_positions'])
                    self._orphan_mismatch_count = 0  # reset after attempt
            else:
                # No mismatch: reset orphan counter
                self._orphan_mismatch_count = 0

            # Store mismatch state for status reporting
            self._position_mismatch = result if result['mismatch'] else None
            self._last_position_verify = datetime.utcnow()

            return result

        except Exception as e:
            logger.warning("Error verifying position sync: %s", e)
            result['error'] = str(e)
            return result

    async def _periodic_position_check(self) -> None:
        """Run position verification if enough time has passed."""
        now = datetime.utcnow()

        if self._last_position_verify is None:
            # First check - do it
            await self.verify_position_sync()
        elif (now - self._last_position_verify).total_seconds() >= self._position_verify_interval:
            # Time for another check
            await self.verify_position_sync()

    async def _auto_close_orphan_positions(self, orphan_positions: list) -> None:
        """
        Close futures positions the engine has no record of (orphan state).

        Called after N consecutive mismatch detections to limit losses on stuck
        positions. Uses OKX's close-position endpoint (instId + posSide) so the
        exchange sizes the reduce internally — no contract/coin conversion here.
        """
        if not self.futures_adapter:
            logger.error("Cannot auto-close orphans: no futures adapter")
            return

        for pos in orphan_positions:
            symbol = pos['symbol']
            side = pos['side']    # "LONG" or "SHORT"
            qty = pos['quantity'] # base coin (BTC) as reported by OKX; for logging only

            # Safety: only auto-close SWAP / dated FUTURES positions.
            # Spot margin positions on the account are not bot-managed and
            # must never be touched by auto-close (could be $M+ user positions).
            if not is_derivative(symbol):
                logger.warning(
                    "AUTO-CLOSE skipped: %s is not a SWAP/FUTURES position — "
                    "only bot-managed futures orphans are auto-closed. "
                    "Close this position manually if needed.",
                    symbol,
                )
                continue

            # Close via OKX's close-position endpoint (whole-position, reduce-only
            # by construction) instead of hand-computing a contract size.
            #
            # The old path called place_order() with a BTC quantity we derived as
            # `qty * ctVal`, which assumed get_positions() reports SWAP size in
            # CONTRACTS. It doesn't: OKX returns `pos` for this account in base coin
            # (0.01 BTC == 1 contract), so `0.01 * 0.01 = 0.0001 BTC` →
            # int(0.0001 / 0.01) = 0 contracts → "need at least 1 contract", and the
            # orphan could never close — permanently blocking new entries. close-position
            # takes only instId + posSide and lets OKX size the reduce internally, so it
            # is immune to the contract/coin ambiguity that broke this path.
            logger.warning(
                "AUTO-CLOSE orphan %s %s: size=%.6f, PnL=%.2f (via close-position)",
                side, symbol, qty, pos['unrealized_pnl'],
            )

            result = await self.futures_adapter.close_position(symbol, pos_side=side)

            if result.success:
                # Cleanup costs are real money that appears in NO trade record.
                # Book them: count against the daily-loss circuit breaker and
                # emit to the untracked-close ledger for dashboard visibility.
                upl = float(pos.get('unrealized_pnl') or 0.0)
                fee_est = 0.0
                try:
                    taker_bps = getattr(self.config, 'futures_taker_fee_bps',
                                        self.config.taker_fee_bps)
                    entry_px = float(pos.get('entry_price') or 0.0)
                    if entry_px > 0:
                        fee_est = taker_bps / 10000.0 * qty * entry_px
                except Exception:
                    fee_est = 0.0
                self._daily_loss_usd += upl - fee_est
                logger.warning(
                    "AUTO-CLOSE SUCCESS: closed orphan %s %s — ledgered pnl≈$%.2f "
                    "− taker fee≈$%.2f (daily P&L now $%.2f)",
                    side, symbol, upl, fee_est, self._daily_loss_usd,
                )
                if self.on_untracked_close:
                    try:
                        self.on_untracked_close({
                            'source': 'ORPHAN_AUTO_CLOSE',
                            'symbol': symbol,
                            'side': side,
                            'quantity': qty,
                            'pnl_usd': round(upl, 4),
                            'fee_est_usd': round(fee_est, 4),
                            'note': 'engine=FLAT mismatch; closed via close-position',
                        })
                    except Exception as _ue:
                        logger.warning("untracked-close ledger write failed: %s", _ue)
            else:
                logger.error(
                    "AUTO-CLOSE FAILED for %s %s: %s", side, symbol, result.error
                )

    async def _verify_leverage_settings(self) -> bool:
        """
        Verify exchange leverage matches our config for every derivative leg.

        Walks both Leg A (config.spot_symbol) and Leg B (config.futures_symbol)
        and applies the configured leverage to each one that's a perpetual swap
        or dated future — covers futures/futures, calendar spreads, and the
        classic basis trade equally.

        Returns True if every derivative leg's leverage matches config or was
        successfully corrected.
        """
        if not self.futures_adapter:
            return True

        # Enforce safety cap before any exchange interaction
        if self.config.futures_leverage > MAX_SAFE_FUTURES_LEVERAGE:
            logger.warning("Leverage %dx exceeds safety cap %dx — capping before verify",
                           self.config.futures_leverage, MAX_SAFE_FUTURES_LEVERAGE)
            self.config.futures_leverage = MAX_SAFE_FUTURES_LEVERAGE
            if self.on_config_corrected:
                self.on_config_corrected(self.config)

        # Collect derivative legs only; spot legs have no leverage to set
        legs = []
        if is_derivative(self.config.spot_symbol):
            legs.append(("Leg A", self.config.spot_symbol))
        if is_derivative(self.config.futures_symbol):
            legs.append(("Leg B", self.config.futures_symbol))

        if not legs:
            logger.info("No derivative legs configured — leverage check skipped")
            return True

        try:
            if not hasattr(self.futures_adapter, 'get_leverage'):
                return True
            all_ok = True
            for label, sym in legs:
                current = await self.futures_adapter.get_leverage(sym)
                if current is not None and current != self.config.futures_leverage:
                    logger.warning("%s (%s) leverage mismatch: exchange=%dx, config=%dx — correcting",
                                   label, sym, current, self.config.futures_leverage)
                    success = await self.futures_adapter.set_leverage(
                        sym, self.config.futures_leverage,
                    )
                    if success:
                        logger.info("%s (%s) leverage corrected to %dx",
                                    label, sym, self.config.futures_leverage)
                    else:
                        logger.warning("%s (%s): exchange rejected %dx — adopting actual %dx",
                                       label, sym, self.config.futures_leverage, current)
                        self.config.futures_leverage = current
                        if self.on_config_corrected:
                            self.on_config_corrected(self.config)
                        all_ok = False
                else:
                    logger.info("✅ %s (%s) leverage verified: %dx",
                                label, sym, self.config.futures_leverage)
            return all_ok
        except Exception as e:
            logger.error("Error verifying leverage: %s", e)
            return True  # Don't block trading on verification error

    async def _execute_entry_orders(self, trade: Trade, signal: Signal) -> bool:
        """Execute entry orders on exchanges using the order executor.

        When entry_slices > 1, splits the entry into N equal child slices
        placed synchronously on both legs together (synchronized pair TWAP).
        Entry prices are blended via VWAP across all slices. If total fills
        fall below min_fill_ratio the entry is rejected.
        """
        if not self.order_executor:
            logger.error("Order executor not configured for live trading")
            return False

        if not self.spot_tick or not self.futures_tick:
            logger.error("No tick data available for order execution")
            return False

        # Verify leverage settings before trading
        await self._verify_leverage_settings()

        # Track order attempts
        self._spot_order_attempts += 1
        self._futures_order_attempts += 1

        try:
            beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
            n_slices = max(1, getattr(self.config, 'entry_slices', 1))
            slice_interval = max(0.0, getattr(self.config, 'entry_slice_interval_sec', 5.0))
            min_fill_ratio = getattr(self.config, 'min_fill_ratio', 0.95)

            # SWAP/FUTURES legs report fills in CONTRACTS; spot legs in base
            # units. Resolve the contracts→base multiplier per leg so fill
            # ratios and recorded quantities are unit-correct (without this,
            # 20 contracts vs a 2.01-ETH target reads as a 992% fill ratio).
            spot_unit = fut_unit = 1.0
            try:
                if is_derivative(self.config.spot_symbol):
                    _ia = await self.spot_adapter.get_symbol_info(self.config.spot_symbol)
                    spot_unit = float((_ia or {}).get('contract_val') or 0) or 1.0
                if is_derivative(self.config.futures_symbol):
                    _ib = await self.futures_adapter.get_symbol_info(self.config.futures_symbol)
                    fut_unit = float((_ib or {}).get('contract_val') or 0) or 1.0
            except Exception as _ue:
                logger.warning("contract_val lookup failed (%s) — fill quantities "
                               "treated as base units", _ue)

            target_spot_qty = trade.spot_qty if trade.spot_qty > 0 else trade.quantity * beta
            target_fut_qty = trade.quantity
            slice_spot_qty = target_spot_qty / n_slices
            slice_fut_qty = target_fut_qty / n_slices

            # VWAP accumulators
            spot_filled_qty = 0.0
            spot_filled_notional = 0.0
            fut_filled_qty = 0.0
            fut_filled_notional = 0.0
            last_spot_order_id = ""
            last_fut_order_id = ""
            last_spread_order = None

            for slice_i in range(n_slices):
                if slice_i > 0 and slice_interval > 0:
                    await asyncio.sleep(slice_interval)

                spread_order = await self.order_executor.execute_entry(
                    position_type=signal.signal_type,
                    spot_tick=self.spot_tick,
                    futures_tick=self.futures_tick,
                    quantity=slice_spot_qty,
                    futures_quantity=slice_fut_qty,
                )
                last_spread_order = spread_order

                if spread_order and (spread_order.is_complete or (
                    spread_order.spot_leg.status.name == 'FILLED'
                    and spread_order.futures_leg.status.name == 'FILLED'
                    and spread_order.spot_leg.filled_qty > 0
                    and spread_order.futures_leg.filled_qty > 0
                )):
                    sq = spread_order.spot_leg.filled_qty
                    sp = spread_order.spot_leg.filled_price
                    fq = spread_order.futures_leg.filled_qty
                    fp = spread_order.futures_leg.filled_price
                    spot_filled_qty += sq
                    spot_filled_notional += sq * sp
                    fut_filled_qty += fq
                    fut_filled_notional += fq * fp
                    last_spot_order_id = spread_order.spot_leg.order_id
                    last_fut_order_id = spread_order.futures_leg.order_id
                    logger.info(
                        "TWAP slice %d/%d filled: spot %.6f @ %.2f | fut %.6f @ %.2f",
                        slice_i + 1, n_slices, sq, sp, fq, fp,
                    )
                else:
                    logger.warning(
                        "TWAP slice %d/%d failed — accepting fills so far and stopping",
                        slice_i + 1, n_slices,
                    )
                    break

            # Check min fill ratio (fills converted to BASE units first — the
            # executor reports SWAP fills in contracts)
            spot_filled_base = spot_filled_qty * spot_unit
            fut_filled_base = fut_filled_qty * fut_unit
            if target_spot_qty > 0 and (spot_filled_base / target_spot_qty) < min_fill_ratio:
                logger.error(
                    "Entry fill ratio %.1f%% (%.6f of %.6f base) below min_fill_ratio %.1f%% — rejecting entry",
                    100.0 * spot_filled_base / target_spot_qty,
                    spot_filled_base, target_spot_qty,
                    100.0 * min_fill_ratio,
                )
                self._last_entry_throttled = getattr(last_spread_order, 'throttled', False) if last_spread_order else False
                return False
            if spot_filled_qty == 0 or fut_filled_qty == 0:
                logger.error("No fills received across all slices")
                self._last_entry_throttled = getattr(last_spread_order, 'throttled', False) if last_spread_order else False
                return False

            # Blended VWAP prices across all completed slices
            vwap_spot_price = spot_filled_notional / spot_filled_qty
            vwap_fut_price = fut_filled_notional / fut_filled_qty
            spread_order = last_spread_order

            # Defensive: log the executor's final state so we can audit when
            # is_complete disagrees with the underlying leg statuses. The
            # 06/18 07:25-07:37 cascade hit a state where `Recovery SUCCESS`
            # was logged immediately followed by `Spread order failed or
            # incomplete` — the engine then auto-closed the recovered
            # positions as orphans. This print makes the actual leg statuses
            # at the decision boundary visible.
            if spread_order:
                from core.order_executor import LegStatus as _LS
                logger.info(
                    "EXECUTOR RETURNED: spot.status=%s qty=%s price=%s | fut.status=%s qty=%s price=%s | is_complete=%s",
                    spread_order.spot_leg.status.name,
                    spread_order.spot_leg.filled_qty,
                    spread_order.spot_leg.filled_price,
                    spread_order.futures_leg.status.name,
                    spread_order.futures_leg.filled_qty,
                    spread_order.futures_leg.filled_price,
                    spread_order.is_complete,
                )
                # SAFETY NET: if both legs report FILLED but is_complete is
                # False (state-machine bug), trust the leg statuses. Without
                # this, the engine treats a successfully-recovered position
                # as a "failed" entry, fails to record the trade, and the
                # orphan-detector then auto-closes the position 3 ticks
                # later at MARKET — eating the user's money on every cycle.
                if not spread_order.is_complete:
                    both_filled = (
                        spread_order.spot_leg.status == _LS.FILLED
                        and spread_order.futures_leg.status == _LS.FILLED
                        and spread_order.spot_leg.filled_qty > 0
                        and spread_order.futures_leg.filled_qty > 0
                    )
                    if both_filled:
                        logger.warning(
                            "OVERRIDE: spread_order.is_complete=False but both "
                            "legs are FILLED with qty>0 — treating as SUCCESS "
                            "to avoid destructive auto-close loop"
                        )

            # Success: we already verified fill ratio above; vwap prices are ready.
            # Also accept the 1-slice path where is_complete/both-filled is the gate.
            _sliced_ok = spot_filled_qty > 0 and fut_filled_qty > 0
            if _sliced_ok or (spread_order and (
                spread_order.is_complete
                or (
                    spread_order.spot_leg.status.name == 'FILLED'
                    and spread_order.futures_leg.status.name == 'FILLED'
                    and spread_order.spot_leg.filled_qty > 0
                    and spread_order.futures_leg.filled_qty > 0
                )
            )):
                trade.spot_order_id = last_spot_order_id or (
                    spread_order.spot_leg.order_id if spread_order else "")
                trade.futures_order_id = last_fut_order_id or (
                    spread_order.futures_leg.order_id if spread_order else "")
                # Use VWAP-blended prices (for 1-slice these equal the single fill price).
                trade.entry_spot_price = vwap_spot_price
                trade.entry_futures_price = vwap_fut_price
                # Record the ACTUAL executed position (contracts × ctVal). The
                # requested beta-derived sizes get floored to whole contracts by
                # the exchange — P&L, stops, exits and capital math must act on
                # what we hold, not what we asked for.
                if spot_filled_base > 0 and fut_filled_base > 0:
                    req_spot, req_fut = target_spot_qty, target_fut_qty
                    trade.spot_qty = spot_filled_base
                    trade.quantity = fut_filled_base
                    exec_ratio = spot_filled_base / fut_filled_base
                    if (abs(spot_filled_base - req_spot) > 1e-9
                            or abs(fut_filled_base - req_fut) > 1e-9):
                        logger.info(
                            "Recorded actual fills: spot %.6f (req %.6f), fut %.6f (req %.6f) "
                            "— executed ratio %.2f vs β %.2f",
                            spot_filled_base, req_spot, fut_filled_base, req_fut,
                            exec_ratio, beta,
                        )
                    # Keep notional/margin honest for capital-at-risk math.
                    leg_a_not = abs(spot_filled_base * vwap_spot_price)
                    leg_b_not = abs(fut_filled_base * vwap_fut_price)
                    _a_deriv = is_derivative(self.config.spot_symbol)
                    _b_deriv = is_derivative(self.config.futures_symbol)
                    _a_lev = max(self.config.spot_leverage    if _a_deriv else 1, 1)
                    _b_lev = max(self.config.futures_leverage if _b_deriv else 1, 1)
                    trade.notional_usd = round(leg_a_not + leg_b_not, 2)
                    trade.margin_usd = round(leg_a_not / _a_lev + leg_b_not / _b_lev, 2)
                # Capture actual entry fees paid — OKX returns the charged fee on
                # each filled order. Fee is negative (amount deducted), so abs().
                if trade.spot_order_id and trade.futures_order_id:
                    try:
                        _fs, _ff = await asyncio.gather(
                            self.spot_adapter.get_order_status(
                                self.config.spot_symbol, trade.spot_order_id),
                            self.futures_adapter.get_order_status(
                                self.config.futures_symbol, trade.futures_order_id),
                            return_exceptions=True,
                        )
                        spot_fee = abs(float((_fs or {}).get("fee", 0) or 0)) \
                            if not isinstance(_fs, Exception) else 0.0
                        fut_fee  = abs(float((_ff or {}).get("fee", 0) or 0)) \
                            if not isinstance(_ff, Exception) else 0.0
                        trade.entry_fees_usd = spot_fee + fut_fee
                        if trade.entry_fees_usd > 0:
                            logger.info("Actual entry fees from OKX: $%.4f (spot=$%.4f fut=$%.4f)",
                                        trade.entry_fees_usd, spot_fee, fut_fee)
                    except Exception as _fe:
                        logger.debug("Entry fee capture failed (%s) — will estimate at close", _fe)
                # DO NOT override trade.quantity here. filled_qty from the adapter is in
                # OKX contract units (1 contract for BTC-USDT-SWAP), not underlying BTC.
                # trade.quantity was correctly set in underlying units at trade creation and
                # must stay that way so reconcile, P&L, and position sizing all stay correct.
                _beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
                trade.entry_spread = (
                    trade.entry_futures_price - _beta * trade.entry_spot_price
                )
                # Execution timing (spread_order may be last failed slice when slicing)
                if spread_order:
                    trade.entry_placed_at = spread_order.created_at
                fill_ts = (
                    (spread_order.spot_leg.last_update or spread_order.futures_leg.last_update)
                    if spread_order else None
                )
                if fill_ts and spread_order and spread_order.created_at:
                    trade.entry_filled_at = fill_ts
                    trade.entry_latency_ms = round(
                        (fill_ts - spread_order.created_at).total_seconds() * 1000, 1
                    )
                logger.info("ENTRY SUCCESS: mode=%s, spot_id=%s @ $%.2f, futures_id=%s @ $%.2f",
                            getattr(self.config, 'entry_execution_mode', self.config.order_execution_mode),
                            trade.spot_order_id, trade.entry_spot_price,
                            trade.futures_order_id, trade.entry_futures_price)

                # Log to CSV for post-analysis
                csv_logger = get_trade_logger()
                csv_logger.log_trade(
                    event_type="ENTRY",
                    position_type=signal.signal_type,
                    quantity=trade.quantity,
                    spot_price=trade.entry_spot_price,
                    futures_price=trade.entry_futures_price,
                    spot_order_id=trade.spot_order_id,
                    futures_order_id=trade.futures_order_id,
                    spot_status="FILLED",
                    futures_status="FILLED",
                    notes=f"mode={self.config.order_execution_mode}"
                )

                # Log periodic stats
                self._log_order_stats()
                return True
            else:
                # Track which leg failed for pattern detection
                if spread_order:
                    from core.order_executor import LegStatus
                    failed_states = (LegStatus.FAILED, LegStatus.CANCELLED)
                    spot_failed = spread_order.spot_leg.status in failed_states
                    futures_failed = spread_order.futures_leg.status in failed_states

                    if spot_failed:
                        self._spot_order_failures += 1
                        logger.error("SPOT LEG FAILED: status=%s, error=%s",
                                    spread_order.spot_leg.status.name,
                                    getattr(spread_order.spot_leg, 'error', 'unknown'))
                    if futures_failed:
                        self._futures_order_failures += 1
                        logger.error("FUTURES LEG FAILED: status=%s",
                                    spread_order.futures_leg.status.name)

                    # CRITICAL: Detect spot-only failure pattern
                    self._check_spot_failure_pattern()

                error = "Spread order failed or incomplete"
                if spread_order and spread_order.has_partial_fill:
                    error = "Spread order had partial fill - leg risk handled"
                logger.error(error)
                self.state.error = error
                return False

        except Exception as e:
            error = f"Error executing entry orders: {str(e)}"
            logger.exception(error)
            self.state.error = error
            self._spot_order_failures += 1
            self._futures_order_failures += 1
            return False

    def _check_spot_failure_pattern(self) -> None:
        """
        Detect if spot orders are failing repeatedly while futures succeed.
        This is a CRITICAL pattern that indicates a systematic issue.
        """
        if self._spot_order_attempts < 3:
            return  # Need at least 3 attempts to detect pattern

        spot_fail_rate = self._spot_order_failures / self._spot_order_attempts
        futures_fail_rate = self._futures_order_failures / self._futures_order_attempts if self._futures_order_attempts > 0 else 0

        # Pattern: Spot failing >50% while futures failing <20%
        if spot_fail_rate > 0.5 and futures_fail_rate < 0.2:
            critical_msg = (
                f"SPOT-ONLY FAILURE PATTERN DETECTED! "
                f"Spot: {self._spot_order_failures}/{self._spot_order_attempts} failed "
                f"({spot_fail_rate*100:.0f}%), Futures: {self._futures_order_failures}/"
                f"{self._futures_order_attempts} failed ({futures_fail_rate*100:.0f}%). "
                f"Check spot adapter, symbol config, or exchange permissions."
            )
            logger.critical("🚨 %s", critical_msg)
            self.state.error = f"CRITICAL: Spot orders failing {spot_fail_rate*100:.0f}% of the time"
            get_notifier().notify_error(critical_msg)

            # Auto-disable algo to prevent further leg imbalance
            self.state.algo_enabled = False
            self.config.algo_enabled = False
            # Reset counters so manual re-enable gets a fresh start
            self._spot_order_attempts = 0
            self._spot_order_failures = 0
            self._futures_order_attempts = 0
            self._futures_order_failures = 0
            logger.critical("Algo DISABLED — manual re-enable required after investigating spot failures")

            # Log to CSV for post-analysis
            csv_logger = get_trade_logger()
            csv_logger.log_spot_failure_pattern(
                self._spot_order_attempts, self._spot_order_failures,
                self._futures_order_attempts, self._futures_order_failures
            )

    def _log_order_stats(self) -> None:
        """Log order execution statistics periodically for monitoring."""
        now = datetime.utcnow()
        if self._last_order_stats_log and (now - self._last_order_stats_log).total_seconds() < self._order_stats_log_interval:
            return

        self._last_order_stats_log = now
        logger.info(
            "📊 ORDER STATS: Spot %d/%d (%.0f%% success), Futures %d/%d (%.0f%% success)",
            self._spot_order_attempts - self._spot_order_failures,
            self._spot_order_attempts,
            (1 - self._spot_order_failures / self._spot_order_attempts) * 100 if self._spot_order_attempts > 0 else 100,
            self._futures_order_attempts - self._futures_order_failures,
            self._futures_order_attempts,
            (1 - self._futures_order_failures / self._futures_order_attempts) * 100 if self._futures_order_attempts > 0 else 100
        )

        # Log to CSV for post-analysis
        csv_logger = get_trade_logger()
        csv_logger.log_order_stats(
            self._spot_order_attempts, self._spot_order_failures,
            self._futures_order_attempts, self._futures_order_failures
        )

    def _log_startup_summary(self) -> None:
        """Log comprehensive startup summary for monitoring and debugging."""
        try:
            cfg = self.config
            logger.info("=" * 60)
            logger.info("🚀 TRADING ENGINE STARTUP SUMMARY")
            logger.info("=" * 60)
            logger.info("SYMBOLS: spot=%s, futures=%s", cfg.spot_symbol, cfg.futures_symbol)
            logger.info("MODE: paper=%s, algo=%s", cfg.paper_trading, cfg.algo_enabled)
            logger.info("POSITION SIZE: $%s (max: $%s)", cfg.position_size_usd, cfg.max_position_size_usd)
            logger.info("LEVERAGE: spot=%dx, futures=%dx", cfg.spot_leverage, cfg.futures_leverage)
            logger.info("FEES (bps): spot_maker=%.1f, spot_taker=%.1f, fut_maker=%.1f, fut_taker=%.1f",
                       getattr(cfg, 'spot_maker_fee_bps', 8),
                       getattr(cfg, 'spot_taker_fee_bps', 10),
                       getattr(cfg, 'futures_maker_fee_bps', 2),
                       getattr(cfg, 'futures_taker_fee_bps', 5))
            logger.info("EXECUTION: entry=%s, exit=%s",
                       getattr(cfg, 'entry_execution_mode', 'LIMIT'),
                       getattr(cfg, 'exit_execution_mode', 'MARKET'))
            logger.info("TIMEOUTS: limit_order=%ds, orphan_recovery=%ds, entry_cooldown=%ds",
                       cfg.limit_order_timeout_sec,
                       getattr(cfg, 'orphan_recovery_timeout_sec', 60),
                       getattr(cfg, 'entry_cooldown_seconds', 60))
            logger.info("SIGNALS: z_entry=%.2f, z_exit=%.2f, stop_loss=%.2f",
                       cfg.entry_threshold,
                       cfg.exit_threshold,
                       getattr(cfg, 'stop_loss_zscore', 4.0))
            logger.info("FILTERS: hurst=%s (threshold=%.2f), std=%s (min=%.1fx)",
                       cfg.hurst_enabled, cfg.hurst_threshold,
                       cfg.std_filter_enabled, cfg.min_std_multiple)
            logger.info("=" * 60)

            # Also log to CSV for easy reference
            try:
                csv_logger = get_trade_logger()
                csv_logger.log_startup(cfg.to_dict())
            except Exception as csv_err:
                logger.warning("CSV logging failed: %s", csv_err)
        except Exception as e:
            logger.error("Error in startup summary: %s", e)

    async def _execute_exit_orders(self, trade: Trade, signal: Signal) -> bool:
        """Execute exit orders on exchanges using the order executor."""
        if not self.order_executor:
            logger.error("Order executor not configured for live trading")
            return False

        if not self.spot_tick or not self.futures_tick:
            logger.error("No tick data available for order execution")
            return False

        try:
            # Mirror the entry sizing: spot leg scaled by the hedge ratio.
            beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
            # After a POST_ONLY rejection fall back to MARKET to guarantee the close.
            # One rejection is enough — staying on limit just leaves the position open longer.
            # Urgent (stop) exits — STOP_LOSS / DOLLAR_STOP / DAILY_LOSS, i.e. any
            # reason that isn't a clean profit/normal/time exit — must close NOW:
            #   • straight to MARKET. A POST_ONLY limit can be cancelSource=31
            #     rejected, which orphans one leg and delays the close — exactly
            #     the risk we can't take on a stop. MARKET orders can't be
            #     POST_ONLY-rejected and fill immediately, so the leg-risk window
            #     collapses (and reduce_only caps any residual).
            #   • never via RFQ — the request→quote→execute cycle is seconds, far
            #     too slow when the spread is moving against us.
            # Non-stop exits keep the maker-first (LIMIT/POST_ONLY) path to save fees.
            _NON_URGENT_EXITS = ("EXIT", "PROFIT_TARGET", "MAX_HOLD")
            exit_reason_u = (trade.exit_reason or "").upper()
            is_stop_exit = exit_reason_u not in _NON_URGENT_EXITS
            is_dollar_stop = exit_reason_u == "DOLLAR_STOP"
            # DOLLAR_STOP is the frequent, fee-heavy exit — try MAKER first (up to N
            # attempts) to save the taker fee, then fall back to MARKET to guarantee the
            # close. STOP_LOSS / DAILY_LOSS stay straight-to-MARKET (genuinely urgent).
            # RFQ stays off for every stop (allow_rfq below) — even a maker DOLLAR_STOP
            # goes to the order book, never the slow request→quote→execute cycle.
            if is_dollar_stop:
                use_market = self._dollar_stop_maker_attempts >= self._DOLLAR_STOP_MAKER_ATTEMPTS
            elif is_stop_exit:
                use_market = True
            else:
                use_market = self._exit_postonly_reject_count >= self._EXIT_POSTONLY_MARKET_AFTER
            self._last_exit_was_market = use_market  # propagate to fee calc in _close_position
            if use_market:
                logger.warning(
                    "Exit using MARKET to guarantee close (reason=%s, dollar_stop_maker=%d/%d, postonly_rejects=%d/%d)",
                    exit_reason_u, self._dollar_stop_maker_attempts, self._DOLLAR_STOP_MAKER_ATTEMPTS,
                    self._exit_postonly_reject_count, self._EXIT_POSTONLY_MARKET_AFTER,
                )
            elif is_dollar_stop:
                logger.info(
                    "DOLLAR_STOP trying MAKER (attempt %d/%d) to save the taker fee before MARKET fallback",
                    self._dollar_stop_maker_attempts + 1, self._DOLLAR_STOP_MAKER_ATTEMPTS,
                )
            # Exit the ACTUAL recorded position; beta-derived fallback for
            # legacy trades opened before spot_qty was recorded.
            exit_spot_qty = trade.spot_qty if getattr(trade, 'spot_qty', 0) > 0 else trade.quantity * beta
            spread_order = await self.order_executor.execute_exit(
                position_type=trade.position_type,
                spot_tick=self.spot_tick,
                futures_tick=self.futures_tick,
                quantity=exit_spot_qty,           # spot leg quantity
                futures_quantity=trade.quantity,  # futures leg quantity
                force_market=use_market,
                allow_rfq=not is_stop_exit,
            )
            self._last_exit_postonly_rejected = spread_order.throttled if spread_order else False

            if spread_order and spread_order.is_complete:
                # Update actual exit prices from fills. Guard against 0.0 from
                # the reconcile-skip path: a 0.0 fill price would zero out
                # exit_spread in _close_position and produce a wildly wrong
                # net (e.g. trade 30 logged -$24 on a +$0.65 actual trade).
                # The mid placeholder set above is far better than 0.0.
                if spread_order.spot_leg.filled_price > 0:
                    trade.exit_spot_price = spread_order.spot_leg.filled_price
                if spread_order.futures_leg.filled_price > 0:
                    trade.exit_futures_price = spread_order.futures_leg.filled_price
                # Capture actual exit fees paid from OKX
                if spread_order.spot_leg.order_id and spread_order.futures_leg.order_id:
                    try:
                        _fs, _ff = await asyncio.gather(
                            self.spot_adapter.get_order_status(
                                self.config.spot_symbol, spread_order.spot_leg.order_id),
                            self.futures_adapter.get_order_status(
                                self.config.futures_symbol, spread_order.futures_leg.order_id),
                            return_exceptions=True,
                        )
                        spot_fee = abs(float((_fs or {}).get("fee", 0) or 0)) \
                            if not isinstance(_fs, Exception) else 0.0
                        fut_fee  = abs(float((_ff or {}).get("fee", 0) or 0)) \
                            if not isinstance(_ff, Exception) else 0.0
                        trade.exit_fees_usd = spot_fee + fut_fee
                        if trade.exit_fees_usd > 0:
                            logger.info("Actual exit fees from OKX: $%.4f (spot=$%.4f fut=$%.4f)",
                                        trade.exit_fees_usd, spot_fee, fut_fee)
                    except Exception as _fe:
                        logger.debug("Exit fee capture failed (%s) — will estimate at close", _fe)
                # Execution timing
                trade.exit_placed_at = spread_order.created_at
                fill_ts = (spread_order.spot_leg.last_update or
                           spread_order.futures_leg.last_update)
                if fill_ts and spread_order.created_at:
                    trade.exit_filled_at = fill_ts
                    trade.exit_latency_ms = round(
                        (fill_ts - spread_order.created_at).total_seconds() * 1000, 1
                    )
                logger.info("Exit orders executed: mode=%s", "MARKET" if self._last_exit_was_market else getattr(self.config, 'exit_execution_mode', self.config.order_execution_mode))
                return True
            else:
                if spread_order and spread_order.has_partial_fill:
                    logger.warning("Exit order had partial fill - leg risk handled, treating as closed")
                    return True
                logger.error("Exit spread order failed or incomplete — leaving position open for retry")
                return False

        except Exception as e:
            logger.exception("Error executing exit orders: %s", e)
            return False

    def get_status(self) -> Dict[str, Any]:
        """Get current engine status."""
        signal_state = self.signal_generator.get_state()

        # Calculate stop-loss cooldown remaining
        sl_cooldown_remaining = 0
        if self._stop_loss_cooldown_until and datetime.utcnow() < self._stop_loss_cooldown_until:
            sl_cooldown_remaining = round((self._stop_loss_cooldown_until - datetime.utcnow()).total_seconds())

        # Enrich the open trade with live NET P&L (same formula the fast-exit
        # overrides use) and held-minutes so the dashboard position panel can
        # show the take-home number and progress toward the time/dollar targets.
        open_trade_dict = None
        if self.open_trade:
            open_trade_dict = self.open_trade.to_dict()
            live_pnl = self._live_net_pnl(self.open_trade)
            if live_pnl is not None:
                open_trade_dict['unrealized_pnl'] = round(live_pnl, 2)
            if self.open_trade.entry_time:
                held_min = (datetime.utcnow() - self.open_trade.entry_time).total_seconds() / 60.0
                open_trade_dict['held_minutes'] = round(held_min, 1)
            # Resolve the active fast-exit targets so the dashboard can show what
            # the scale-invariant settings translate to in live dollars/periods.
            try:
                tg = self._effective_exit_targets(self.open_trade)
                open_trade_dict['exit_target_usd'] = round(tg['target_usd'], 2) if tg['target_usd'] > 0 else 0.0
                open_trade_dict['exit_stop_usd'] = round(tg['stop_usd'], 2) if tg['stop_usd'] > 0 else 0.0
                if tg.get('round_trip_cost', 0) > 0:
                    open_trade_dict['round_trip_cost'] = round(tg['round_trip_cost'], 2)
                if tg['max_hold_periods'] > 0 and self._entry_tick_count is not None:
                    periods_held = self.signal_generator.total_ticks - self._entry_tick_count
                    open_trade_dict['max_hold_periods'] = round(tg['max_hold_periods'])
                    open_trade_dict['periods_held'] = periods_held
                    # Resolved max hold in minutes for dashboard display (polling = 0.5s/tick)
                    open_trade_dict['max_hold_minutes'] = round(tg['max_hold_periods'] * 0.5 / 60, 1)
                elif tg.get('max_hold_minutes', 0) > 0:
                    open_trade_dict['max_hold_minutes'] = round(tg['max_hold_minutes'], 1)
            except Exception:
                pass
            # Absolute spread levels (BE/TP/SL) — the drift-free way to watch an
            # open trade: unlike the in-trade z-score, these don't move with the
            # rolling mean. Display only.
            levels = self._exit_spread_levels(self.open_trade)
            if levels:
                open_trade_dict['spread_levels'] = {
                    k: (round(v, 2) if isinstance(v, float) else v)
                    for k, v in levels.items()
                }
            try:
                gate_floor = self._exit_gate_floor(self.open_trade)
                if gate_floor is not None:
                    open_trade_dict['exit_gate_floor_usd'] = round(gate_floor, 2)
            except Exception:
                pass

        # Tick age: how stale is the most recent price update
        tick_age_ms = None
        if self.state.last_tick_time:
            tick_age_ms = round((datetime.utcnow() - self.state.last_tick_time).total_seconds() * 1000)

        # WS connection state (True/False/None = unknown)
        ws_connected = getattr(self.futures_adapter, "_connected", None)
        if ws_connected is None:
            ws_connected = getattr(self.spot_adapter, "_connected", None)

        return {
            'is_running': self.state.is_running,
            'algo_enabled': self.state.algo_enabled,
            'paper_trading': self.state.paper_trading,
            'asset': self.config.asset,
            'position': self.state.current_position,
            'last_tick_time': self.state.last_tick_time.isoformat() if self.state.last_tick_time else None,
            'tick_age_ms': tick_age_ms,
            'ws_connected': ws_connected,
            'error': self.state.error,
            'spot_connected': self.spot_adapter is not None,
            'futures_connected': self.futures_adapter is not None,
            'signal': signal_state,
            'spot_tick': self.spot_tick.to_dict() if self.spot_tick else None,
            'futures_tick': self.futures_tick.to_dict() if self.futures_tick else None,
            'open_trade': open_trade_dict,
            'sl_cooldown_remaining': sl_cooldown_remaining,
            'sl_cooldown_sec': self._stop_loss_cooldown_sec,
            'z_reset_block_direction': self._z_reset_block_direction,
            'executing_trade': self._executing_trade,
            'position_mismatch': self._position_mismatch,
            'entry_execution_mode': getattr(self.config, 'entry_execution_mode', 'LIMIT'),
            'exit_execution_mode': getattr(self.config, 'exit_execution_mode', 'LIMIT'),
            'daily_loss_usd': round(self._daily_loss_usd, 2),
            'daily_loss_limit': self.config.daily_max_loss_usd,
        }

    def get_spread_history(self, n: int = 100) -> List[float]:
        """Get spread history for charting."""
        return self.signal_generator.get_spread_history(n)

    def get_zscore_history(self, n: int = 100) -> List[float]:
        """Get Z-score history for charting."""
        return self.signal_generator.get_zscore_history(n)

    def reset(self) -> None:
        """Reset engine state (preserves running status)."""
        # Preserve running state
        was_running = self.state.is_running
        algo_was_enabled = self.state.algo_enabled

        self.signal_generator.reset()
        self.state = EngineState(paper_trading=self.config.paper_trading)

        # Restore running state
        self.state.is_running = was_running
        self.state.algo_enabled = algo_was_enabled

        self.open_trade = None
        self._entry_tick_count = None
        self.spot_tick = None
        self.futures_tick = None
        self._stop_loss_cooldown_until = None
        self._z_reset_block_direction = None
        self._executing_trade = False
        self._last_entry_throttled = False
        self._last_exit_postonly_rejected = False
        self._exit_postonly_reject_count = 0
        self._dollar_stop_maker_attempts = 0
        self._last_exit_attempt = None
        self._tick_fail_count = 0  # Reset tick failure counter too
        logger.info("Engine reset (running=%s, algo=%s)", was_running, algo_was_enabled)
