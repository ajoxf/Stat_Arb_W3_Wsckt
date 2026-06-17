"""
Trading engine for crypto statistical arbitrage.
Manages the main trading loop, position management, and order execution.
"""

import asyncio
import logging
import math
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
# OKX technically allows up to 125x on BTC, but statistical arbitrage has
# correlated legs that reduce net risk — 25x on the futures leg is already generous.
MAX_SAFE_FUTURES_LEVERAGE = 25


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
        self.on_status: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

        # Control flags
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Post-stop-loss cooldown: prevent re-entry for this many seconds after a stop-loss
        self._stop_loss_cooldown_sec = 60
        self._stop_loss_cooldown_until: Optional[datetime] = None

        # General entry cooldown: prevent rapid re-entry after any trade
        self._entry_cooldown_until: Optional[datetime] = None

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

        # Exit retry throttle — don't hammer the exchange on consecutive failures
        self._last_exit_attempt: Optional[datetime] = None
        self._exit_retry_interval_sec = 10

        # Optional callback invoked when the engine self-corrects config values
        # (e.g. leverage capped by exchange). Register in app.py to persist to DB.
        self.on_config_corrected = None

        # Tick processing lock: prevents concurrent _process_tick_pair tasks
        # Critical for WebSocket mode where ticks arrive faster than processing
        self._processing_tick = False

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

    def set_adapters(self, spot: Optional[ExchangeAdapter], futures: Optional[ExchangeAdapter]) -> None:
        """Set exchange adapters (REST mode)."""
        self.spot_adapter = spot
        self.futures_adapter = futures
        self._use_websocket = False

        # Initialize order executor if we have both adapters
        if spot and futures:
            self.order_executor = OrderExecutor(self.config, spot, futures)
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
        if self.spot_tick and self.futures_tick and not self._processing_tick:
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

        # Spin up the periodic AI health monitor.
        try:
            self.ai_monitor.start()
        except Exception:
            logger.exception("AI monitor failed to start (continuing)")

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

        # Notify signal callback
        if self.on_signal:
            self.on_signal(signal)

        # Execute trading logic if algo enabled
        if self.state.algo_enabled and signal.signal_type != "NONE":
            await self._process_signal(signal)
        elif not self.state.algo_enabled and signal.signal_type != "NONE":
            # Signal fired but algo is off — show once in the blocked panel
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

        # Create trade record
        _leverage = max(getattr(self.config, 'futures_leverage', 1), 1)
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
            notional_usd=self.config.position_size_usd,
            margin_usd=round(self.config.position_size_usd / _leverage, 2),
            is_open=True,
            is_paper=self.state.paper_trading,
        )

        # Execute orders if not paper trading
        if not self.state.paper_trading:
            self._executing_trade = True
            try:
                success = await self._execute_entry_orders(trade, signal)
                if not success:
                    # Apply cooldown after any failed order to prevent rapid retry
                    # This is critical: without this, the engine retries on every tick
                    cooldown_sec = max(30, getattr(self.config, 'entry_cooldown_seconds', 60))
                    self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=cooldown_sec)
                    logger.warning("Entry orders failed - applying %ds cooldown to prevent rapid retry",
                                   cooldown_sec)
                    return
            finally:
                self._executing_trade = False

        self.open_trade = trade
        self.state.current_position = position_type
        self.signal_generator.set_position(
            position_type,
            entry_mean=signal.spread_mean,
            entry_std=signal.spread_std,
        )

        logger.info("Opened %s position: futures_qty=%.6f, spot_qty=%.6f (beta=%.4f), "
                    "spot=%.2f, futures=%.2f, spread=%.6f, zscore=%.4f",
                    position_type, quantity, spot_qty, beta,
                    spot_price, futures_price, signal.spread, signal.zscore)

        get_notifier().notify_trade_entry(trade, signal)

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

        # Throttle exit retries — after a failure don't hammer exchange every tick
        if not self.state.paper_trading and self._last_exit_attempt:
            elapsed = (datetime.utcnow() - self._last_exit_attempt).total_seconds()
            if elapsed < self._exit_retry_interval_sec:
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
                logger.error(
                    "Exit orders FAILED for %s position — leaving position open for retry",
                    trade.position_type,
                )
                return  # Do NOT reset state; engine retries after _exit_retry_interval_sec

        self._last_exit_attempt = None  # Clear throttle on success
        trade.is_open = False

        # ── Realized P&L from ACTUAL fills (now that the executor has stamped
        # them onto trade.exit_spot_price / exit_futures_price). For paper or
        # when fills weren't recorded, falls back to the mid placeholders we
        # set above. Fees are subtracted to give the net the dashboard logs.
        beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        # Spread from fills (futures - β × spot)
        entry_spread_fills = trade.entry_futures_price - beta * trade.entry_spot_price
        exit_spread_fills  = trade.exit_futures_price  - beta * trade.exit_spot_price

        if trade.position_type == "LONG":
            # LONG profits when spread falls
            spread_change = entry_spread_fills - exit_spread_fills
        else:
            spread_change = exit_spread_fills - entry_spread_fills
        pnl_gross = spread_change * trade.quantity

        # Per-leg fee bps from the same schedule the signal filter uses, so
        # cost estimates and realized P&L can never silently diverge.
        spot_maker = getattr(self.config, 'spot_maker_fee_bps', self.config.maker_fee_bps)
        spot_taker = getattr(self.config, 'spot_taker_fee_bps', self.config.taker_fee_bps)
        fut_maker  = getattr(self.config, 'futures_maker_fee_bps', self.config.maker_fee_bps)
        fut_taker  = getattr(self.config, 'futures_taker_fee_bps', self.config.taker_fee_bps)
        entry_mode = getattr(self.config, 'entry_execution_mode', self.config.order_execution_mode)
        exit_mode  = getattr(self.config, 'exit_execution_mode',  self.config.order_execution_mode)
        leg_a_deriv = is_derivative(self.config.spot_symbol)
        leg_b_deriv = is_derivative(self.config.futures_symbol)
        def _bps(deriv, mode):
            if mode == "LIMIT":
                return fut_maker if deriv else spot_maker
            return fut_taker if deriv else spot_taker
        a_entry, b_entry = _bps(leg_a_deriv, entry_mode), _bps(leg_b_deriv, entry_mode)
        a_exit,  b_exit  = _bps(leg_a_deriv, exit_mode),  _bps(leg_b_deriv, exit_mode)
        spot_qty = trade.quantity * beta
        fees_usd = (
            a_entry / 10000.0 * spot_qty       * trade.entry_spot_price +
            b_entry / 10000.0 * trade.quantity * trade.entry_futures_price +
            a_exit  / 10000.0 * spot_qty       * trade.exit_spot_price +
            b_exit  / 10000.0 * trade.quantity * trade.exit_futures_price
        )

        pnl = pnl_gross - fees_usd
        pnl_percent = (pnl / trade.notional_usd) * 100 if trade.notional_usd > 0 else 0

        trade.pnl_usd = pnl
        trade.pnl_percent = pnl_percent
        trade.pnl_gross_usd = pnl_gross
        trade.fees_usd = fees_usd
        # Audit trail: store fill-derived spreads so the DB matches OKX
        trade.entry_spread = entry_spread_fills
        trade.exit_spread  = exit_spread_fills

        # Daily-loss tracker tracks NET realized P&L
        self._daily_loss_usd += pnl

        logger.info(
            "Closed %s position: net=$%.2f (%.2f%%) = gross $%.2f − fees $%.2f, "
            "reason=%s, zscore=%.4f",
            trade.position_type, pnl, pnl_percent, pnl_gross, fees_usd,
            signal.signal_type, signal.zscore,
        )

        get_notifier().notify_trade_exit(trade)

        # Reset state
        self.state.current_position = "NONE"
        self.signal_generator.set_position("NONE")
        self.open_trade = None

        # Apply post-stop-loss cooldown to prevent immediate re-entry
        if signal.signal_type == "STOP_LOSS":
            from datetime import timedelta
            self._stop_loss_cooldown_until = datetime.utcnow() + timedelta(seconds=self._stop_loss_cooldown_sec)
            logger.info("Stop-loss cooldown active for %ds", self._stop_loss_cooldown_sec)

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
        positions. Uses a market order with explicit posSide for hedge-mode accounts.
        """
        if not self.futures_adapter:
            logger.error("Cannot auto-close orphans: no futures adapter")
            return

        for pos in orphan_positions:
            symbol = pos['symbol']
            side = pos['side']    # "LONG" or "SHORT"
            qty = pos['quantity'] # contracts (as reported by exchange)

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

            close_side = "sell" if side == "LONG" else "buy"
            pos_side = "long" if side == "LONG" else "short"

            # `qty` from get_positions() is in CONTRACTS for SWAP. `place_order`
            # expects BTC and divides by ctVal again, so passing the contract
            # count directly inflates the order size by 1/ctVal (e.g. 32
            # contracts → adapter sees "32 BTC" → sends sz=3200). That's why
            # every AUTO-CLOSE in the log fails with 51169 — OKX has only 32
            # contracts on the LONG side, not 3200. Convert here.
            info = await self.futures_adapter.get_symbol_info(symbol)
            ct_val = float(info.get("contract_val") or 0) if info else 0.0
            if ct_val <= 0:
                logger.error(
                    "AUTO-CLOSE skipped for %s: could not fetch contract_val "
                    "(needed to convert %s contracts → BTC for place_order)",
                    symbol, qty,
                )
                continue
            qty_btc = qty * ct_val

            logger.warning(
                "AUTO-CLOSE orphan %s %s: %d contracts (%.6f BTC), PnL=%.2f",
                side, symbol, int(round(qty)), qty_btc, pos['unrealized_pnl'],
            )

            if qty_btc <= 0:
                logger.warning(
                    "Auto-close skipped: qty_btc=0 for %s (raw contracts=%.4f, ctVal=%.4f)",
                    symbol, qty, ct_val,
                )
                continue

            result = await self.futures_adapter.place_order(
                symbol=symbol,
                side=close_side.upper(),
                order_type="MARKET",
                quantity=qty_btc,
                pos_side=pos_side,
                reduce_only=True,
            )

            if result.success:
                logger.warning(
                    "AUTO-CLOSE SUCCESS: closed orphan %s %s, order_id=%s",
                    side, symbol, result.order_id,
                )
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
        """Execute entry orders on exchanges using the order executor."""
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
            # Spot leg is scaled by the hedge ratio; futures leg = trade.quantity.
            beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
            spread_order = await self.order_executor.execute_entry(
                position_type=signal.signal_type,
                spot_tick=self.spot_tick,
                futures_tick=self.futures_tick,
                quantity=trade.quantity * beta,   # spot leg quantity
                futures_quantity=trade.quantity,  # futures leg quantity
            )

            if spread_order and spread_order.is_complete:
                trade.spot_order_id = spread_order.spot_leg.order_id
                trade.futures_order_id = spread_order.futures_leg.order_id
                # Update actual fill prices + re-stamp entry_spread from FILLS
                # (it was provisionally set from the mid-derived signal.spread
                # at trade creation; now we have the real fills and β to
                # recompute it). The dashboard's open-position Entry Spread
                # display reads this field, so it'll be accurate during the
                # life of the trade and not just at close.
                trade.entry_spot_price = spread_order.spot_leg.filled_price
                trade.entry_futures_price = spread_order.futures_leg.filled_price
                _beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
                trade.entry_spread = (
                    trade.entry_futures_price - _beta * trade.entry_spot_price
                )
                # Execution timing
                trade.entry_placed_at = spread_order.created_at
                fill_ts = (spread_order.spot_leg.last_update or
                           spread_order.futures_leg.last_update)
                if fill_ts and spread_order.created_at:
                    trade.entry_filled_at = fill_ts
                    trade.entry_latency_ms = round(
                        (fill_ts - spread_order.created_at).total_seconds() * 1000, 1
                    )
                logger.info("ENTRY SUCCESS: mode=%s, spot_id=%s @ $%.2f, futures_id=%s @ $%.2f",
                            self.config.order_execution_mode,
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
            spread_order = await self.order_executor.execute_exit(
                position_type=trade.position_type,
                spot_tick=self.spot_tick,
                futures_tick=self.futures_tick,
                quantity=trade.quantity * beta,   # spot leg quantity
                futures_quantity=trade.quantity,  # futures leg quantity
            )

            if spread_order and spread_order.is_complete:
                # Update actual exit prices from fills
                trade.exit_spot_price = spread_order.spot_leg.filled_price
                trade.exit_futures_price = spread_order.futures_leg.filled_price
                # Execution timing
                trade.exit_placed_at = spread_order.created_at
                fill_ts = (spread_order.spot_leg.last_update or
                           spread_order.futures_leg.last_update)
                if fill_ts and spread_order.created_at:
                    trade.exit_filled_at = fill_ts
                    trade.exit_latency_ms = round(
                        (fill_ts - spread_order.created_at).total_seconds() * 1000, 1
                    )
                logger.info("Exit orders executed: mode=%s", self.config.order_execution_mode)
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

        return {
            'is_running': self.state.is_running,
            'algo_enabled': self.state.algo_enabled,
            'paper_trading': self.state.paper_trading,
            'asset': self.config.asset,
            'position': self.state.current_position,
            'last_tick_time': self.state.last_tick_time.isoformat() if self.state.last_tick_time else None,
            'error': self.state.error,
            'spot_connected': self.spot_adapter is not None,
            'futures_connected': self.futures_adapter is not None,
            'signal': signal_state,
            'spot_tick': self.spot_tick.to_dict() if self.spot_tick else None,
            'futures_tick': self.futures_tick.to_dict() if self.futures_tick else None,
            'open_trade': self.open_trade.to_dict() if self.open_trade else None,
            'sl_cooldown_remaining': sl_cooldown_remaining,
            'sl_cooldown_sec': self._stop_loss_cooldown_sec,
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
        self.spot_tick = None
        self.futures_tick = None
        self._stop_loss_cooldown_until = None
        self._executing_trade = False
        self._tick_fail_count = 0  # Reset tick failure counter too
        logger.info("Engine reset (running=%s, algo=%s)", was_running, algo_was_enabled)
