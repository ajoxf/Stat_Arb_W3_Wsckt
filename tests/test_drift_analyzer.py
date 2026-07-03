"""
Unit tests for core/drift_analyzer.py.

Proves the intraday drift detector separates the two regimes that matter:
  - OU mean-reverting spread  -> RANGING  (safe to trade)
  - drifting/trending spread  -> TRENDING (halt the day)
  - pure random walk          -> never a false halt
and that DailyDriftMonitor flags a trend that only emerges in the afternoon,
without flagging a calm ranging day. Synthetic series are shaped like the real
ETH/BTC spread (level ~ -2700, tick noise ~ 18); seeds are fixed for determinism.
"""
import numpy as np
import pytest

from core.drift_analyzer import analyze, DailyDriftMonitor

MU, SIGMA, N = -2700.0, 18.0, 240


def _ou(rng, theta=0.08, n=N):
    x = np.empty(n); x[0] = MU
    for t in range(1, n):
        x[t] = x[t - 1] + theta * (MU - x[t - 1]) + rng.normal(0, SIGMA)
    return x


def _trend(rng, drift=5.0, n=N):
    return MU + np.cumsum(rng.normal(drift, SIGMA, n))


def _rw(rng, n=N):
    return MU + np.cumsum(rng.normal(0, SIGMA, n))


def _rate(gen, accept, trials=40):
    rng = np.random.default_rng(12345)
    return sum(1 for _ in range(trials) if analyze(gen(rng)).state in accept) / trials


# ── static classification ─────────────────────────────────────────────────────

def test_mean_reverting_reads_ranging():
    assert _rate(_ou, {"RANGING"}) >= 0.90


def test_clear_trend_reads_trending():
    assert _rate(_trend, {"TRENDING"}) >= 0.75


def test_random_walk_never_false_halts():
    # a driftless walk must not be called TRENDING (would cause false day-halts)
    assert _rate(_rw, {"RANGING", "NEUTRAL"}) >= 0.90


def test_efficiency_ratio_ordering():
    rng = np.random.default_rng(0)
    assert analyze(_trend(rng)).efficiency_ratio > analyze(_ou(rng)).efficiency_ratio


# ── daily monitor ─────────────────────────────────────────────────────────────

def _run_day(day, anchor=None, warm=60, window=90, persist=3):
    mon = DailyDriftMonitor(min_samples=warm, halt_persistence=persist, assess_window=window)
    mon.reset_day()
    if anchor is not None:
        mon.set_anchor(anchor)
    flagged = None
    for i, s in enumerate(day):
        mon.update(s)
        if i >= warm:
            mon.assess()
            if mon.should_halt() and flagged is None:
                flagged = i
    return mon.should_halt(), flagged


def test_monitor_does_not_halt_ranging_day():
    halt, _ = _run_day(_ou(np.random.default_rng(7)))
    assert halt is False


def test_monitor_halts_trending_day():
    halt, flagged = _run_day(_trend(np.random.default_rng(7)))
    assert halt is True
    assert flagged is not None


def test_monitor_flags_afternoon_trend_only():
    """Calm morning then trending afternoon: quiet through the morning, flags
    only once the afternoon trend establishes (anchored to the morning level)."""
    rng = np.random.default_rng(3)
    morning = _ou(rng, n=120)
    afternoon = morning[-1] + np.cumsum(rng.normal(5.0, SIGMA, 120))
    day = np.concatenate([morning, afternoon])
    halt, flagged = _run_day(day, anchor=float(np.mean(morning[:60])))
    assert halt is True
    assert flagged >= 120          # not flagged during the calm morning


def test_monitor_warmup_returns_none():
    mon = DailyDriftMonitor(min_samples=60)
    mon.reset_day()
    for s in _ou(np.random.default_rng(1), n=30):
        mon.update(s)
    assert mon.assess() is None    # still warming up
    assert mon.should_halt() is False
