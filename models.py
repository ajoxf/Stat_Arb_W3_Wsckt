"""
Data models for the Crypto Statistical Arbitrage Trading System.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime


class SignalType(Enum):
    """Trading signal types."""
    NONE = "NONE"
    LONG = "LONG"      # Long spread (buy spot, sell futures)
    SHORT = "SHORT"    # Short spread (sell spot, buy futures)
    EXIT = "EXIT"      # Exit current position
    STOP_LOSS = "STOP_LOSS"  # Stop loss triggered


class PositionType(Enum):
    """Position types."""
    NONE = "NONE"
    LONG = "LONG"
    SHORT = "SHORT"


class OrderSide(Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    """Order type."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class ExchangeStatus(Enum):
    """Exchange connection status."""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class ExchangeRole(Enum):
    """Exchange role in trading."""
    SPOT = "SPOT"
    FUTURES = "FUTURES"
    BOTH = "BOTH"


@dataclass
class TradingConfig:
    """Trading configuration settings."""
    id: int = 1  # Singleton
    asset: str = "BTC"
    spot_symbol: str = "BTC-USDT"
    futures_symbol: str = "BTC-USDT-SWAP"

    # Z-score thresholds
    entry_threshold: float = 2.0
    exit_threshold: float = 0.5
    stop_loss_threshold: float = 4.0

    # ── Fast-exit overrides (trending-market protection) ─────────────────────
    # These let a position exit on dollars/time instead of waiting for the
    # z-score to fully revert to ±exit_threshold — important in trending
    # markets where the rolling mean drifts and profit erodes before z hits 0.5.
    # All default 0 = DISABLED, preserving the pure z-score exit behaviour.
    # "Net" P&L means gross minus the same round-trip fee estimate used at the
    # realized close, so these fire on take-home dollars, not gross.
    #
    # Two flavours per override: a SCALE-INVARIANT form (recommended — keeps
    # working unchanged across position sizes and pairs) and a fixed-DOLLAR
    # form (simple hard cap). The scale-invariant form takes precedence when
    # set (>0); otherwise the dollar form is used; if both 0 the override is off.
    #
    # Profit target:
    #   scale-invariant = fraction of the expected full reversion in dollars:
    #     target$ = sigma_frac × |entry_zscore| × entry_spread_std × quantity
    #   recommended sigma_frac ≈ 0.6–0.7 (capture ~two-thirds of the move).
    profit_target_sigma_frac: float = 0.0
    profit_target_usd: float = 0.0   # fixed-$ fallback
    # Cost floor: an active profit target must clear the round-trip fees by this
    # multiple before it can fire, so a statistically-small target can never
    # exit at a net loss once the exit crosses the spread.
    #     target$ = max(target$, profit_target_min_cost_mult × round_trip_fees)
    #   recommended ≈ 1.0 (net profit at least equal to total fees paid).
    profit_target_min_cost_mult: float = 0.0
    #
    # Max hold:
    #   scale-invariant = multiple of the measured mean-reversion half-life:
    #     exit when periods_held >= halflife_mult × half_life (and net P&L > 0)
    #   recommended halflife_mult ≈ 4 (range 2–6; ~4× half-lives ≈ 94% reverted).
    #   Only fires on a profitable, stalled trade — the Z-progress gate below
    #   still protects a position that is actively reverting.
    max_hold_halflife_mult: float = 0.0
    max_hold_minutes: float = 0.0    # fixed-minutes fallback
    # Z-progress gate: MAX_HOLD is suppressed while the trade is actively
    # reverting. If Z has already crossed this fraction of the entry→exit
    # distance, skip the MAX_HOLD exit and let the profit target or Z-exit
    # fire instead. 0 = no gate (always exit on time), 1 = never exit on time.
    # Recommended: 0.5 (suppress MAX_HOLD once Z is more than halfway home).
    max_hold_z_progress_min: float = 0.5
    #
    # Dollar stop:
    #   scale-invariant = percent of capital-at-risk (per-leg margin + buffer):
    #     stop$ = capital_pct/100 × capital_at_risk
    #   recommended capital_pct ≈ 0.5–1.0 (% of locked capital per trade).
    stop_loss_capital_pct: float = 0.0
    max_loss_usd: float = 0.0        # fixed-$ fallback

    # Exit-signal mode. The default ("zscore") matches the original behaviour:
    # exit when the rolling z-score reverts to ±exit_threshold. The problem is
    # that the rolling mean drifts during the hold — z can revert to ~0 without
    # the spread actually moving back, producing trades that "look like" mean
    # reversion exits but are actually flat-or-losing in dollars (see trade 32
    # post-mortem). The "spread" mode freezes the rolling mean at entry time
    # and exits only when the live spread crosses back over that frozen value;
    # "hybrid" exits when either condition fires. Stop-loss always uses z-score
    # regardless of mode — that's a safety net, not a profit-take.
    exit_signal_mode: str = "zscore"  # zscore | spread | hybrid

    # Rolling window settings
    lookback_period: int = 100
    stats_update_interval: int = 300  # Seconds between mean/std recalculation (default 5 min)

    # Filters
    hurst_enabled: bool = True
    hurst_threshold: float = 0.5  # H < 0.5 = mean reverting
    std_filter_enabled: bool = True
    min_std_multiple: float = 0.9  # STD must be > costs * multiple (0.9 = just above break-even)
    trend_direction_filter: bool = False  # Only take signals aligned with spread slope direction
    z_reset_gate_enabled: bool = True  # After stop-loss, block same-side re-entry until z reverts

    # Position sizing
    position_size_usd: float = 1000.0
    max_position_size_usd: float = 10000.0
    daily_max_loss_usd: float = 0.0

    # Leverage settings
    spot_leverage: int = 1      # 1 = no margin; up to 50x for BTC/ETH perps on OKX
    futures_leverage: int = 1   # 1-50x for BTC/ETH perps on OKX

    # Hedge ratio (beta) between the two legs: spread = futures - hedge_ratio * spot.
    # 1.0 = classic basis trade (same underlying, e.g. BTC spot vs BTC-SWAP).
    # For cross-instrument pairs (e.g. BTC vs ETH) set the ratio so the legs are
    # comparable; spot leg size is scaled by this ratio to stay dollar-hedged.
    hedge_ratio: float = 1.0

    # M2M (mark-to-market) buffer, as a percent on top of the required per-leg
    # margin. Headroom for fees, slippage, and adverse price drift between the
    # balance check and order fill. The pre-trade balance guard requires
    # available >= total_margin * (1 + m2m_buffer_pct/100). Default 10%;
    # raise (e.g. 30%) for more safety margin against liquidation on volatile pairs.
    m2m_buffer_pct: float = 10.0

    # Reward/risk multiple. NOTE: this SETS the dollar stop, it is not a pure
    # entry gate — _effective_exit_targets derives stop$ = profit_target$ / this,
    # and the effective stop is the TIGHTER of that and the %-capital cap. So a
    # HIGHER value = a TIGHTER stop (fires sooner); a LOWER value = a wider stop
    # (more room to revert). 0.0 = disabled (stop then comes only from the
    # %-capital cap / max_loss_usd). ~0.8–1.0 gives mean-reversion room.
    min_entry_rr_multiple: float = 0.0

    # Trading mode
    paper_trading: bool = True
    algo_enabled: bool = False

    # Order execution mode: "MARKET" or "LIMIT"
    # Separate modes for entries vs exits to optimize fee/slippage tradeoff
    order_execution_mode: str = "MARKET"  # Legacy field, kept for backward compatibility
    entry_execution_mode: str = "LIMIT"   # Entries: LIMIT for maker fees (less urgent)
    exit_execution_mode: str = "LIMIT"    # Exits: LIMIT for maker fees (saves ~5 bps vs MARKET)
    # Limit order settings
    limit_order_timeout_sec: int = 30  # Max time to wait for fill
    limit_order_price_offset_bps: float = 1.0  # Offset from best bid/ask in basis points

    # Institutional order slicing (synchronized pair TWAP entry)
    entry_slices: int = 1             # N child orders per entry leg (1 = no slicing)
    entry_slice_interval_sec: float = 5.0  # Seconds between slices
    min_fill_ratio: float = 0.95      # Reject entry if total fills < this fraction of target qty

    # Fee estimates for STD filter (per side, in basis points)
    # Spot and Futures have different fee structures on OKX
    # Spot (non-VIP): Maker 8 bps, Taker 10 bps
    # Futures (non-VIP): Maker 2 bps, Taker 5 bps
    spot_maker_fee_bps: float = 8.0    # Spot limit orders (0.08%)
    spot_taker_fee_bps: float = 10.0   # Spot market orders (0.10%)
    futures_maker_fee_bps: float = 2.0  # Futures limit orders (0.02%)
    futures_taker_fee_bps: float = 5.0  # Futures market orders (0.05%)

    # Legacy fields - kept for backward compatibility
    taker_fee_bps: float = 5.0
    maker_fee_bps: float = 2.0

    # Safety settings
    entry_cooldown_seconds: int = 60  # Minimum seconds between trades (prevents rapid re-entry)
    verify_exchange_position: bool = True  # Check exchange for existing positions before entry

    # Orphan leg recovery: try LIMIT order for unfilled leg before falling back to market
    orphan_recovery_timeout_sec: int = 60  # Seconds to try filling orphan leg as maker

    # Slippage estimate per leg (applied to all 4 legs: spot+futures × entry+exit)
    # LIMIT orders: ~1-2 bps queue/amendment slippage on liquid markets (BTC/ETH)
    # MARKET orders: ~5-10 bps market impact, more in high-vol conditions
    slippage_bps: float = 1.5

    # ── Post-entry exit overrides ──────────────────────────────────────────
    #
    # Hurst regime-change exit: fires when H > hurst_exit_threshold for
    # hurst_exit_n_ticks consecutive ticks after entry.  H crossing 0.5+ means
    # the spread has flipped from mean-reverting to trending — exit before
    # the dollar stop is hit.  Distinct from hurst_threshold (entry filter).
    hurst_exit_enabled: bool = False
    hurst_exit_threshold: float = 0.55   # H > this signals trending regime
    hurst_exit_n_ticks: int = 3          # consecutive ticks required to fire

    # Spread velocity exit: fires when adverse spread drift exceeds
    # velocity_exit_pts_per_min for velocity_exit_n_ticks consecutive ticks.
    # Adverse = spread rising for SHORT, spread falling for LONG.
    # Velocity is measured over a rolling window of velocity_exit_window_ticks
    # ticks (each tick ≈ 0.5s, so 20 ticks ≈ 10s of price history).
    velocity_exit_enabled: bool = False
    velocity_exit_pts_per_min: float = 2.0   # spread-point threshold per minute
    velocity_exit_n_ticks: int = 5           # consecutive ticks above threshold
    velocity_exit_window_ticks: int = 20     # rolling window for velocity calc

    # ── Trailing stop ──────────────────────────────────────────────────────
    # Activates once P&L reaches trailing_stop_floor_pct % of the profit target
    # (e.g. 70 = armed when P&L ≥ 70% of target). Once armed, fires when P&L
    # drops trailing_stop_pct % below its peak (e.g. 20 = exit if P&L retraces
    # 20% from the high). Both values = 0 disables trailing stop entirely.
    # trailing_stop_floor_pct = 0 → trailing is armed from first profitable tick.
    trailing_stop_pct: float = 0.0          # pullback % from peak to trigger (e.g. 20)
    trailing_stop_floor_pct: float = 0.0    # activate only when P&L ≥ X% of target (e.g. 70)

    # RFQ / Block trading — OKX atomic multi-leg execution
    # When per-leg notional >= rfq_notional_threshold_usd the engine routes to
    # OKX RFQ instead of the live order book, eliminating legging risk entirely.
    # Both legs fill in a single matching event from a market maker's quote.
    # Set rfq_notional_threshold_usd = 0 to disable (order book always used).
    # OKX RFQ minimum at VIP4 is ~$100k notional per leg, so set the threshold at
    # or above that once demo-validated — below it OKX rejects the RFQ (we then
    # fall back to the order book). Keep 0 (disabled) until the demo loop passes.
    rfq_notional_threshold_usd: float = 0.0   # 0 = disabled; set 100000 after validation
    rfq_anonymous: bool = True                 # hide identity from market makers
    rfq_counterparties: str = ""               # comma-separated OKX trader codes; empty = all makers
    rfq_quote_timeout_sec: float = 10.0        # seconds to wait for quotes before fallback
    rfq_min_quotes: int = 1                    # minimum quotes needed before executing
    rfq_fallback_to_orderbook: bool = True     # fall back to order book if RFQ fails
    # Reject a quote whose all-in markup vs mid exceeds this (per leg, bps); the
    # trade then falls back to the order book. 0 = no guard (NOT recommended once
    # RFQ is live — a wide maker quote can eat the whole edge). Calibrate from the
    # demo validation: set above typical observed markup, below your per-trade edge.
    rfq_max_markup_bps: float = 5.0

    # Self-learning: automatically apply Claude's parameter recommendations
    auto_tune_enabled: bool = False

    # Legacy field - now computed from taker/maker fees based on order mode
    estimated_costs_bps: float = 10.0  # Kept for backward compatibility

    # Telegram notifications
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_notify_trades: bool = True
    telegram_notify_signals: bool = False
    telegram_notify_errors: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'asset': self.asset,
            'spot_symbol': self.spot_symbol,
            'futures_symbol': self.futures_symbol,
            'entry_threshold': self.entry_threshold,
            'exit_threshold': self.exit_threshold,
            'stop_loss_threshold': self.stop_loss_threshold,
            'profit_target_sigma_frac': self.profit_target_sigma_frac,
            'profit_target_usd': self.profit_target_usd,
            'profit_target_min_cost_mult': self.profit_target_min_cost_mult,
            'max_hold_halflife_mult': self.max_hold_halflife_mult,
            'max_hold_minutes': self.max_hold_minutes,
            'max_hold_z_progress_min': self.max_hold_z_progress_min,
            'stop_loss_capital_pct': self.stop_loss_capital_pct,
            'max_loss_usd': self.max_loss_usd,
            'exit_signal_mode': self.exit_signal_mode,
            'lookback_period': self.lookback_period,
            'stats_update_interval': self.stats_update_interval,
            'hurst_enabled': self.hurst_enabled,
            'hurst_threshold': self.hurst_threshold,
            'std_filter_enabled': self.std_filter_enabled,
            'min_std_multiple': self.min_std_multiple,
            'trend_direction_filter': self.trend_direction_filter,
            'position_size_usd': self.position_size_usd,
            'max_position_size_usd': self.max_position_size_usd,
            'daily_max_loss_usd': self.daily_max_loss_usd,
            'spot_leverage': self.spot_leverage,
            'futures_leverage': self.futures_leverage,
            'hedge_ratio': self.hedge_ratio,
            'm2m_buffer_pct': self.m2m_buffer_pct,
            'paper_trading': self.paper_trading,
            'algo_enabled': self.algo_enabled,
            'order_execution_mode': self.order_execution_mode,
            'entry_execution_mode': self.entry_execution_mode,
            'exit_execution_mode': self.exit_execution_mode,
            'limit_order_timeout_sec': self.limit_order_timeout_sec,
            'limit_order_price_offset_bps': self.limit_order_price_offset_bps,
            'entry_slices': self.entry_slices,
            'entry_slice_interval_sec': self.entry_slice_interval_sec,
            'min_fill_ratio': self.min_fill_ratio,
            'spot_maker_fee_bps': self.spot_maker_fee_bps,
            'spot_taker_fee_bps': self.spot_taker_fee_bps,
            'futures_maker_fee_bps': self.futures_maker_fee_bps,
            'futures_taker_fee_bps': self.futures_taker_fee_bps,
            'taker_fee_bps': self.taker_fee_bps,
            'maker_fee_bps': self.maker_fee_bps,
            'slippage_bps': self.slippage_bps,
            'auto_tune_enabled': self.auto_tune_enabled,
            'estimated_costs_bps': self.estimated_costs_bps,
            'entry_cooldown_seconds': self.entry_cooldown_seconds,
            'verify_exchange_position': self.verify_exchange_position,
            'orphan_recovery_timeout_sec': self.orphan_recovery_timeout_sec,
            'telegram_enabled': self.telegram_enabled,
            'telegram_bot_token': '***' if self.telegram_bot_token else '',
            'telegram_chat_id': self.telegram_chat_id,
            'telegram_notify_trades': self.telegram_notify_trades,
            'telegram_notify_signals': self.telegram_notify_signals,
            'telegram_notify_errors': self.telegram_notify_errors,
            'rfq_notional_threshold_usd': self.rfq_notional_threshold_usd,
            'rfq_anonymous': self.rfq_anonymous,
            'rfq_counterparties': self.rfq_counterparties,
            'rfq_quote_timeout_sec': self.rfq_quote_timeout_sec,
            'rfq_min_quotes': self.rfq_min_quotes,
            'rfq_fallback_to_orderbook': self.rfq_fallback_to_orderbook,
            'hurst_exit_enabled': self.hurst_exit_enabled,
            'hurst_exit_threshold': self.hurst_exit_threshold,
            'hurst_exit_n_ticks': self.hurst_exit_n_ticks,
            'velocity_exit_enabled': self.velocity_exit_enabled,
            'velocity_exit_pts_per_min': self.velocity_exit_pts_per_min,
            'velocity_exit_n_ticks': self.velocity_exit_n_ticks,
            'velocity_exit_window_ticks': self.velocity_exit_window_ticks,
            'trailing_stop_pct': self.trailing_stop_pct,
            'trailing_stop_floor_pct': self.trailing_stop_floor_pct,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TradingConfig':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Exchange:
    """Exchange configuration and credentials."""
    id: Optional[int] = None
    name: str = ""
    exchange_type: str = ""  # okx, binance, bybit
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""  # OKX only
    is_testnet: bool = True
    role: str = "BOTH"  # SPOT, FUTURES, BOTH
    is_active: bool = False
    status: str = "DISCONNECTED"
    last_error: str = ""
    created_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'exchange_type': self.exchange_type,
            'api_key': self.api_key,
            'secret_key': '***' if self.secret_key else '',  # Masked
            'passphrase': '***' if self.passphrase else '',  # Masked
            'is_testnet': self.is_testnet,
            'role': self.role,
            'is_active': self.is_active,
            'status': self.status,
            'last_error': self.last_error,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def to_dict_with_secrets(self) -> Dict[str, Any]:
        """Return dict including secrets (for internal use)."""
        d = self.to_dict()
        d['secret_key'] = self.secret_key
        d['passphrase'] = self.passphrase
        return d


