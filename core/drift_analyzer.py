"""
Daily drift analysis — an intraday "is the spread trending or reverting today?"
detector, decoupled from the trading engine so it can be back-tested on history
before it ever gates a live trade.

Motivation
----------
The pair (ETH vs BTC) is cointegrated over long horizons — beta reverts to ~38 —
but on any given *day* the spread can either oscillate around a stable level
(tradeable for mean reversion) or march in one direction (a trend that runs the
strategy over). The z-score always "reverts" mechanically because the rolling
mean chases price, so z-reversion alone can't tell the two regimes apart.

The tell is DRIFT: fix an anchor in the morning (the spread mean once the day's
warm-up data is in) and watch how the spread behaves relative to that FIXED
anchor. If it keeps crossing back through the anchor, it's ranging. If it walks
away and stays on one side making new extremes, it's trending — flag the day.

This module is pure/stateless math (numpy only) plus one small stateful monitor.
No engine imports, no I/O — so verify_drift.py can prove the logic on synthetic
series and drift_backtest.py can replay real spread_history through it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List

import numpy as np


# ── Classification thresholds (calibrate these on real data via the backtest) ──
# trend_score is a 0..1 blend of the individual trendiness signals below.
TREND_SCORE_TRENDING = 0.60   # >= this  -> TRENDING (halt new entries)
TREND_SCORE_RANGING  = 0.40   # <  this  -> RANGING  (ok to trade)
                              # in between -> NEUTRAL (hold state, don't flip)

# Per-metric mapping knobs — the [low, high] band each raw metric is stretched
# across to become a 0..1 "trendiness". low -> 0 (fully ranging), high -> 1.
ER_BAND        = (0.10, 0.35)   # efficiency ratio: net move / path length
ONE_SIDED_BAND = (0.60, 0.92)   # fraction of samples on one side of the anchor
VR_BAND        = (1.00, 2.00)   # variance ratio at VR_LAG
XING_RATE_FULL_TREND = 0.02     # zero-crossings/step at/below this -> fully trend
XING_RATE_FULL_RANGE = 0.12     # at/above this -> fully ranging

VR_LAG = 8   # variance-ratio horizon in samples (Lo–MacKinlay q)


@dataclass
class DriftMetrics:
    """All raw drift measurements for one window + a blended verdict."""
    n: int
    efficiency_ratio: float          # 0..1  (1 = straight-line move)
    zero_crossings: int              # times deviation crossed the anchor
    zero_crossing_rate: float        # crossings / (n-1)
    one_sided_fraction: float        # 0.5..1 (1 = never came back to anchor)
    variance_ratio: float            # <1 revert, ~1 random walk, >1 trend
    ou_theta: float                  # OU mean-reversion speed (>0 = reverting)
    ou_half_life: float              # periods; inf = not mean-reverting
    net_move: float                  # signed anchor-to-last displacement
    trend_score: float               # 0..1 blended trendiness
    state: str                       # "RANGING" | "NEUTRAL" | "TRENDING"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _stretch(x: float, lo: float, hi: float) -> float:
    """Map x from [lo, hi] onto [0, 1], clamped."""
    if hi == lo:
        return 0.0
    return _clip01((x - lo) / (hi - lo))


def efficiency_ratio(series: np.ndarray) -> float:
    """Kaufman efficiency ratio: |net displacement| / total path length.

    1.0 = a perfectly straight line (pure trend). ~0 = lots of back-and-forth
    with little net progress (choppy / mean-reverting). This is the direct
    formalisation of "drift constantly one way vs. round-tripping".
    """
    if len(series) < 2:
        return 0.0
    net = abs(float(series[-1] - series[0]))
    path = float(np.sum(np.abs(np.diff(series))))
    return _clip01(net / path) if path > 0 else 0.0


def zero_crossings(series: np.ndarray, anchor: float) -> int:
    """How many times the series crosses back through the anchor. Frequent
    crossings = the spread keeps returning to the anchor = ranging."""
    d = np.asarray(series, dtype=float) - anchor
    s = np.sign(d)
    s = s[s != 0]                      # ignore exact-touch samples
    if len(s) < 2:
        return 0
    return int(np.sum(s[1:] != s[:-1]))


def one_sided_fraction(series: np.ndarray, anchor: float) -> float:
    """Fraction of samples on whichever side of the anchor is more populated.
    0.5 on a balanced ranging day; -> 1.0 when the spread parks on one side."""
    d = np.asarray(series, dtype=float) - anchor
    above = float(np.sum(d > 0))
    below = float(np.sum(d < 0))
    total = above + below
    if total == 0:
        return 0.5
    return max(above, below) / total


def variance_ratio(series: np.ndarray, q: int = VR_LAG) -> float:
    """Lo–MacKinlay variance ratio at lag q, using overlapping differences.

    VR(q) = Var(x[t]-x[t-q]) / (q * Var(x[t]-x[t-1])).
      < 1  negative autocorrelation  -> mean-reverting
      ~ 1  random walk
      > 1  positive autocorrelation   -> trending / momentum
    Returns 1.0 (random-walk-neutral) when the series is too short to estimate.
    """
    x = np.asarray(series, dtype=float)
    n = len(x)
    if n < q + 2 or q < 2:
        return 1.0
    d1 = np.diff(x)                       # 1-period differences
    var1 = np.var(d1)
    if var1 <= 0:
        return 1.0
    dq = x[q:] - x[:-q]                   # q-period (overlapping) differences
    varq = np.var(dq)
    return float((varq / q) / var1)


def ou_theta_halflife(series: np.ndarray) -> tuple[float, float]:
    """OU mean-reversion speed theta and half-life, matching signals.py exactly.

    d(spread) = theta * (mean - spread) + noise ; theta>0 mean-reverts.
    Returns (theta, half_life_periods). theta<=0 -> (theta, inf) = trending.
    """
    x = np.asarray(series, dtype=float)
    n = len(x)
    if n < 10:
        return 0.0, float('inf')
    lag = x[:-1]
    diff = x[1:] - x[:-1]
    xreg = np.mean(lag) - lag
    denom = float(np.dot(xreg, xreg))
    if denom == 0:
        return 0.0, float('inf')
    theta = float(np.dot(xreg, diff) / denom)
    if theta <= 0:
        return theta, float('inf')
    hl = math.log(2) / theta
    hl = max(1.0, min(hl, float(n)))
    return theta, round(hl, 1)


def analyze(series, anchor: Optional[float] = None) -> DriftMetrics:
    """Compute every drift metric for `series` and blend them into a verdict.

    `anchor` is the fixed reference level (the morning spread mean). When None,
    the window's own mean is used — fine for a standalone/backtest window, but
    the live monitor should pass the FROZEN morning anchor so intraday drift
    away from it stays visible (a re-centred anchor hides the trend).
    """
    x = np.asarray(list(series), dtype=float)
    n = len(x)
    if n < 2:
        return DriftMetrics(n, 0.0, 0, 0.0, 0.5, 1.0, 0.0, float('inf'),
                            0.0, 0.0, "NEUTRAL")

    # Anchor to the EARLY part of the window (the "morning" level), not the full
    # mean — a whole-window mean sits in the middle of a trend and hides it (the
    # series then looks balanced above/below its own mean). The live monitor
    # passes the frozen morning anchor for the same reason.
    if anchor is None:
        a = float(np.mean(x[:max(10, n // 4)]))
    else:
        a = float(anchor)

    er   = efficiency_ratio(x)
    xing = zero_crossings(x, a)
    xrate = xing / (n - 1) if n > 1 else 0.0
    osf  = one_sided_fraction(x, a)
    vr   = variance_ratio(x)
    theta, hl = ou_theta_halflife(x)
    net  = float(x[-1] - a)

    # Each signal -> 0..1 trendiness, then averaged.
    er_t   = _stretch(er, *ER_BAND)
    osf_t  = _stretch(osf, *ONE_SIDED_BAND)
    vr_t   = _stretch(vr, *VR_BAND)
    xing_t = _stretch(XING_RATE_FULL_RANGE - xrate,
                      0.0, XING_RATE_FULL_RANGE - XING_RATE_FULL_TREND)
    # Long half-life relative to the window = weak reversion = trending.
    ou_t   = 1.0 if theta <= 0 else _stretch(hl / n, 0.5, 1.0)

    trend_score = float(np.mean([er_t, osf_t, vr_t, xing_t, ou_t]))

    if trend_score >= TREND_SCORE_TRENDING:
        state = "TRENDING"
    elif trend_score < TREND_SCORE_RANGING:
        state = "RANGING"
    else:
        state = "NEUTRAL"

    return DriftMetrics(
        n=n, efficiency_ratio=round(er, 4), zero_crossings=xing,
        zero_crossing_rate=round(xrate, 4), one_sided_fraction=round(osf, 4),
        variance_ratio=round(vr, 4), ou_theta=round(theta, 6),
        ou_half_life=hl, net_move=round(net, 6),
        trend_score=round(trend_score, 4), state=state,
    )


class DailyDriftMonitor:
    """Stateful, per-day intraday monitor. This is the piece that would plug
    into the engine: reset each morning, feed it spreads, ask should_halt().

    It is deliberately NOT wired to the engine here — build/verify first.
    """

    def __init__(self, min_samples: int = 60, halt_persistence: int = 3,
                 min_range: float = 0.0, assess_window: int = 90):
        self.min_samples = min_samples          # warm-up before any verdict
        self.halt_persistence = halt_persistence  # consecutive TRENDING to halt
        self.min_range = min_range              # ignore dead-flat days (abs units)
        # Assess a TRAILING window (vs the frozen morning anchor), not the whole
        # day — otherwise an afternoon trend gets averaged out by a calm morning
        # and the flag never trips. The anchor stays the morning level so drift
        # away from it is always visible; the window keeps the verdict recent.
        self.assess_window = assess_window
        self.reset_day()

    def reset_day(self, anchor: Optional[float] = None) -> None:
        """Call at the start of each trading day (auto-rearm)."""
        self._buf: List[float] = []
        self._anchor: Optional[float] = anchor
        self._trending_streak = 0
        self._halted = False

    def set_anchor(self, anchor: float) -> None:
        """Freeze the morning anchor once warm-up data is in."""
        self._anchor = float(anchor)

    def update(self, spread: float) -> None:
        self._buf.append(float(spread))
        if self._anchor is None and len(self._buf) >= self.min_samples:
            # auto-anchor to the warm-up mean if the caller didn't set one
            self._anchor = float(np.mean(self._buf))

    def assess(self) -> Optional[DriftMetrics]:
        """Current drift verdict, or None until warm-up completes."""
        if len(self._buf) < self.min_samples:
            return None
        arr = np.asarray(self._buf[-self.assess_window:], dtype=float)
        if self.min_range > 0 and (arr.max() - arr.min()) < self.min_range:
            return None  # too flat to call — not enough movement to be a "trend"
        m = analyze(arr, anchor=self._anchor)
        if m.state == "TRENDING":
            self._trending_streak += 1
        else:
            self._trending_streak = 0
        if self._trending_streak >= self.halt_persistence:
            self._halted = True
        return m

    def should_halt(self) -> bool:
        """True once the day has been judged trending persistently. Sticky for
        the rest of the day — a trending day doesn't get un-flagged on one dip."""
        return self._halted
