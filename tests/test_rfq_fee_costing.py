"""
RFQ fee-costing: an RFQ-routed trade is a block-trade TAKER on both legs, but
its fee isn't captured per-leg (RFQ returns a block trade_id, not order_ids), so
fees fall back to the maker/taker estimate. Without an RFQ signal that estimate
assumes a MAKER exit — under-stating cost, setting the profit-target floor too
low, and letting a marginally-negative RFQ exit book as a phantom profit.

_trade_routes_via_rfq flags trades whose per-leg notional crosses the RFQ
threshold; the fee estimators then cost both legs at taker. Critically it is a
strict no-op when RFQ is disabled (rfq_notional_threshold_usd == 0, the default)
or the position is below threshold.
"""
from types import SimpleNamespace

import pytest

from core.trading_engine import TradingEngine
from models import Trade, TradingConfig

BETA = 35.58
# Per-leg notional for the trade below: spot 1.1 * 1777.47 ≈ $1955, fut
# 0.03 * 63194.90 ≈ $1896 → max ≈ $1955. Thresholds are chosen around this.
SPOT_PX, FUT_PX = 1777.47, 63194.90
SPOT_QTY, FUT_QTY = 1.1, 0.03
PER_LEG = SPOT_QTY * SPOT_PX  # ≈ 1955.2, the larger leg


def _engine(threshold, **cfg):
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = TradingConfig(hedge_ratio=BETA,
                               rfq_notional_threshold_usd=threshold, **cfg)
    # Mids == entry prices → notional is deterministic.
    eng.spot_tick = SimpleNamespace(mid=SPOT_PX)
    eng.futures_tick = SimpleNamespace(mid=FUT_PX)
    eng._exit_postonly_reject_count = 0
    eng._EXIT_POSTONLY_MARKET_AFTER = 3
    eng.signal_generator = SimpleNamespace(current_half_life=float("inf"))
    trade = Trade(position_type="SHORT",
                  entry_spot_price=SPOT_PX, entry_futures_price=FUT_PX,
                  entry_zscore=-3.54, entry_spread_std=25.0,
                  quantity=FUT_QTY, spot_qty=SPOT_QTY)
    return eng, trade


# ── routing predicate ─────────────────────────────────────────────────────────

def test_routing_false_when_rfq_disabled():
    eng, tr = _engine(threshold=0.0)          # default → RFQ off
    assert eng._trade_routes_via_rfq(tr) is False


def test_routing_false_below_threshold():
    eng, tr = _engine(threshold=PER_LEG + 500.0)
    assert eng._trade_routes_via_rfq(tr) is False


def test_routing_true_at_or_above_threshold():
    eng, tr = _engine(threshold=PER_LEG - 500.0)
    assert eng._trade_routes_via_rfq(tr) is True


# ── cost floor: taker when RFQ-routed, unchanged when not ─────────────────────

def test_rfq_floor_costs_more_than_maker():
    eng_off, tr_off = _engine(threshold=0.0)
    eng_on,  tr_on  = _engine(threshold=PER_LEG - 500.0)
    cost_maker = eng_off._round_trip_cost_usd(tr_off)
    cost_rfq   = eng_on._round_trip_cost_usd(tr_on)
    # Taker (RFQ) > maker on every leg → strictly higher round-trip cost.
    assert cost_rfq > cost_maker > 0


def test_rfq_disabled_is_exact_noop():
    # With RFQ off the floor must equal the pure config-mode (maker) computation
    # byte-for-byte — no behaviour change for order-book-only setups.
    eng, tr = _engine(threshold=0.0)
    slip = eng.config.slippage_bps / 10000.0 * (
        SPOT_QTY * SPOT_PX + FUT_QTY * FUT_PX + SPOT_QTY * SPOT_PX + FUT_QTY * FUT_PX)
    expected = eng._round_trip_fees(tr) + slip     # no override → config modes
    assert eng._round_trip_cost_usd(tr) == pytest.approx(expected)


def test_round_trip_fees_entry_override_raises_cost():
    # Directly: overriding entry+exit to a taker mode must cost >= the maker base.
    eng, tr = _engine(threshold=0.0)
    maker = eng._round_trip_fees(tr)
    taker = eng._round_trip_fees(tr, exit_mode_override="RFQ",
                                 entry_mode_override="RFQ")
    assert taker > maker


# ── the payoff: profit target clears the taker floor for an RFQ trade ─────────

def test_profit_target_clears_taker_floor_for_rfq_trade():
    eng, tr = _engine(threshold=PER_LEG - 500.0,
                      profit_target_capital_pct=0.01,   # tiny → floor-bound
                      profit_target_min_cost_mult=2.0)
    t = eng._effective_exit_targets(tr)
    rt_cost_taker = eng._round_trip_cost_usd(tr)
    assert t['target_usd'] >= 2.0 * rt_cost_taker
    # And that taker floor exceeds what a maker assumption would have set.
    eng_off, tr_off = _engine(threshold=0.0,
                              profit_target_capital_pct=0.01,
                              profit_target_min_cost_mult=2.0)
    assert t['target_usd'] > eng_off._effective_exit_targets(tr_off)['target_usd']


# ── live net P&L uses the same taker basis (decision stays self-consistent) ───

def test_live_net_pnl_lower_under_rfq():
    eng_off, tr_off = _engine(threshold=0.0)
    eng_on,  tr_on  = _engine(threshold=PER_LEG - 500.0)
    net_maker = eng_off._live_net_pnl(tr_off)
    net_rfq   = eng_on._live_net_pnl(tr_on)
    assert net_maker is not None and net_rfq is not None
    # Same gross (mids == entry), higher taker fee → strictly lower net.
    assert net_rfq < net_maker