@dataclass
class Trade:
    """Trade record."""
    id: Optional[int] = None
    asset: str = ""
    position_type: str = "LONG"  # LONG or SHORT

    # Entry details
    entry_time: Optional[datetime] = None
    entry_spot_price: float = 0.0
    entry_futures_price: float = 0.0
    entry_spread: float = 0.0
    entry_zscore: float = 0.0
    entry_spread_mean: float = 0.0   # Rolling mean at entry time — used as exit target
    entry_spread_std: float = 0.0    # Rolling std at entry time — for reference

    # Exit details
    exit_time: Optional[datetime] = None
    exit_spot_price: float = 0.0
    exit_futures_price: float = 0.0
    exit_spread: float = 0.0
    exit_zscore: float = 0.0
    exit_reason: str = ""  # EXIT, STOP_LOSS, MANUAL

    # Position details
    quantity: float = 0.0
    notional_usd: float = 0.0

    # P&L
    pnl_usd: float = 0.0
    pnl_percent: float = 0.0
    fees_usd: float = 0.0      # realized round-trip fees from OKX (4 leg-fills)
    entry_fees_usd: float = 0.0  # actual fees captured from OKX fills at entry (transient)
    exit_fees_usd:  float = 0.0  # actual fees captured from OKX fills at exit  (transient)
    pnl_gross_usd: float = 0.0 # P&L before fees (audit trail: pnl_gross - fees = pnl_usd)
    # ── Capital metrics (return-on-margin tracking) ──────────────────────────
    # Actually-locked capital at trade open: per-leg margin + M2M buffer. This
    # is what the user can't deploy elsewhere while the trade is open — the
    # honest denominator for "% return on what I actually risked".
    capital_locked_usd: float = 0.0
    # Return on locked capital (pnl_usd / capital_locked_usd × 100). The
    # existing pnl_percent reports return on Leg A notional, which understates
    # actual return-on-capital by ~3-5× at typical leverage. Both kept so the
    # operator can see the strategy edge (notional %) and the operator's
    # personal return (capital %).
    pnl_pct_on_capital: float = 0.0

    # Actual execution modes recorded at close — LIMIT (maker) or MARKET (taker).
    # Entry never falls back from LIMIT to MARKET (rejected limit orders just retry);
    # exit falls back to MARKET after the first POST_ONLY rejection.
    actual_entry_mode: str = ""
    actual_exit_mode: str = ""

    # Order IDs
    spot_order_id: str = ""
    futures_order_id: str = ""

    # Execution timing (populated after live order fill)
    entry_placed_at: Optional[datetime] = None   # When orders were sent to exchange
    entry_filled_at: Optional[datetime] = None   # When both legs confirmed filled
    entry_latency_ms: Optional[float] = None     # placed → filled in ms
    exit_placed_at: Optional[datetime] = None
    exit_filled_at: Optional[datetime] = None
    exit_latency_ms: Optional[float] = None

    # Margin
    margin_usd: float = 0.0  # Futures margin requirement (notional / leverage)

    # Status
    is_open: bool = True
    is_paper: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'asset': self.asset,
            'position_type': self.position_type,
            'entry_time': self.entry_time.isoformat() if self.entry_time else None,
            'entry_spot_price': self.entry_spot_price,
            'entry_futures_price': self.entry_futures_price,
            'entry_spread': self.entry_spread,
            'entry_zscore': self.entry_zscore,
            'entry_spread_mean': self.entry_spread_mean,
            'entry_spread_std': self.entry_spread_std,
            'exit_time': self.exit_time.isoformat() if self.exit_time else None,
            'exit_spot_price': self.exit_spot_price,
            'exit_futures_price': self.exit_futures_price,
            'exit_spread': self.exit_spread,
            'exit_zscore': self.exit_zscore,
            'exit_reason': self.exit_reason,
            'quantity': self.quantity,
            'notional_usd': self.notional_usd,
            'pnl_usd': self.pnl_usd,
            'pnl_percent': self.pnl_percent,
            'pnl_gross_usd': self.pnl_gross_usd,
            'fees_usd': self.fees_usd,
            'capital_locked_usd': self.capital_locked_usd,
            'pnl_pct_on_capital': self.pnl_pct_on_capital,
            'spot_order_id': self.spot_order_id,
            'futures_order_id': self.futures_order_id,
            'entry_placed_at': self.entry_placed_at.isoformat() if self.entry_placed_at else None,
            'entry_filled_at': self.entry_filled_at.isoformat() if self.entry_filled_at else None,
            'entry_latency_ms': self.entry_latency_ms,
            'exit_placed_at': self.exit_placed_at.isoformat() if self.exit_placed_at else None,
            'exit_filled_at': self.exit_filled_at.isoformat() if self.exit_filled_at else None,
            'exit_latency_ms': self.exit_latency_ms,
            'margin_usd': self.margin_usd,
            'is_open': self.is_open,
            'is_paper': self.is_paper,
        }


