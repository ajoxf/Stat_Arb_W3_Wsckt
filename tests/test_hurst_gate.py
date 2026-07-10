"""
Tests for the Hurst/regime entry gate (_hurst_gate_ok).

The bug: a finite half-life ('_hl_confirms_mr') let ANY series through even
when Hurst screamed trend (live: H 0.87 / HL ~250 periods passed, then every
trade stopped out in the trend). The fix keeps the half-life *rescue* for
BORDERLINE Hurst (just over the threshold) but blocks a STRONG trend
(H >= 0.65) regardless of half-life.
"""
from types import SimpleNamespace

from core.signals import SignalGenerator
from models import TradingConfig


def _gen(hurst, half_life, enabled=True, threshold=0.5, lookback=7200):
    g = SignalGenerator.__new__(SignalGenerator)          # bypass heavy __init__
    g.config = TradingConfig(hurst_enabled=enabled, hurst_threshold=threshold,
                             lookback_period=lookback)
    g.current_hurst = hurst
    g.current_half_life = half_life
    g.lookback = lookback
    return g


def test_filter_off_always_passes():
    assert _gen(0.95, 200, enabled=False)._hurst_gate_ok() is True


def test_clearly_mean_reverting_passes():
    # Hurst below threshold — always allowed.
    assert _gen(0.42, float('inf'))._hurst_gate_ok() is True


def test_strong_trend_blocked_even_with_finite_half_life():
    # THE fix: H 0.87 with a finite HL (216-329 live) must be blocked.
    assert _gen(0.87, 250)._hurst_gate_ok() is False
    assert _gen(0.87, 329)._hurst_gate_ok() is False


def test_borderline_trend_rescued_by_finite_half_life():
    # Just over the threshold, below the strong-trend ceiling, finite HL — the
    # HL cross-check still rescues these (the behaviour we want to keep).
    assert _gen(0.55, 250)._hurst_gate_ok() is True


def test_borderline_trend_without_half_life_blocked():
    # Over threshold, no finite half-life to confirm MR -> blocked.
    assert _gen(0.55, float('inf'))._hurst_gate_ok() is False


def test_at_ceiling_is_blocked():
    # Hurst exactly at the 0.65 ceiling is NOT below it -> blocked.
    assert _gen(0.65, 250)._hurst_gate_ok() is False


def test_slow_half_life_does_not_confirm_mr():
    # A half-life longer than lookback*0.5 is not fast mean reversion.
    assert _gen(0.55, 5000, lookback=7200)._hurst_gate_ok() is False


def test_ceiling_never_below_threshold():
    # If the user raises the threshold above 0.65, the ceiling follows it so a
    # trade at threshial-but-below-threshold logic stays coherent (>= threshold
    # with finite HL and hurst < max(0.65, threshold) passes).
    g = _gen(0.68, 250, threshold=0.7)
    # hurst 0.68 < threshold 0.7 -> passes on the first clause (mean-reverting).
    assert g._hurst_gate_ok() is True
    # hurst 0.72 >= threshold 0.7, ceiling = max(0.65,0.7)=0.7, 0.72 !< 0.7 -> blocked
    assert _gen(0.72, 250, threshold=0.7)._hurst_gate_ok() is False
