"""
Tests for the fill-accounting package: lattice co-sizing, per-leg P&L from
actual fills, and the contract-flooring dust guard.

The per-leg P&L cases are keyed to REAL OKX fills from 2026-07-07 morning,
where the dashboard mismatched the exchange:
  SHORT 06:50->07:39  ETH 1.00 @ 1778.04->1770.36 (+$7.68)
                      BTC 0.02 @ 63288.2->63063.8 (-$4.488)   gross +$3.192
  LONG  07:40->07:46  ETH 1.00 @ 1770.34->1773.01 (+$2.67)
                      BTC 0.02 @ 63089.3->63129.4 (-$0.802)   gross +$1.868
The engine had modeled ~+$1.67 / ~+$1.57 from requested (unrounded) sizes.
"""
from types import SimpleNamespace

import pytest

from adapters.okx_adapter import floor_contracts
from core.trading_engine import TradingEngine, lattice_leg_sizes, per_leg_gross_pnl
from models import Trade, TradingConfig

BETA = 37.22


# ── contract flooring dust guard ──────────────────────────────────────────────

def test_floor_contracts_rescues_float_dust():
    # 0.03 / 0.01 = 2.9999999999999996 — must be 3 contracts, not 2.
    assert floor_contracts(0.03 / 0.01) == 3
    assert floor_contracts(1.1 / 0.1) == 11


def test_floor_contracts_never_rounds_up():
    assert floor_contracts(2.69) == 2
    assert floor_contracts(2.9989) == 2


# ── lattice co-sizing ─────────────────────────────────────────────────────────

def test_lattice_picks_balanced_combo_at_small_size():
    # The real morning case: ideal 10.08 ETH-ct / 2.71 BTC-ct. Independent
    # flooring gives 10/2 = ratio 50 (34% off beta); the lattice must pick
    # 11/3 = 36.67 (1.5% off), within the +12% notional budget.
    res = lattice_leg_sizes(1.0079, 0.02708, BETA, 0.1, 0.01, 1778.04, 63288.2)
    assert res is not None
    sq, fq, a_ct, b_ct = res
    assert (a_ct, b_ct) == (11, 3)
    assert sq == pytest.approx(1.1)
    assert fq == pytest.approx(0.03)
    assert abs(sq / fq - BETA) / BETA < 0.02


def test_lattice_keeps_floor_when_already_balanced():
    # At larger size the plain floor is near-perfect; the lattice must not
    # make things worse or blow the budget.
    res = lattice_leg_sizes(10.05, 0.271, 10.05 / 0.271, 0.1, 0.01, 1778.0, 63288.0)
    assert res is not None
    sq, fq, _, _ = res
    beta = 10.05 / 0.271
    assert abs(sq / fq - beta) / beta < 0.005


def test_lattice_returns_none_when_min_contract_breaks_budget():
    # Futures ideal is 0.1 contract; the forced minimum of 1 contract would be
    # 10x the requested size — no candidate fits the budget tolerance.
    res = lattice_leg_sizes(0.05, 0.001, 50.0, 0.1, 0.01, 1778.0, 63288.0)
    assert res is None


def test_lattice_ignores_non_contract_instruments():
    assert lattice_leg_sizes(1.0, 0.027, BETA, 0.0, 0.01, 1778.0, 63288.0) is None