@dataclass
class MarketTick:
    """Market tick data."""
    symbol: str = ""
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume_24h: float = 0.0
    timestamp: Optional[datetime] = None

    @property
    def mid(self) -> float:
        """Mid price."""
        return (self.bid + self.ask) / 2 if self.bid and self.ask else self.last

    @property
    def spread_bps(self) -> float:
        """Bid-ask spread in basis points."""
        if self.mid > 0:
            return ((self.ask - self.bid) / self.mid) * 10000
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'bid': self.bid,
            'ask': self.ask,
            'last': self.last,
            'mid': self.mid,
            'volume_24h': self.volume_24h,
            'spread_bps': self.spread_bps,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
        }


@dataclass
class Signal:
    """Trading signal."""
    signal_type: str = "NONE"
    zscore: float = 0.0
    spread: float = 0.0
    spread_mean: float = 0.0
    spread_std: float = 0.0
    hurst: float = 0.5
    hurst_ok: Optional[bool] = True  # None when data is still being collected
    std_filter_ok: Optional[bool] = True  # None when data is still being collected
    regime: str = "UNKNOWN"  # MEAN_REVERTING, TRENDING, COLLECTING, UNKNOWN
    timestamp: Optional[datetime] = None

    # Current position context
    current_position: str = "NONE"

    # Half-life of mean reversion (periods); inf = not mean-reverting
    half_life: float = float('inf')

    def to_dict(self) -> Dict[str, Any]:
        return {
            'signal_type': self.signal_type,
            'zscore': self.zscore,
            'spread': self.spread,
            'spread_mean': self.spread_mean,
            'spread_std': self.spread_std,
            'hurst': self.hurst,
            'hurst_ok': self.hurst_ok,
            'std_filter_ok': self.std_filter_ok,
            'regime': self.regime,
            'current_position': self.current_position,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'half_life': self.half_life if self.half_life != float('inf') else None,
        }


