"""
Pure, dependency-free analytics helpers for the /analysis page.

Read-only post-hoc math over closed-trade records. Nothing here touches
signals, orders, or engine state — it only summarises history, so it can be
unit-tested in isolation and can never affect live trading.
"""
from typing import Dict, List, Optional


def max_drawdown(pnls: List[float]) -> Dict[str, float]:
    """Peak-to-trough decline of the cumulative realised-P&L curve.

    Args:
        pnls: realised P&L per CLOSED trade, in chronological (exit) order.

    The equity curve starts at 0 (pure cumulative P&L). Max drawdown is the
    largest drop from any running peak to a later trough — the classic
    definition, expressed here in dollars so no capital figure is required.
    Convert to a percentage outside this function by dividing by the capital
    base (see ``drawdown_pct``), keeping the "% of total capital" choice in one
    place instead of baking a denominator into the core math.

    Returns a dict with:
      max_drawdown_usd     worst peak→trough decline (>= 0)
      current_drawdown_usd decline from the all-time peak to the latest equity (>= 0)
      peak_equity_usd      highest cumulative P&L reached
      final_equity_usd     latest cumulative P&L (== sum of pnls)
      n                    number of trades counted
    """
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    n = 0
    for p in pnls:
        equity += (p or 0.0)
        n += 1
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    current_dd = peak - equity
    return {
        "max_drawdown_usd": max_dd,
        "current_drawdown_usd": current_dd,
        "peak_equity_usd": peak,
        "final_equity_usd": equity,
        "n": n,
    }


def drawdown_pct(dd_usd: float, capital_base: Optional[float]) -> Optional[float]:
    """A drawdown dollar amount as a percentage of total capital.

    capital_base is the account's total equity — the honest "% of total
    capital" denominator. Returns None when capital isn't known (e.g. the
    exchange balance couldn't be fetched) so the caller can show "—" rather
    than a divide-by-zero or a misleading 0.
    """
    if not capital_base or capital_base <= 0:
        return None
    return dd_usd / capital_base * 100.0


def trade_excursions(trade, equity: Optional[float] = None) -> Dict[str, Optional[float]]:
    """Per-trade adverse/favourable excursion, from the lifecycle extremes
    already recorded at close (trough_net_usd / peak_net_usd, net USD after
    fees).

    equity, when given, adds mae_eq_pct = MAE$ as a % of ACCOUNT equity — each
    trade's worst dip measured against the whole account (vs mae_pct, which is
    against that trade's own locked capital). None when equity isn't known.

    MAE (maximum adverse excursion) = how far the trade went AGAINST you: the
    magnitude of the worst net-USD point, or 0 if it never went negative.
    MFE (maximum favourable excursion) = the best net-USD point reached.
    'recovered' flags a trade that dipped underwater yet still closed positive
    — the "it came back" case that this view exists to surface.
    """
    cap = getattr(trade, "capital_locked_usd", 0.0) or 0.0
    trough = getattr(trade, "trough_net_usd", 0.0) or 0.0
    peak = getattr(trade, "peak_net_usd", 0.0) or 0.0
    pnl = getattr(trade, "pnl_usd", 0.0) or 0.0
    mae_usd = -trough if trough < 0 else 0.0
    # One-word verdict that synthesises the MAE/MFE/final path into the outcome
    # that actually matters — far more legible than a yes/no "recovered":
    #   clean         won without ever going underwater
    #   recovered     dipped underwater but still closed green (holding paid off)
    #   round_tripped reached unrealised profit, then gave it ALL back (the leak
    #                 the trailing-stop / TP tuning is chasing)
    #   loss          never in profit, closed red (trend ran it over)
    if pnl > 0 and trough >= 0:
        verdict = "clean"
    elif pnl > 0:
        verdict = "recovered"
    elif peak > 0:
        verdict = "round_tripped"
    else:
        verdict = "loss"
    return {
        "mae_usd": mae_usd,
        "mae_pct": (mae_usd / cap * 100.0) if cap > 0 else None,
        "mae_eq_pct": (mae_usd / equity * 100.0) if (equity and equity > 0) else None,
        "mfe_usd": peak if peak > 0 else 0.0,
        "trough_net_usd": trough,
        "peak_net_usd": peak,
        "pnl_usd": pnl,
        "capital_locked_usd": cap,
        "recovered": bool(trough < 0 and pnl > 0),
        "verdict": verdict,
    }
