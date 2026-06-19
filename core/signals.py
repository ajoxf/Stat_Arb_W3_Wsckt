"""
Signal generation module for crypto statistical arbitrage.
Implements Z-score calculation, Hurst exponent, and STD filter.
"""

import math
import numpy as np
from collections import deque
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any, Callable
import logging

from models import Signal, TradingConfig, MarketTick, SDTouchEvent
from adapters.base import is_derivative

logger = logging.getLogger(__name__)


class SignalGenerator:
    """
    Generates trading signals based on spread Z-score with filters.

    The spread is calculated as: Futures Price - hedge_ratio * Spot Price
    (hedge_ratio defaults to 1.0 for same-underlying basis trades)
    Z-score = (spread - rolling_mean) / rolling_std

    Entry signals:
    - LONG: z >= +entry_threshold (spread above mean, futures premium high, expect reversion down)
    - SHORT: z <= -entry_threshold (spread below mean, futures discount, expect reversion up)

    Exit signals (direction-aware):
    - LONG position: exit when z <= +exit_threshold (spread reverted toward mean)
    - SHORT position: exit when z >= -exit_threshold (spread reverted toward mean)

    Filters (applied to ENTRY only):
    - Hurst exponent: H < 0.5 indicates mean-reverting regime
    - STD filter: Ensures volatility is sufficient to cover trading costs
    """

    def __init__(self, config: TradingConfig):
        self.config = config
        self.lookback = config.lookback_period
        self.stats_update_interval = config.stats_update_interval  # Seconds

        # Rolling data storage
        self.spread_history: deque = deque(maxlen=self.lookback)
        self.spot_prices: deque = deque(maxlen=self.lookback)
        self.futures_prices: deque = deque(maxlen=self.lookback)

        # Current state
        self.current_zscore: float = 0.0
        self.current_spread: float = 0.0
        self.current_mean: float = 0.0
        self.current_std: float = 0.0
        self.current_hurst: float = 0.5
        self.current_half_life: float = float('inf')  # periods; inf = not mean-reverting

        # Stats update timing
        self.last_stats_update: Optional[datetime] = None
        self._stats_initialized: bool = False

        # SD touch tracking
        self.last_sd_level: float = 0.0
        self.sd_touch_events: List[SDTouchEvent] = []

        # Callback for SD touch events (for database logging)
        self.on_sd_touch: Optional[Callable[[SDTouchEvent], None]] = None

        # Track current position for exit signals
        self.current_position: str = "NONE"
        # Mean and std locked in at entry — used as exit target
        self.entry_mean: Optional[float] = None
        self.entry_std: Optional[float] = None

        # Track blocked signals for diagnostics
        self.last_blocked_signal: Optional[Dict[str, Any]] = None

    def update_config(self, config: TradingConfig) -> None:
        """Update configuration.

        Three cases that invalidate the rolling spread history are handled here:

        - **Leg A or Leg B changed**: the cached ticks are from a different
          instrument entirely, so we clear everything and start collecting fresh.
        - **Hedge ratio changed**: the spot/futures price ticks are still good,
          but every cached spread was computed with the wrong beta. We rebuild
          ``spread_history`` from ``spot_prices`` / ``futures_prices`` using the
          new beta — preserves all the collected data and avoids a multi-hour
          collect-from-scratch.
        - **Lookback changed**: existing behavior — resize the deques in place.
        """
        old_config = self.config
        old_spot_sym = getattr(old_config, 'spot_symbol', None)
        old_fut_sym  = getattr(old_config, 'futures_symbol', None)
        old_beta     = getattr(old_config, 'hedge_ratio', 1.0) or 1.0

        self.config = config
        self.stats_update_interval = config.stats_update_interval

        # Clear stale blocked-signal records whose filter has just been disabled
        # so the dashboard doesn't show a ghost block for a filter that's now off.
        if self.last_blocked_signal:
            reason = self.last_blocked_signal.get('reason') or ''
            if 'Trend filter' in reason and not config.trend_direction_filter:
                self.last_blocked_signal = None

        # 1. Pair changed — drop everything; the ticks aren't comparable
        pair_changed = (
            old_spot_sym != config.spot_symbol or
            old_fut_sym  != config.futures_symbol
        )
        if pair_changed:
            self.spread_history.clear()
            self.spot_prices.clear()
            self.futures_prices.clear()
            self.current_spread = 0.0
            self.current_mean = 0.0
            self.current_std = 0.0
            self.current_zscore = 0.0
            self.current_hurst = 0.5
            self.current_half_life = float('inf')
            self.last_stats_update = None
            self._stats_initialized = False
            logger.info("Pair changed (%s/%s -> %s/%s) — spread history cleared",
                        old_spot_sym, old_fut_sym,
                        config.spot_symbol, config.futures_symbol)

        # 2. Beta changed — recompute every cached spread under the new beta
        new_beta = getattr(config, 'hedge_ratio', 1.0) or 1.0
        if not pair_changed and abs(new_beta - old_beta) > 1e-12 and self.spot_prices:
            recomputed = [F - new_beta * S
                          for S, F in zip(self.spot_prices, self.futures_prices)]
            self.spread_history = deque(recomputed, maxlen=self.lookback)
            self.current_spread = recomputed[-1] if recomputed else 0.0
            # Stats need to be redrawn from the new series on the next tick
            self.last_stats_update = None
            self._stats_initialized = False
            logger.info("Hedge ratio changed (%.6f -> %.6f) — recomputed %d cached spreads",
                        old_beta, new_beta, len(recomputed))

        # 3. Lookback changed — resize in place (existing behavior, preserved)
        if config.lookback_period != self.lookback:
            self.lookback = config.lookback_period
            old_spreads = list(self.spread_history)
            old_spots = list(self.spot_prices)
            old_futures = list(self.futures_prices)
            self.spread_history = deque(old_spreads[-self.lookback:], maxlen=self.lookback)
            self.spot_prices = deque(old_spots[-self.lookback:], maxlen=self.lookback)
            self.futures_prices = deque(old_futures[-self.lookback:], maxlen=self.lookback)

    def set_position(self, position: str,
                     entry_mean: Optional[float] = None,
                     entry_std: Optional[float] = None) -> None:
        """Set current position for exit signal calculation.

        Pass entry_mean/entry_std when opening a position so the exit target
        is the mean *at entry time*, not the current rolling mean.
        """
        self.current_position = position
        if position == "NONE":
            self.entry_mean = None
            self.entry_std = None
        elif entry_mean is not None:
            self.entry_mean = entry_mean
            self.entry_std = entry_std

    def add_tick(self, spot_tick: MarketTick, futures_tick: MarketTick) -> None:
        """Add a new tick and update calculations."""
        spot_price = spot_tick.mid
        futures_price = futures_tick.mid

        if spot_price <= 0 or futures_price <= 0:
            logger.warning(
                "Skipping tick — invalid prices (spot=%.4f, futures=%.4f). "
                "Check WebSocket feed or REST fallback.",
                spot_price, futures_price,
            )
            return

        # Hedge ratio (beta) makes the two legs comparable for cross-instrument
        # pairs. Defaults to 1.0 -> spread = futures - spot (classic basis trade).
        hedge_ratio = getattr(self.config, 'hedge_ratio', 1.0) or 1.0
        spread = futures_price - hedge_ratio * spot_price

        self.spot_prices.append(spot_price)
        self.futures_prices.append(futures_price)
        self.spread_history.append(spread)

        self.current_spread = spread
        self._update_statistics()

    def _update_statistics(self) -> None:
        """
        Update rolling statistics.

        Mean and STD are only recalculated at the configured interval
        (stats_update_interval seconds). Z-score is always calculated
        using the current spread and the (potentially stale) mean/std.

        This provides stable bands for easier entry/exit tracking.
        """
        if len(self.spread_history) < 2:
            return

        now = datetime.utcnow()

        # Check if we need to recalculate mean/std
        should_update_stats = (
            not self._stats_initialized or
            self.last_stats_update is None or
            (now - self.last_stats_update).total_seconds() >= self.stats_update_interval
        )

        if should_update_stats:
            spreads = np.array(self.spread_history)
            self.current_mean = float(np.mean(spreads))
            self.current_std = float(np.std(spreads, ddof=1))

            # Update Hurst and half-life if we have enough data
            if len(self.spread_history) >= 20:
                self.current_hurst = self._calculate_hurst(spreads)
                self.current_half_life = self._calculate_half_life(spreads)

            self.last_stats_update = now
            self._stats_initialized = True

            logger.debug("Stats updated: mean=%.6f, std=%.6f, hurst=%.4f, half_life=%.1f",
                        self.current_mean, self.current_std, self.current_hurst,
                        self.current_half_life if self.current_half_life != float('inf') else -1)

        # Always update z-score with current spread
        if self.current_std > 0:
            self.current_zscore = (self.current_spread - self.current_mean) / self.current_std
        else:
            self.current_zscore = 0.0

    def _spread_slope(self) -> float:
        """Linear slope of the spread over the last 20% of the lookback window
        (~1440 ticks at the default 7200-tick lookback ≈ 24 minutes at 1 tick/s).

        Positive slope → spread trending up (Leg B outperforming Leg A) → SHORT-favourable.
        Negative slope → spread trending down (Leg A outperforming Leg B) → LONG-favourable.
        Returns 0.0 when there is insufficient data.
        """
        n = max(20, self.lookback // 5)
        if len(self.spread_history) < n:
            return 0.0
        recent = np.array(list(self.spread_history)[-n:])
        x = np.arange(n, dtype=float)
        slope, _ = np.polyfit(x, recent, 1)
        return float(slope)

    def _calculate_hurst(self, series: np.ndarray) -> float:
        """
        Calculate Hurst exponent using R/S (Rescaled Range) analysis.

        H < 0.5: Mean-reverting (anti-persistent)
        H = 0.5: Random walk
        H > 0.5: Trending (persistent)
        """
        n = len(series)
        if n < 20:
            return 0.5

        # Use different sub-series lengths
        max_k = min(n // 2, 50)
        min_k = 10

        if max_k <= min_k:
            return 0.5

        rs_values = []
        n_values = []

        for k in range(min_k, max_k + 1, 5):
            rs_list = []

            for start in range(0, n - k + 1, k):
                subseries = series[start:start + k]
                if len(subseries) < k:
                    continue

                mean_val = np.mean(subseries)
                deviations = subseries - mean_val
                cumulative_deviations = np.cumsum(deviations)

                r = np.max(cumulative_deviations) - np.min(cumulative_deviations)
                s = np.std(subseries, ddof=1)

                if s > 0:
                    rs_list.append(r / s)

            if rs_list:
                rs_values.append(np.mean(rs_list))
                n_values.append(k)

        if len(rs_values) < 2:
            return 0.5

        # Linear regression in log-log space
        log_n = np.log(n_values)
        log_rs = np.log(rs_values)

        try:
            # H = slope of log(R/S) vs log(n)
            slope, _ = np.polyfit(log_n, log_rs, 1)
            hurst = float(np.clip(slope, 0.0, 1.0))
            return hurst
        except Exception:
            return 0.5

    def _calculate_half_life(self, series: np.ndarray) -> float:
        """
        Calculate the half-life of mean reversion via OLS on the OU process.

        Models: spread[t+1] - spread[t] = theta * (mean - spread[t]) + noise
        If theta > 0 the process is mean-reverting with half-life = ln(2) / theta.

        A half-life of N periods means the spread reverts halfway to the mean
        in N ticks.  Use 2-5x the half-life as the lookback window so the window
        is long enough to capture a full reversion cycle without being so long
        that it smooths out tradeable dislocations.

        Returns half-life in periods (same units as lookback_period).
        Returns float('inf') when the series is not mean-reverting (theta <= 0).
        """
        n = len(series)
        if n < 10:
            return float('inf')

        spread_lag = series[:-1]                    # spread[t]
        spread_diff = series[1:] - series[:-1]      # spread[t+1] - spread[t]
        x = np.mean(spread_lag) - spread_lag        # (mean - spread[t])
        y = spread_diff

        try:
            # OLS without intercept (matches QuantInsti OU formulation):
            # theta = sum(x * y) / sum(x^2)
            denom = np.dot(x, x)
            if denom == 0:
                return float('inf')
            theta = np.dot(x, y) / denom

            if theta <= 0:
                return float('inf')  # trending or random — no mean reversion

            hl = math.log(2) / theta
            # Clamp: at least 1 period, at most the full window length
            hl = max(1.0, min(hl, float(n)))
            return round(hl, 1)
        except Exception:
            return float('inf')

    def _compute_round_trip_cost(self) -> Dict[str, float]:
        """
        Compute the full round-trip cost breakdown in bps.

        Round-trip = entry (Leg A + Leg B) + exit (Leg A + Leg B) + slippage × 4.
        Fees are picked per-leg from the instrument shape (spot rates for spot,
        futures rates for any perp/dated future) so the cost estimate is correct
        regardless of which leg-slot holds which instrument type — futures/futures,
        calendar spreads, and the classic basis trade are all handled.

        The breakdown keys keep their legacy names (entry_spot_bps / entry_fut_bps)
        for dashboard backward-compatibility; semantically they're Leg A / Leg B.
        """
        spot_maker = getattr(self.config, 'spot_maker_fee_bps', self.config.maker_fee_bps)
        spot_taker = getattr(self.config, 'spot_taker_fee_bps', self.config.taker_fee_bps)
        fut_maker  = getattr(self.config, 'futures_maker_fee_bps', self.config.maker_fee_bps)
        fut_taker  = getattr(self.config, 'futures_taker_fee_bps', self.config.taker_fee_bps)

        entry_mode = getattr(self.config, 'entry_execution_mode', self.config.order_execution_mode)
        exit_mode  = getattr(self.config, 'exit_execution_mode',  self.config.order_execution_mode)

        # Pick the per-leg fee schedule based on what each leg actually is
        leg_a_is_deriv = is_derivative(getattr(self.config, 'spot_symbol', ''))
        leg_b_is_deriv = is_derivative(getattr(self.config, 'futures_symbol', ''))
        leg_a_maker = fut_maker if leg_a_is_deriv else spot_maker
        leg_a_taker = fut_taker if leg_a_is_deriv else spot_taker
        leg_b_maker = fut_maker if leg_b_is_deriv else spot_maker
        leg_b_taker = fut_taker if leg_b_is_deriv else spot_taker

        entry_spot_bps = leg_a_maker if entry_mode == "LIMIT" else leg_a_taker
        entry_fut_bps  = leg_b_maker if entry_mode == "LIMIT" else leg_b_taker
        exit_spot_bps  = leg_a_maker if exit_mode  == "LIMIT" else leg_a_taker
        exit_fut_bps   = leg_b_maker if exit_mode  == "LIMIT" else leg_b_taker

        entry_cost_bps = entry_spot_bps + entry_fut_bps
        exit_cost_bps  = exit_spot_bps  + exit_fut_bps
        fees_bps       = entry_cost_bps + exit_cost_bps

        slippage_per_leg = getattr(self.config, 'slippage_bps', 0.0)
        slippage_bps = slippage_per_leg * 4  # 4 legs: spot+futures × entry+exit

        round_trip_bps = fees_bps + slippage_bps

        return {
            'entry_spot_bps':   entry_spot_bps,
            'entry_fut_bps':    entry_fut_bps,
            'exit_spot_bps':    exit_spot_bps,
            'exit_fut_bps':     exit_fut_bps,
            'entry_cost_bps':   entry_cost_bps,
            'exit_cost_bps':    exit_cost_bps,
            'fees_bps':         fees_bps,
            'slippage_bps':     slippage_bps,
            'round_trip_bps':   round_trip_bps,
            'entry_mode':       entry_mode,
            'exit_mode':        exit_mode,
        }

    def _check_std_filter(self) -> Tuple[bool, float]:
        """
        Check if spread STD is sufficient to cover trading costs.

        Uses separate spot and futures fees since they differ significantly:
          Spot (non-VIP):    Maker 8 bps, Taker 10 bps
          Futures (non-VIP): Maker 2 bps, Taker  5 bps

        Round-trip cost = entry (spot + futures) + exit (spot + futures) + slippage × 4.

        The comparison must be in *spread units*, not dollar-per-leg units.
        Spread = futures - beta*spot, so a $1 spread move translates to
        ``futures_qty`` dollars of PnL, where ``futures_qty = position_size /
        (beta * spot_price)`` (engine's sizing rule). Breakeven spread move is
        therefore ``rt_bps/10000 * beta * spot_price`` — note the beta factor.
        For the classic basis trade (beta = 1) this reduces to the original
        ``rt_bps/10000 * spot_price``; for cross pairs (beta != 1) it correctly
        scales cost up to the futures-price magnitude.

        Returns (passed, profitability_ratio)
        """
        if not self.config.std_filter_enabled:
            return True, float('inf')

        if self.current_std <= 0:
            return False, 0.0

        spot_price = self.spot_prices[-1] if self.spot_prices else 0
        if spot_price <= 0:
            return False, 0.0

        total_cost_bps = self._compute_round_trip_cost()['round_trip_bps']
        beta = max(getattr(self.config, 'hedge_ratio', 1.0) or 1.0, 1e-9)
        costs_price = (total_cost_bps / 10000) * beta * spot_price

        # Profitability ratio: how many times STD covers the costs
        profitability_ratio = self.current_std / costs_price if costs_price > 0 else float('inf')

        passed = profitability_ratio >= self.config.min_std_multiple
        return passed, profitability_ratio

    def _track_sd_touch(self, zscore: float, spot_price: float, futures_price: float) -> Optional[SDTouchEvent]:
        """Track when Z-score crosses SD levels."""
        current_sd_level = 0.0

        # Determine current SD level
        for level in [-3, -2, -1, 1, 2, 3]:
            if level < 0:
                if zscore <= level and zscore > level - 1:
                    current_sd_level = level
                    break
            else:
                if zscore >= level and zscore < level + 1:
                    current_sd_level = level
                    break

        # Check for SD level crossing
        if current_sd_level != 0 and current_sd_level != self.last_sd_level:
            direction = "DOWN" if current_sd_level < self.last_sd_level else "UP"

            event = SDTouchEvent(
                asset=self.config.asset,
                timestamp=datetime.utcnow(),
                sd_level=current_sd_level,
                direction=direction,
                spread=self.current_spread,
                zscore=zscore,
                spot_price=spot_price,
                futures_price=futures_price,
            )

            self.sd_touch_events.append(event)
            self.last_sd_level = current_sd_level

            # Call callback for database logging
            if self.on_sd_touch:
                self.on_sd_touch(event)

            return event

        self.last_sd_level = current_sd_level
        return None

    def generate_signal(self) -> Signal:
        """
        Generate trading signal based on current state.

        Returns Signal object with type and all relevant metrics.
        """
        timestamp = datetime.utcnow()

        # Not enough data - must have FULL lookback period before trading
        if len(self.spread_history) < self.lookback:
            return Signal(
                signal_type="NONE",
                zscore=self.current_zscore,
                spread=self.current_spread,
                spread_mean=self.current_mean,
                spread_std=self.current_std,
                hurst=self.current_hurst,
                hurst_ok=None,  # Unknown until we have full data
                std_filter_ok=None,  # Unknown until we have full data
                regime="COLLECTING",
                current_position=self.current_position,
                timestamp=timestamp,
                half_life=self.current_half_life,
            )

        # Check filters
        hurst_ok = not self.config.hurst_enabled or self.current_hurst < self.config.hurst_threshold
        std_ok, _ = self._check_std_filter()

        # Determine regime
        if self.current_hurst < 0.4:
            regime = "MEAN_REVERTING"
        elif self.current_hurst > 0.6:
            regime = "TRENDING"
        else:
            regime = "NEUTRAL"

        # Track SD touches
        spot_price = self.spot_prices[-1] if self.spot_prices else 0
        futures_price = self.futures_prices[-1] if self.futures_prices else 0
        self._track_sd_touch(self.current_zscore, spot_price, futures_price)

        # Determine signal type
        signal_type = "NONE"
        blocked_reason = None

        if self.current_position == "NONE":
            # Check if Z-score crosses entry threshold
            z_triggers_long = self.current_zscore >= self.config.entry_threshold
            z_triggers_short = self.current_zscore <= -self.config.entry_threshold

            if z_triggers_long or z_triggers_short:
                # Z-score triggered - check why it might be blocked
                if not hurst_ok:
                    blocked_reason = "Hurst filter (H={:.3f} > {:.2f})".format(
                        self.current_hurst, self.config.hurst_threshold)
                elif not std_ok:
                    blocked_reason = "STD filter (volatility too low)"
                elif z_triggers_long and self.current_zscore >= self.config.stop_loss_threshold:
                    blocked_reason = "Z-score at stop-loss level ({:.2f} >= {:.2f})".format(
                        self.current_zscore, self.config.stop_loss_threshold)
                elif z_triggers_short and self.current_zscore <= -self.config.stop_loss_threshold:
                    blocked_reason = "Z-score at stop-loss level ({:.2f} <= -{:.2f})".format(
                        self.current_zscore, self.config.stop_loss_threshold)
                elif getattr(self.config, 'trend_direction_filter', False):
                    # Trend direction filter: only allow signals aligned with the
                    # slope of the spread.  Positive slope = Leg B outperforming →
                    # SHORT only.  Negative slope = Leg A outperforming → LONG only.
                    slope = self._spread_slope()
                    if slope > 0 and z_triggers_long:
                        blocked_reason = (
                            "Trend filter: spread rising (slope={:.5f}), SHORT signals only"
                            .format(slope))
                    elif slope < 0 and z_triggers_short:
                        blocked_reason = (
                            "Trend filter: spread falling (slope={:.5f}), LONG signals only"
                            .format(slope))
                    else:
                        signal_type = "LONG" if z_triggers_long else "SHORT"
                else:
                    # All filters pass - generate signal
                    if z_triggers_long:
                        signal_type = "LONG"
                    else:
                        signal_type = "SHORT"

                # Record blocked signal if applicable — only update when the reason
                # changes so the dashboard doesn't flicker on every tick.
                if blocked_reason:
                    prev_reason = self.last_blocked_signal.get('reason') if self.last_blocked_signal else None
                    if blocked_reason != prev_reason:
                        self.last_blocked_signal = {
                            'timestamp': timestamp.isoformat(),
                            'would_be_signal': 'LONG' if z_triggers_long else 'SHORT',
                            'zscore': round(self.current_zscore, 4),
                            'reason': blocked_reason,
                        }
                        logger.debug("Signal blocked: %s (Z=%.4f) - %s",
                                    'LONG' if z_triggers_long else 'SHORT',
                                    self.current_zscore, blocked_reason)

        elif self.current_position == "LONG":
            # LONG entered at high +z. Two exit paths depending on mode:
            #   zscore : exit when rolling z reverts to ±exit_threshold (original)
            #   spread : exit only when the live spread falls back to the
            #            rolling mean *frozen at entry time*. Decouples exit
            #            from rolling-mean drift, which is the failure mode
            #            that turned trade 32 from +$0.22 (theoretical) into
            #            -$1.08 (actual).
            #   hybrid : either fires
            # Stop-loss is always z-score (safety net, not a profit-take).
            mode = getattr(self.config, 'exit_signal_mode', 'zscore') or 'zscore'
            z_exit_hit = self.current_zscore <= self.config.exit_threshold
            spread_exit_hit = (
                self.entry_mean is not None
                and self.current_spread <= self.entry_mean
            )
            if mode == 'spread':
                if spread_exit_hit:
                    signal_type = "EXIT"
            elif mode == 'hybrid':
                if z_exit_hit or spread_exit_hit:
                    signal_type = "EXIT"
            else:  # 'zscore' (default, original behaviour)
                if z_exit_hit:
                    signal_type = "EXIT"
            if signal_type == "NONE" and self.current_zscore >= self.config.stop_loss_threshold:
                signal_type = "STOP_LOSS"

        elif self.current_position == "SHORT":
            # SHORT entered at low -z. Mirror of LONG: in 'spread' mode we
            # exit only when the live spread rises back to the entry mean.
            mode = getattr(self.config, 'exit_signal_mode', 'zscore') or 'zscore'
            z_exit_hit = self.current_zscore >= -self.config.exit_threshold
            spread_exit_hit = (
                self.entry_mean is not None
                and self.current_spread >= self.entry_mean
            )
            if mode == 'spread':
                if spread_exit_hit:
                    signal_type = "EXIT"
            elif mode == 'hybrid':
                if z_exit_hit or spread_exit_hit:
                    signal_type = "EXIT"
            else:  # 'zscore' default
                if z_exit_hit:
                    signal_type = "EXIT"
            if signal_type == "NONE" and self.current_zscore <= -self.config.stop_loss_threshold:
                signal_type = "STOP_LOSS"

        return Signal(
            signal_type=signal_type,
            zscore=self.current_zscore,
            spread=self.current_spread,
            spread_mean=self.current_mean,
            spread_std=self.current_std,
            hurst=self.current_hurst,
            hurst_ok=hurst_ok,
            std_filter_ok=std_ok,
            regime=regime,
            current_position=self.current_position,
            timestamp=timestamp,
            half_life=self.current_half_life,
        )

    def get_spread_history(self, n: int = 100) -> List[float]:
        """Get last n spread values."""
        return list(self.spread_history)[-n:]

    def get_zscore_history(self, n: int = 100) -> List[float]:
        """Calculate Z-score history for charting."""
        if len(self.spread_history) < 20:
            return []

        spreads = list(self.spread_history)
        zscores = []

        for i in range(19, len(spreads)):
            window = spreads[max(0, i - self.lookback + 1):i + 1]
            mean = np.mean(window)
            std = np.std(window, ddof=1)
            if std > 0:
                z = (spreads[i] - mean) / std
                zscores.append(float(z))
            else:
                zscores.append(0.0)

        return zscores[-n:]

    def get_state(self) -> Dict[str, Any]:
        """Get current state for dashboard."""
        # Calculate seconds until next stats update
        if self.last_stats_update:
            elapsed = (datetime.utcnow() - self.last_stats_update).total_seconds()
            next_update_in = max(0, self.stats_update_interval - elapsed)
        else:
            next_update_in = 0

        # Calculate filter status (same logic as generate_signal)
        hurst_ok = not self.config.hurst_enabled or self.current_hurst < self.config.hurst_threshold
        std_ok, std_ratio = self._check_std_filter()

        # Check if we have enough data (must have full lookback period)
        data_ready = len(self.spread_history) >= self.lookback

        # Determine regime
        if self.current_hurst < 0.4:
            regime = "MEAN_REVERTING"
        elif self.current_hurst > 0.6:
            regime = "TRENDING"
        else:
            regime = "NEUTRAL"

        # Suggested lookback based on half-life: 2.5x HL is a reasonable midpoint
        # of the conventional 2-5x range.  None when HL is infinite (no mean reversion).
        hl = self.current_half_life
        suggested_lookback = round(2.5 * hl) if hl != float('inf') and hl > 0 else None

        cost = self._compute_round_trip_cost()

        beta = getattr(self.config, 'hedge_ratio', 1.0) or 1.0
        last_spot = self.spot_prices[-1] if self.spot_prices else 0.0
        last_fut  = self.futures_prices[-1] if self.futures_prices else 0.0

        return {
            'zscore': round(self.current_zscore, 4),
            'spread': round(self.current_spread, 6),
            'spread_mean': round(self.current_mean, 6),
            'spread_std': round(self.current_std, 6),
            'hedge_ratio': beta,
            'beta_x_spot': round(beta * last_spot, 6) if last_spot else 0.0,
            'fut_div_beta': round(last_fut / beta, 6) if last_fut and beta else 0.0,
            'hurst': round(self.current_hurst, 4),
            'half_life': round(hl, 1) if hl != float('inf') else None,
            'suggested_lookback': suggested_lookback,
            'hurst_ok': hurst_ok if data_ready else None,
            'std_filter_ok': std_ok if data_ready else None,
            'std_ratio': round(std_ratio, 2) if std_ratio != float('inf') else None,
            'std_ratio_required': self.config.min_std_multiple,
            'std_filter_enabled': self.config.std_filter_enabled,
            'order_mode': cost['entry_mode'],
            'fee_bps_used': round(cost['entry_cost_bps'], 2),
            'round_trip_cost_bps': round(cost['round_trip_bps'], 2),
            'round_trip_fees_bps': round(cost['fees_bps'], 2),
            'round_trip_slippage_bps': round(cost['slippage_bps'], 2),
            'cost_breakdown': {
                'entry_spot_bps': round(cost['entry_spot_bps'], 2),
                'entry_fut_bps':  round(cost['entry_fut_bps'], 2),
                'exit_spot_bps':  round(cost['exit_spot_bps'], 2),
                'exit_fut_bps':   round(cost['exit_fut_bps'], 2),
                'entry_mode':     cost['entry_mode'],
                'exit_mode':      cost['exit_mode'],
            },
            'regime': regime if data_ready else "COLLECTING",
            'spread_slope': round(self._spread_slope(), 8) if data_ready else 0.0,
            'data_points': len(self.spread_history),
            'lookback': self.lookback,
            'data_ready': data_ready,
            'current_position': self.current_position,
            'stats_update_interval': self.stats_update_interval,
            'last_stats_update': self.last_stats_update.isoformat() if self.last_stats_update else None,
            'next_stats_update_in': round(next_update_in),
            'last_blocked_signal': self.last_blocked_signal,
        }

    def load_spread_history(
        self,
        spreads: List[float],
        spot_prices: Optional[List[float]] = None,
        futures_prices: Optional[List[float]] = None,
    ) -> None:
        """
        Load spread history from external source (e.g., database) on engine
        restart.

        When ``spot_prices`` and ``futures_prices`` are provided (the normal
        recovery path), we **ignore the stored ``spreads`` list and recompute
        every spread under the CURRENT hedge ratio**. This is the only correct
        behavior: a spread persisted under an old hedge ratio is a number with
        the wrong formula attached. Recomputing from the raw prices guarantees
        the entire history is consistent with the running config.

        Without the price components, falls back to using the stored spreads
        verbatim — kept for backward compatibility with callers that don't
        have access to the inputs.
        """
        self.spread_history.clear()
        self.spot_prices.clear()
        self.futures_prices.clear()

        if spot_prices is not None and futures_prices is not None:
            beta = getattr(self.config, 'hedge_ratio', 1.0) or 1.0
            paired = list(zip(spot_prices, futures_prices))[-self.lookback:]
            for S, F in paired:
                self.spot_prices.append(S)
                self.futures_prices.append(F)
                self.spread_history.append(F - beta * S)
            logger.info(
                "Loaded %d ticks; recomputed spreads under current β=%.6f",
                len(paired), beta,
            )
        else:
            for spread in spreads[-self.lookback:]:
                self.spread_history.append(spread)
            logger.info(
                "Loaded %d spread values from history (no price components — "
                "using stored values as-is)", len(self.spread_history),
            )

        if self.spread_history:
            self.current_spread = self.spread_history[-1]
        if len(self.spread_history) >= 2:
            self.last_stats_update = None      # force recompute on this call
            self._stats_initialized = False
            self._update_statistics()

    def optimize_parameters(
        self,
        spread_history: Optional[List[float]] = None,
        lookback_range: Optional[List[int]] = None,
        threshold_range: Optional[List[float]] = None,
    ) -> Dict[str, Any]:
        """
        Grid search over (lookback, entry_threshold) to find the combination that
        maximises mean-reversion PnL on a 70% training slice, then validates on
        the held-out 30% test slice.

        The simulation is intentionally simple: enter when |Z| > threshold, exit
        when Z crosses zero.  Fee costs are NOT deducted — the goal is relative
        comparison across parameter pairs, not absolute P&L prediction.

        Returns a dict with:
          best_lookback, best_threshold, train_pnl, test_pnl,
          half_life, suggested_lookback, grid (raw results for heatmap)
        """
        spreads = spread_history if spread_history is not None else list(self.spread_history)
        spreads = np.array(spreads, dtype=float)

        if len(spreads) < 30:
            return {'error': 'Not enough data (need >= 30 points)', 'grid': []}

        # Default search space
        if lookback_range is None:
            lookback_range = [int(x) for x in np.linspace(5, min(50, len(spreads) // 3), 6)]
            lookback_range = sorted(set(max(3, v) for v in lookback_range))
        if threshold_range is None:
            threshold_range = [round(x, 2) for x in np.linspace(0.5, 2.5, 5)]

        # 70 / 30 split
        split = int(len(spreads) * 0.7)
        train = spreads[:split]
        test = spreads[split:]

        def _sim(data: np.ndarray, lookback: int, threshold: float) -> float:
            total = 0.0
            position = 0        # 0=flat, 1=long, -1=short
            entry_spread = 0.0
            for i in range(lookback, len(data)):
                window = data[i - lookback:i]
                mu = np.mean(window)
                sigma = np.std(window, ddof=1)
                if sigma <= 0:
                    continue
                z = (data[i] - mu) / sigma
                if position == 0:
                    if z >= threshold:
                        position = 1
                        entry_spread = data[i]
                    elif z <= -threshold:
                        position = -1
                        entry_spread = data[i]
                elif position == 1 and z <= 0:
                    total += entry_spread - data[i]
                    position = 0
                elif position == -1 and z >= 0:
                    total += data[i] - entry_spread
                    position = 0
            return total

        # Grid search on training set
        best_pnl = float('-inf')
        best_lb = lookback_range[0]
        best_thr = threshold_range[0]
        grid = []

        for lb in lookback_range:
            for thr in threshold_range:
                pnl = _sim(train, lb, thr)
                grid.append({'lookback': lb, 'threshold': thr, 'train_pnl': round(pnl, 6)})
                if pnl > best_pnl:
                    best_pnl = pnl
                    best_lb = lb
                    best_thr = thr

        test_pnl = _sim(test, best_lb, best_thr)

        # Half-life from full history
        hl = self._calculate_half_life(spreads)
        suggested_lb = round(2.5 * hl) if hl != float('inf') else None

        logger.info(
            "Parameter optimisation: best lookback=%d, threshold=%.2f, "
            "train_pnl=%.4f, test_pnl=%.4f, half_life=%s",
            best_lb, best_thr, best_pnl, test_pnl,
            f"{hl:.1f}" if hl != float('inf') else "inf",
        )

        return {
            'best_lookback': best_lb,
            'best_threshold': best_thr,
            'train_pnl': round(best_pnl, 6),
            'test_pnl': round(test_pnl, 6),
            'half_life': round(hl, 1) if hl != float('inf') else None,
            'suggested_lookback': suggested_lb,
            'train_size': len(train),
            'test_size': len(test),
            'grid': grid,
        }

    def reset(self) -> None:
        """Reset all state."""
        self.spread_history.clear()
        self.spot_prices.clear()
        self.futures_prices.clear()
        self.current_zscore = 0.0
        self.current_spread = 0.0
        self.current_mean = 0.0
        self.current_std = 0.0
        self.current_hurst = 0.5
        self.current_half_life = float('inf')
        self.last_sd_level = 0.0
        self.sd_touch_events.clear()
        self.current_position = "NONE"
        self.entry_mean = None
        self.entry_std = None
        self.last_stats_update = None
        self._stats_initialized = False
        self.last_blocked_signal = None
