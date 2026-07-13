"""
Tests for the profit-target precedence chain in _effective_exit_targets:
sigma-fraction (volatility-aware) > %-of-capital ("bank the win", fires on
P&L alone — no z condition) > fixed USD. The cost floor applies to all forms.
"""
from types import SimpleNamespace

import pytest

from core.trading_engine import TradingEngine
from models import Trade, TradingConfig

BETA = 35.58


def make_engine(**cfg):
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = TradingConfig(hedge_ratio=BETA, **cfg)
    eng.spot_tick = None
    eng.futures_tick = None
    eng._exit_postonly_reject_count = 0
    eng._EXIT_POSTONLY_MARKET_AFTER = 1
    eng.signal_generator = SimpleNamespace(current_half_life=float("inf"))
    trade = Trade(position_type="SHORT",
                  entry_spot_price=1777.47, entry_futures_price=63194.90,
                  entry_zscore=-3.54, entry_spread_std=25.0,
                  quantity=0.03, spot_qty=1.1)
    return eng, trade


def test_capital_pct_target_resolves_from_capital_at_risk():
    eng, tr = make_engine(profit_target_capital_pct=0.5)
    t = eng._effective_exit_targets(tr)
    assert t['target_usd'] == pytest.approx(0.005 * eng._capital_at_risk(tr))
    assert t['target_usd'] > 0


def test_sigma_fraction_takes_precedence_over_capital_pct():
    eng, tr = make_engine(profit_target_sigma_frac=0.5,
                          profit_target_capital_pct=0.5)
    t = eng._effective_exit_targets(tr)
    assert t['target_usd'] == pytest.approx(0.5 * 3.54 * 25.0 * 0.03)


def test_usd_fallback_when_both_pct_forms_zero():
    eng, tr = make_engine(profit_target_usd=3.0)
    t = eng._effective_exit_targets(tr)
    assert t['target_usd'] == pytest.approx(3.0)


def test_cost_floor_applies_to_capital_pct_form():
    # A deliberately tiny %-target must be raised to the cost floor.
    eng, tr = make_engine(profit_target_capital_pct=0.01,
                          profit_target_min_cost_mult=1.5)
    t = eng._effective_exit_targets(tr)
    assert t['round_trip_cost'] > 0
    assert t['target_usd'] >= 1.5 * t['round_trip_cost']


def test_all_forms_zero_means_no_target():
    eng, tr = make_engine()
    t = eng._effective_exit_targets(tr)
    assert t['target_usd'] == 0.0


# ── PROFIT_TARGET must fire even with a dollar stop armed ─────────────────────
# Regression: PROFIT_TARGET and MAX_HOLD were `elif` branches to `if stop_usd>0`,
# so arming a dollar stop (stop_usd > 0) took the first branch and skipped them —
# a trade sitting above its target never booked (live: +$9.66 stuck vs +$8.97 TP).

def _override_engine(net_pnl, target_usd, stop_usd, position="LONG"):
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = TradingConfig(hedge_ratio=BETA)   # trailing/hurst/velocity all off
    eng._peak_pnl = 0.0
    eng._trough_pnl = 0.0
    eng._peak_at = None
    eng._trough_at = None
    eng._z_seen_min = None
    eng._z_seen_max = None
    eng._override_exit_reason = None
    eng.state = SimpleNamespace(current_position=position)
    eng.open_trade = Trade(position_type=position, entry_spot_price=1770.52,
                           entry_futures_price=62658.5, quantity=0.1, spot_qty=3.5,
                           entry_zscore=4.34)
    eng._effective_exit_targets = lambda tr: {
        "target_usd": target_usd, "stop_usd": stop_usd,
        "max_hold_periods": 0.0, "max_hold_minutes": 0.0, "round_trip_cost": 0.0}
    eng._live_net_pnl = lambda tr: net_pnl
    eng._round_trip_fees = lambda tr, *a, **k: 4.5
    return eng


def _sig(z, spread):
    return SimpleNamespace(zscore=z, spread=spread, spread_mean=194.0,
                           spread_std=25.0, hurst=None, regime="MEAN_REVERTING",
                           signal_type="NONE")


def test_profit_target_fires_with_dollar_stop_armed():
    eng = _override_engine(net_pnl=9.66, target_usd=8.97, stop_usd=40.5)
    out = eng._check_override_exit(_sig(-0.84, 77.6))
    assert out is not None, "PROFIT_TARGET did not fire while a dollar stop was armed"
    assert out.signal_type == "EXIT"
    assert eng._override_exit_reason == "PROFIT_TARGET"


def test_dollar_stop_still_wins_when_both_would_fire():
    # Priority preserved: past the stop, DOLLAR_STOP wins even though the target
    # branch now runs independently.
    eng = _override_engine(net_pnl=-50.0, target_usd=8.97, stop_usd=40.5)
    out = eng._check_override_exit(_sig(-6.0, 600.0))
    assert out is not None
    assert eng._override_exit_reason == "DOLLAR_STOP"