@dataclass
class OrderResult:
    """Order execution result."""
    success: bool = False
    order_id: str = ""
    filled_qty: float = 0.0
    filled_price: float = 0.0
    commission: float = 0.0
    error: str = ""
    already_flat: bool = False  # OKX sCode 51169: no position to close in this direction

    def to_dict(self) -> Dict[str, Any]:
        return {
            'success': self.success,
            'order_id': self.order_id,
            'filled_qty': self.filled_qty,
            'filled_price': self.filled_price,
            'commission': self.commission,
            'error': self.error,
        }


@dataclass
class Position:
    """Current position."""
    symbol: str = ""
    side: str = ""  # LONG, SHORT
    quantity: float = 0.0
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'side': self.side,
            'quantity': self.quantity,
            'entry_price': self.entry_price,
            'unrealized_pnl': self.unrealized_pnl,
            'leverage': self.leverage,
        }


@dataclass
class AccountInfo:
    """Account information with margin details."""
    exchange: str = ""
    balance_usd: float = 0.0
    available_balance_usd: float = 0.0
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0

    # Account identification
    uid: str = ""                       # User ID from exchange
    account_level: str = ""             # Account level/type

    # Enhanced margin details
    total_equity: float = 0.0           # Total account equity
    initial_margin: float = 0.0         # Initial margin requirement (IMR)
    maintenance_margin: float = 0.0     # Maintenance margin requirement (MMR)
    margin_ratio: float = 0.0           # Current margin ratio (%)
    available_margin: float = 0.0       # Available margin for new positions

    # Position-level details
    spot_margin_used: float = 0.0       # Margin used for spot positions
    futures_margin_used: float = 0.0    # Margin used for futures positions
    spot_unrealized_pnl: float = 0.0    # Unrealized P&L from spot
    futures_unrealized_pnl: float = 0.0 # Unrealized P&L from futures

    # Risk metrics
    liquidation_price: Optional[float] = None  # Estimated liquidation price
    mark_price: Optional[float] = None         # Current mark price
    leverage_used: float = 1.0                 # Effective leverage

    def to_dict(self) -> Dict[str, Any]:
        return {
            'exchange': self.exchange,
            'balance_usd': self.balance_usd,
            'available_balance_usd': self.available_balance_usd,
            'margin_used': self.margin_used,
            'unrealized_pnl': self.unrealized_pnl,
            'uid': self.uid,
            'account_level': self.account_level,
            'total_equity': self.total_equity,
            'initial_margin': self.initial_margin,
            'maintenance_margin': self.maintenance_margin,
            'margin_ratio': self.margin_ratio,
            'available_margin': self.available_margin,
            'spot_margin_used': self.spot_margin_used,
            'futures_margin_used': self.futures_margin_used,
            'spot_unrealized_pnl': self.spot_unrealized_pnl,
            'futures_unrealized_pnl': self.futures_unrealized_pnl,
            'liquidation_price': self.liquidation_price,
            'mark_price': self.mark_price,
            'leverage_used': self.leverage_used,
        }


