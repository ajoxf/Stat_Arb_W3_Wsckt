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
