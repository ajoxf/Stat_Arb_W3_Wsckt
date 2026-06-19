"""
Replay the live SignalGenerator math on historical 1-minute data.

Mirrors core/signals.py exactly so backtest results are an apples-to-apples
projection of how the live bot would have traded the same window.

The spread definition, z-score formula, STD filter, entry/exit/stop-loss
rules, and fee/slippage accounting are all 1:1 with production. Hurst is
optional (matches live default: disabled).

Output: a DataFrame of completed trades with entry/exit timestamps, prices,
signal type, gross P&L, fees, net P&L.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd


# Default VIP 4 Group 1 rates — override per-call if operator's tier changes.
VIP4_FEES = dict(spot_maker=3.0, spot_taker=4.5, fut_maker=0.8, fut_taker=2.7)


@dataclass
class ReplayConfig:
    # Strategy
    lookback: int = 3600                # minutes (matches live ~1h at 0.5s tick)
    entry_threshold: float = 4.5        # z-score
    exit_threshold: float = 0.0         # z-score
    stop_loss_threshold: float = 4.0    # |z| above entry → emergency exit

    # Filters
    std_filter_enabled: bool = True
    min_std_multiple: float = 1.0           # min edge ratio (capture ÷ cost)
    profit_target_sigma_frac: float = 0.0   # f for the capture estimate / floor coherence
    profit_target_min_cost_mult: float = 0.0
    hurst_enabled: bool = False
    hurst_threshold: float = 0.5
    hurst_window: int = 100

    # Fees (bps)
    spot_maker_bps: float = VIP4_FEES["spot_maker"]
    spot_taker_bps: float = VIP4_FEES["spot_taker"]
    fut_maker_bps:  float = VIP4_FEES["fut_maker"]
    fut_taker_bps:  float = VIP4_FEES["fut_taker"]
    slippage_bps_per_leg: float = 1.5
    entry_mode: str = "LIMIT"           # LIMIT → maker, MARKET → taker
    exit_mode: str = "LIMIT"

    # Sizing
    notional_per_leg_usd: float = 1000.0
    max_concurrent_positions: int = 1   # per pair


@dataclass
class Trade:
    pair_id: str
    signal_type: str                    # LONG or SHORT
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_spot: float
    entry_futures: float
    exit_spot: float
    exit_futures: float
    notional_usd: float
    gross_pnl: float
    fees: float
    slippage: float
    net_pnl: float
    exit_reason: str                    # mean_revert | stop_loss | data_end
    bars_held: int


def _round_trip_cost_bps(cfg: ReplayConfig) -> float:
    """Same formula as core/signals.py:_compute_round_trip_cost."""
    spot_entry = cfg.spot_maker_bps if cfg.entry_mode == "LIMIT" else cfg.spot_taker_bps
    fut_entry  = cfg.fut_maker_bps  if cfg.entry_mode == "LIMIT" else cfg.fut_taker_bps
    spot_exit  = cfg.spot_maker_bps if cfg.exit_mode  == "LIMIT" else cfg.spot_taker_bps
    fut_exit   = cfg.fut_maker_bps  if cfg.exit_mode  == "LIMIT" else cfg.fut_taker_bps
    fees = spot_entry + fut_entry + spot_exit + fut_exit
    slippage = cfg.slippage_bps_per_leg * 4
    return fees + slippage


def _hurst_rs(values: np.ndarray, max_lag: int = 20) -> float:
    """Rescaled-range Hurst estimator. Matches core/signals.py implementation."""
    if len(values) < max_lag + 2:
        return 0.5
    lags = range(2, min(max_lag, len(values) // 2))
    tau = []
    for lag in lags:
        diff = np.subtract(values[lag:], values[:-lag])
        std = np.std(diff)
        if std > 0:
            tau.append(np.sqrt(std))
    if len(tau) < 4:
        return 0.5
    log_lags = np.log(list(lags)[:len(tau)])
    log_tau = np.log(tau)
    slope = np.polyfit(log_lags, log_tau, 1)[0]
    return float(slope * 2.0)


def replay_pair(spot_df: pd.DataFrame,
                futures_df: pd.DataFrame,
                cfg: ReplayConfig,
                pair_id: str) -> pd.DataFrame:
    """
    Walk through 1-minute aligned candles for one pair and emit a trade
    every time the live bot's state machine would have entered → exited.

    `spot_df` and `futures_df` must each have columns: ts, c (close).
    """
    merged = pd.merge_asof(
        spot_df[["ts", "c"]].rename(columns={"c": "spot"}).sort_values("ts"),
        futures_df[["ts", "c"]].rename(columns={"c": "futures"}).sort_values("ts"),
        on="ts",
        direction="nearest",
        tolerance=pd.Timedelta("90s"),
    ).dropna().reset_index(drop=True)
    if len(merged) <= cfg.lookback + 50:
        return pd.DataFrame()

    spread = (merged["futures"] - merged["spot"]).to_numpy()
    spot_arr = merged["spot"].to_numpy()
    fut_arr = merged["futures"].to_numpy()
    ts = merged["ts"]

    # Rolling stats — vectorised
    spread_series = pd.Series(spread)
    rolling_mean = spread_series.rolling(cfg.lookback).mean()
    rolling_std = spread_series.rolling(cfg.lookback).std(ddof=0)
    zscore = (spread_series - rolling_mean) / rolling_std.replace(0, np.nan)

    round_trip_bps = _round_trip_cost_bps(cfg)

    trades: List[Trade] = []
    position: Optional[str] = None
    entry_idx: Optional[int] = None
    entry_spread: float = 0.0
    entry_mean: float = 0.0
    entry_std: float = 0.0

    # Pre-compute edge-filter passes — mirror of signals._check_std_filter:
    # edge_ratio = expected capturable move / round-trip cost, in spread units.
    #   capture = f*|z|*std            (when a sigma-fraction target is set)
    #           = (|z|-exit)*std       (else: full z-reversion distance)
    # Required multiple is floored at the exit cost-floor multiple so entry can
    # never be looser than the exit (the E >= cost_mult coherence condition).
    cost_in_price = (round_trip_bps / 10000.0) * spot_arr
    z_abs = np.abs(zscore.to_numpy())
    std_np = rolling_std.to_numpy()
    if (cfg.profit_target_sigma_frac or 0.0) > 0:
        capture = cfg.profit_target_sigma_frac * z_abs * std_np
    else:
        capture = np.maximum(z_abs - cfg.exit_threshold, 0.0) * std_np
    edge_ratio = capture / np.where(cost_in_price > 0, cost_in_price, np.nan)
    required = max(cfg.min_std_multiple, cfg.profit_target_min_cost_mult or 0.0)
    std_pass = (~cfg.std_filter_enabled) | (edge_ratio >= required)

    # Pre-compute Hurst on a sliding window if enabled (expensive — skip if disabled)
    hurst_pass = np.ones(len(merged), dtype=bool)
    if cfg.hurst_enabled:
        hurst_pass = np.ones(len(merged), dtype=bool)  # default true if window short
        for i in range(cfg.lookback, len(merged)):
            window = spread[i - cfg.hurst_window:i]
            h = _hurst_rs(window)
            hurst_pass[i] = h < cfg.hurst_threshold

    for i in range(cfg.lookback, len(merged)):
        z = zscore.iat[i]
        if np.isnan(z):
            continue

        if position is None:
            if not (std_pass[i] and hurst_pass[i]):
                continue
            # Stop-loss guard: don't enter if z already past stop-loss threshold
            if z >= cfg.entry_threshold and z < cfg.stop_loss_threshold:
                position, entry_idx = "LONG", i
                entry_spread, entry_mean, entry_std = spread[i], rolling_mean.iat[i], rolling_std.iat[i]
            elif z <= -cfg.entry_threshold and z > -cfg.stop_loss_threshold:
                position, entry_idx = "SHORT", i
                entry_spread, entry_mean, entry_std = spread[i], rolling_mean.iat[i], rolling_std.iat[i]
            continue

        # In a position — check exits.
        exit_now = False
        reason = ""
        # Exit z computed against ENTRY-time mean/std (so mean reversion is to the regime we entered)
        exit_z = (spread[i] - entry_mean) / entry_std if entry_std > 0 else z

        if position == "LONG":
            if exit_z <= cfg.exit_threshold:
                exit_now, reason = True, "mean_revert"
            elif z >= cfg.stop_loss_threshold:
                exit_now, reason = True, "stop_loss"
        elif position == "SHORT":
            if exit_z >= -cfg.exit_threshold:
                exit_now, reason = True, "mean_revert"
            elif z <= -cfg.stop_loss_threshold:
                exit_now, reason = True, "stop_loss"

        if exit_now:
            trades.append(_finalize_trade(
                pair_id, position, entry_idx, i, ts, spot_arr, fut_arr, cfg, reason))
            position = None
            entry_idx = None

    if position is not None and entry_idx is not None:
        # Force-close at end of data so P&L isn't biased by held-open positions
        trades.append(_finalize_trade(
            pair_id, position, entry_idx, len(merged) - 1, ts, spot_arr, fut_arr, cfg, "data_end"))

    if not trades:
        return pd.DataFrame()

    return pd.DataFrame([t.__dict__ for t in trades])


def _finalize_trade(pair_id, position, entry_idx, exit_idx, ts,
                    spot_arr, fut_arr, cfg, reason) -> Trade:
    """Compute fees/slippage/PnL exactly as live order_executor would."""
    entry_spot, entry_fut = spot_arr[entry_idx], fut_arr[entry_idx]
    exit_spot,  exit_fut  = spot_arr[exit_idx],  fut_arr[exit_idx]

    qty = cfg.notional_per_leg_usd / entry_spot       # base-asset units, same on both legs

    # Spread payoff per unit of base asset
    # LONG  = bet spread will fall = BUY spot + SELL futures at entry, reverse at exit
    # SHORT = bet spread will rise = SELL spot + BUY futures at entry, reverse at exit
    if position == "LONG":
        spot_pnl = (exit_spot - entry_spot) * qty   # bought spot
        fut_pnl  = (entry_fut - exit_fut)  * qty    # sold futures
    else:
        spot_pnl = (entry_spot - exit_spot) * qty   # sold spot
        fut_pnl  = (exit_fut  - entry_fut) * qty    # bought futures
    gross = spot_pnl + fut_pnl

    # Fees: bps of notional at fill price, all 4 legs
    spot_entry_bps = cfg.spot_maker_bps if cfg.entry_mode == "LIMIT" else cfg.spot_taker_bps
    fut_entry_bps  = cfg.fut_maker_bps  if cfg.entry_mode == "LIMIT" else cfg.fut_taker_bps
    spot_exit_bps  = cfg.spot_maker_bps if cfg.exit_mode  == "LIMIT" else cfg.spot_taker_bps
    fut_exit_bps   = cfg.fut_maker_bps  if cfg.exit_mode  == "LIMIT" else cfg.fut_taker_bps
    fees = (
        spot_entry_bps / 10000 * entry_spot * qty +
        fut_entry_bps  / 10000 * entry_fut  * qty +
        spot_exit_bps  / 10000 * exit_spot  * qty +
        fut_exit_bps   / 10000 * exit_fut   * qty
    )
    slippage = cfg.slippage_bps_per_leg / 10000 * (
        entry_spot + entry_fut + exit_spot + exit_fut) * qty

    return Trade(
        pair_id=pair_id, signal_type=position,
        entry_ts=ts.iloc[entry_idx], exit_ts=ts.iloc[exit_idx],
        entry_spot=entry_spot, entry_futures=entry_fut,
        exit_spot=exit_spot, exit_futures=exit_fut,
        notional_usd=cfg.notional_per_leg_usd,
        gross_pnl=gross, fees=fees, slippage=slippage,
        net_pnl=gross - fees - slippage,
        exit_reason=reason,
        bars_held=exit_idx - entry_idx,
    )


# ---- simple per-pair metrics ------------------------------------------------

def summarize_trades(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0, "net_pnl": 0.0, "win_rate": 0.0,
                "avg_net": 0.0, "max_drawdown": 0.0, "sharpe": 0.0}
    cum = trades["net_pnl"].cumsum()
    running_max = cum.cummax()
    dd = (cum - running_max).min()
    daily_pnl = trades.set_index("exit_ts")["net_pnl"].resample("1D").sum()
    sharpe = (
        np.sqrt(365) * daily_pnl.mean() / daily_pnl.std()
        if daily_pnl.std() > 0 else 0.0
    )
    return {
        "trades": len(trades),
        "wins": int((trades["net_pnl"] > 0).sum()),
        "losses": int((trades["net_pnl"] <= 0).sum()),
        "win_rate": float((trades["net_pnl"] > 0).mean()),
        "net_pnl": float(trades["net_pnl"].sum()),
        "gross_pnl": float(trades["gross_pnl"].sum()),
        "fees": float(trades["fees"].sum()),
        "slippage": float(trades["slippage"].sum()),
        "avg_net": float(trades["net_pnl"].mean()),
        "max_drawdown": float(dd),
        "sharpe": float(sharpe),
        "stop_losses": int((trades["exit_reason"] == "stop_loss").sum()),
        "avg_bars_held": float(trades["bars_held"].mean()),
    }
