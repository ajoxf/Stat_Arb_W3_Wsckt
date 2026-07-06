"""
Unit tests for exit_spread_levels — the absolute spread values at which a trade
breaks even, takes profit, and stops out.

The invariant under test (mirrors _live_net_pnl / _check_override_exit):
    net(S)  = d × (S − entry) × qty − fees      d = −1 LONG / +1 SHORT
    BE:  net(S) = 0        TP:  net(S) = target        SL:  gross(S) = −stop
Numbers cross-checked against live trade #57 (SHORT, 2026-07-03): entry spread
−2723.5673, qty 0.054026, stop $6.68 → stop level ≈ −2847.21, where the
DOLLAR_STOP actually fired (gross −$6.73 just past the line).
"""
import pytest

from core.trading_engine import exit_spread_levels


def net_at(S, entry, qty, position_type, fees):
    d = -1.0 if position_type == "LONG" else 1.0
    return d * (S - entry) * qty - fees


def gross_at(S, entry, qty, position_type):
    d = -1.0 if position_type == "LONG" else 1.0
    return d * (S - entry) * qty


# ── real-trade cross-check (#57) ─────────────────────────────────────────────

def test_short_stop_level_matches_trade_57():
    lv = exit_spread_levels(entry_spread=-2723.5673, quantity=0.054026,
                            position_type="SHORT", fees_usd=1.10,
                            target_usd=2.74, stop_usd=6.68)
    assert lv['stop'] == pytest.approx(-2847.21, abs=0.01)
    # the trade actually stopped at gross −6.73 → spread ≈ −2848.2: past the line
    assert -2848.2 < lv['stop']


# ── coherence: the levels solve the exact exit equations ─────────────────────

@pytest.mark.parametrize("ptype", ["LONG", "SHORT"])
def test_levels_solve_exit_equations(ptype):
    entry, qty, fees, target, stop = -2790.24, 0.027775, 1.35, 2.74, 3.43
    lv = exit_spread_levels(entry, qty, ptype, fees, target, stop)
    assert net_at(lv['break_even'], entry, qty, ptype, fees) == pytest.approx(0.0, abs=1e-9)
    assert net_at(lv['take_profit'], entry, qty, ptype, fees) == pytest.approx(target, abs=1e-9)
    assert gross_at(lv['stop'], entry, qty, ptype) == pytest.approx(-stop, abs=1e-9)


# ── orientation ──────────────────────────────────────────────────────────────

def test_long_profits_when_spread_falls():
    lv = exit_spread_levels(-2790.0, 0.05, "LONG", 1.0, 3.0, 4.0)
    assert lv['favorable'] == 'down'
    assert lv['break_even'] < -2790.0      # must fall past fees to break even
    assert lv['take_profit'] < lv['break_even']
    assert lv['stop'] > -2790.0            # rising spread stops a LONG out


def test_short_profits_when_spread_rises():
    lv = exit_spread_levels(-2723.0, 0.05, "SHORT", 1.0, 3.0, 4.0)
    assert lv['favorable'] == 'up'
    assert lv['break_even'] > -2723.0
    assert lv['take_profit'] > lv['break_even']
    assert lv['stop'] < -2723.0


# ── disabled overrides & guards ──────────────────────────────────────────────

def test_disabled_target_and_stop_are_none():
    lv = exit_spread_levels(-2700.0, 0.05, "LONG", 1.0, 0.0, 0.0)
    assert lv['take_profit'] is None
    assert lv['stop'] is None
    assert lv['break_even'] is not None    # BE always exists (fees always real)


def test_unusable_quantity_returns_none():
    assert exit_spread_levels(-2700.0, 0.0, "LONG", 1.0, 2.0, 3.0) is None
    assert exit_spread_levels(-2700.0, -1.0, "SHORT", 1.0, 2.0, 3.0) is None