@dataclass
class SDTouchEvent:
    """Standard deviation touch event for analysis."""
    id: Optional[int] = None
    asset: str = ""
    timestamp: Optional[datetime] = None
    sd_level: float = 0.0  # -2, -1, 1, 2, etc.
    direction: str = ""  # UP or DOWN (which direction it touched from)
    spread: float = 0.0
    zscore: float = 0.0
    spot_price: float = 0.0
    futures_price: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'asset': self.asset,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'sd_level': self.sd_level,
            'direction': self.direction,
            'spread': self.spread,
            'zscore': self.zscore,
            'spot_price': self.spot_price,
            'futures_price': self.futures_price,
        }


# Crypto asset configurations
CRYPTO_ASSETS: Dict[str, Dict[str, str]] = {
    'BTC': {
        'name': 'Bitcoin',
        'okx_spot': 'BTC-USDT',
        'okx_futures': 'BTC-USDT-SWAP',
        'binance_spot': 'BTCUSDT',
        'binance_futures': 'BTCUSDT',
        'bybit_spot': 'BTCUSDT',
        'bybit_futures': 'BTCUSDT',
    },
    'ETH': {
        'name': 'Ethereum',
        'okx_spot': 'ETH-USDT',
        'okx_futures': 'ETH-USDT-SWAP',
        'binance_spot': 'ETHUSDT',
        'binance_futures': 'ETHUSDT',
        'bybit_spot': 'ETHUSDT',
        'bybit_futures': 'ETHUSDT',
    },
    'SOL': {
        'name': 'Solana',
        'okx_spot': 'SOL-USDT',
        'okx_futures': 'SOL-USDT-SWAP',
        'binance_spot': 'SOLUSDT',
        'binance_futures': 'SOLUSDT',
        'bybit_spot': 'SOLUSDT',
        'bybit_futures': 'SOLUSDT',
    },
    'XRP': {
        'name': 'Ripple',
        'okx_spot': 'XRP-USDT',
        'okx_futures': 'XRP-USDT-SWAP',
        'binance_spot': 'XRPUSDT',
        'binance_futures': 'XRPUSDT',
        'bybit_spot': 'XRPUSDT',
        'bybit_futures': 'XRPUSDT',
    },
    'DOGE': {
        'name': 'Dogecoin',
        'okx_spot': 'DOGE-USDT',
        'okx_futures': 'DOGE-USDT-SWAP',
        'binance_spot': 'DOGEUSDT',
        'binance_futures': 'DOGEUSDT',
        'bybit_spot': 'DOGEUSDT',
        'bybit_futures': 'DOGEUSDT',
    },
    'AVAX': {
        'name': 'Avalanche',
        'okx_spot': 'AVAX-USDT',
        'okx_futures': 'AVAX-USDT-SWAP',
        'binance_spot': 'AVAXUSDT',
        'binance_futures': 'AVAXUSDT',
        'bybit_spot': 'AVAXUSDT',
        'bybit_futures': 'AVAXUSDT',
    },
    'LINK': {
        'name': 'Chainlink',
        'okx_spot': 'LINK-USDT',
        'okx_futures': 'LINK-USDT-SWAP',
        'binance_spot': 'LINKUSDT',
        'binance_futures': 'LINKUSDT',
        'bybit_spot': 'LINKUSDT',
        'bybit_futures': 'LINKUSDT',
    },
}


def get_symbols_for_asset(asset: str, exchange_type: str) -> tuple:
    """Get spot and futures symbols for an asset on a specific exchange."""
    if asset not in CRYPTO_ASSETS:
        raise ValueError(f"Unknown asset: {asset}")

    config = CRYPTO_ASSETS[asset]
    exchange_type = exchange_type.lower()

    spot_key = f"{exchange_type}_spot"
    futures_key = f"{exchange_type}_futures"

    if spot_key not in config or futures_key not in config:
        raise ValueError(f"Unknown exchange type: {exchange_type}")

    return config[spot_key], config[futures_key]