def test_lattice_spot_size_must_reach_executor_regression():
    """Regression for the 2026-07-07 09:42 live incident: the lattice chose
    spot 1.1 ETH (11 ct), but the executor re-derived its target as
    quantity × beta = 1.0674, placed floor(10.674) = 10 contracts, and the
    fill-ratio guard then rejected a FULLY FILLED entry at 93.7% < 95% —
    orphaning both legs. The lattice spot size must be carried on
    trade.spot_qty so the target and the placed size agree exactly."""
    beta = 35.58
    res = lattice_leg_sizes(1.013334, 0.028480, beta, 0.1, 0.01, 1776.95, 63224.4)
    assert res is not None
    lattice_spot, lattice_fut = res[0], res[1]
    assert lattice_spot == pytest.approx(1.1)

    # Correct wiring: target == lattice spot -> full fill is exactly 100%.
    placed_ct = floor_contracts(lattice_spot / 0.1)
    assert placed_ct == 11
    filled_base = placed_ct * 0.1
    assert filled_base / lattice_spot >= 0.999

    # The buggy wiring (target re-derived from beta) rejects its own fill.
    buggy_target = lattice_fut * beta                      # 1.0674
    buggy_ct = floor_contracts(buggy_target / 0.1)         # 10
    assert (buggy_ct * 0.1) / buggy_target < 0.95          # the 93.7% reject


# ── per-leg gross P&L (matches OKX to the cent) ──────────────────────────────

def test_per_leg_pnl_matches_okx_short_trade():
    gross = per_leg_gross_pnl("SHORT", 1.0, 0.02,
                              1778.04, 63288.2, 1770.36, 63063.8)
    assert gross == pytest.approx(3.192, abs=1e-9)


def test_per_leg_pnl_matches_okx_long_trade():
    gross = per_leg_gross_pnl("LONG", 1.0, 0.02,
                              1770.34, 63089.3, 1773.01, 63129.4)
    assert gross == pytest.approx(1.868, abs=1e-9)


def test_per_leg_pnl_equals_spread_formula_when_ratio_is_beta():
    # With spot_qty == beta x fut_qty the per-leg form must be identical to
    # the legacy spread_change x quantity formula.
    qty = 0.054
    spot_qty = BETA * qty
    e_s, e_f, x_s, x_f = 1778.04, 63288.2, 1770.36, 63063.8
    per_leg = per_leg_gross_pnl("SHORT", spot_qty, qty, e_s, e_f, x_s, x_f)
    entry_spread = e_f - BETA * e_s
    exit_spread = x_f - BETA * x_s
    legacy = (exit_spread - entry_spread) * qty
    assert per_leg == pytest.approx(legacy, abs=1e-9)


# ── engine live P&L acts on the recorded real position ───────────────────────

def _engine_with_trade(spot_qty, fut_qty, cur_spot, cur_fut):
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = TradingConfig(
        hedge_ratio=BETA,
        maker_fee_bps=0, taker_fee_bps=0,
        spot_maker_fee_bps=0, spot_taker_fee_bps=0,
        futures_maker_fee_bps=0, futures_taker_fee_bps=0,
    )
    eng._exit_postonly_reject_count = 0
    eng._EXIT_POSTONLY_MARKET_AFTER = 1
    eng.open_trade = Trade(position_type="SHORT",
                           entry_spot_price=1778.04, entry_futures_price=63288.2,
                           quantity=fut_qty, spot_qty=spot_qty)
    eng.spot_tick = SimpleNamespace(mid=cur_spot)
    eng.futures_tick = SimpleNamespace(mid=cur_fut)
    return eng


def test_live_net_pnl_uses_recorded_fills():
    # Real position 1.0 / 0.02 (ratio 50): live P&L must equal OKX's +3.192,
    # not the beta-model number (~+1.67 at the requested 0.0271 BTC).
    eng = _engine_with_trade(1.0, 0.02, 1770.36, 63063.8)
    assert eng._live_net_pnl(eng.open_trade) == pytest.approx(3.192, abs=1e-9)


def test_live_net_pnl_falls_back_to_beta_for_legacy_trades():
    # spot_qty = 0 (legacy row): derive spot leg as beta x quantity.
    eng = _engine_with_trade(0.0, 0.054, 1770.36, 63063.8)
    expected = per_leg_gross_pnl("SHORT", BETA * 0.054, 0.054,
                                 1778.04, 63288.2, 1770.36, 63063.8)
    assert eng._live_net_pnl(eng.open_trade) == pytest.approx(expected, abs=1e-9)
