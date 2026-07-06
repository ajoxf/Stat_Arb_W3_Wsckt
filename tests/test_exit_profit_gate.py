"""
Unit tests for the exit profit gate (_signal_exit_gated).

The gate suppresses the signal-generator's reversion EXIT while the trade is
below break-even (net of ALL fees), so a drifted rolling z can no longer close
a trade at a loss and call it a profit-take. It must never touch STOP_LOSS,
override exits (PROFIT_TARGET / MAX_HOLD / DOLLAR_STOP), or fire when disabled.

Uses the real TradingEngine._signal_exit_gated + _live_net_pnl with a real
TradingConfig (real fee schedule) and controlled tick mids.
"""
from types import SimpleNamespace

from core.trading_engine import TradingEngine
from models import Trade, TradingConfig

BETA = 37.22
ENTRY_SPOT = 1740.0
ENTRY_FUT = 62000.0


def make_engine(gate=0.0, gate_pct=0.0, position="LONG",
                spot_mid=ENTRY_SPOT, fut_mid=ENTRY_FUT):
    eng = TradingEngine.__new__(TradingEngine)          # bypass heavy __init__
    eng.config = TradingConfig(hedge_ratio=BETA, exit_profit_gate_usd=gate,
                               exit_profit_gate_pct=gate_pct)
    eng._exit_postonly_reject_count = 0
    eng._EXIT_POSTONLY_MARKET_AFTER = 1
    eng._override_exit_reason = None
    eng._exit_gate_last_log = None
    eng.open_trade = Trade(
        position_type=position,
        entry_spot_price=ENTRY_SPOT, entry_futures_price=ENTRY_FUT,
        entry_zscore=3.0, entry_spread_std=64.0, quantity=0.054,
    )
    eng.spot_tick = SimpleNamespace(mid=spot_mid)
    eng.futures_tick = SimpleNamespace(mid=fut_mid)
    return eng


def exit_signal(z=0.30):
    return SimpleNamespace(signal_type="EXIT", zscore=z)


def test_gate_holds_reversion_exit_below_break_even():
    # At entry mids the trade is exactly in the fee hole: net = -fees < 0.
    eng = make_engine(gate=0.0)
    assert eng._live_net_pnl(eng.open_trade) < 0
    assert eng._signal_exit_gated(exit_signal()) is True


def test_gate_releases_once_net_clears_floor():
    # LONG profits when the spread falls: raise the spot mid so
    # spread = fut - beta*spot drops far past the fee hole.
    eng = make_engine(gate=0.0, position="LONG", spot_mid=ENTRY_SPOT + 8.0)
    assert eng._live_net_pnl(eng.open_trade) > 0
    assert eng._signal_exit_gated(exit_signal()) is False


def test_gate_respects_positive_floor():
    # A floor just above the current (positive) net holds; just below releases.
    eng = make_engine(gate=0.0, position="LONG", spot_mid=ENTRY_SPOT + 8.0)
    net = eng._live_net_pnl(eng.open_trade)
    assert net > 0
    eng.config.exit_profit_gate_usd = net + 1.0
    assert eng._signal_exit_gated(exit_signal()) is True
    eng.config.exit_profit_gate_usd = max(net - 1.0, 0.0)
    eng._exit_gate_last_log = None
    assert eng._signal_exit_gated(exit_signal()) is False


def test_stop_loss_never_gated():
    eng = make_engine(gate=0.0)   # deep in the fee hole
    sig = SimpleNamespace(signal_type="STOP_LOSS", zscore=4.2)
    assert eng._signal_exit_gated(sig) is False


def test_override_exits_never_gated():
    eng = make_engine(gate=0.0)
    eng._override_exit_reason = "PROFIT_TARGET"
    assert eng._signal_exit_gated(exit_signal()) is False


def test_negative_setting_disables_gate():
    eng = make_engine(gate=-1.0)
    assert eng._live_net_pnl(eng.open_trade) < 0
    assert eng._signal_exit_gated(exit_signal()) is False


def test_missing_ticks_fail_open():
    # If P&L can't be priced, never block the exit.
    eng = make_engine(gate=0.0)
    eng.spot_tick = None
    assert eng._signal_exit_gated(exit_signal()) is False


def test_short_direction_symmetry():
    # SHORT profits when the spread rises: raise the futures mid.
    eng = make_engine(gate=0.0, position="SHORT", fut_mid=ENTRY_FUT + 300.0)
    assert eng._live_net_pnl(eng.open_trade) > 0
    assert eng._signal_exit_gated(exit_signal(z=-0.3)) is False
    # ...and is held while still in the fee hole at entry mids.
    eng2 = make_engine(gate=0.0, position="SHORT")
    assert eng2._signal_exit_gated(exit_signal(z=-0.3)) is True


# ── %-of-capital form ─────────────────────────────────────────────────────────

def test_pct_floor_resolves_from_capital_at_risk():
    eng = make_engine(gate_pct=0.5)
    capital = eng._capital_at_risk(eng.open_trade)
    assert eng._exit_gate_floor(eng.open_trade) == pytest_approx(0.005 * capital)


def test_pct_form_holds_and_releases_around_its_floor():
    # Profitable trade; set the % so the floor lands just above net -> held,
    # then just below net -> released. Robust to the fee schedule.
    eng = make_engine(position="LONG", spot_mid=ENTRY_SPOT + 8.0)
    net = eng._live_net_pnl(eng.open_trade)
    capital = eng._capital_at_risk(eng.open_trade)
    assert net > 0
    eng.config.exit_profit_gate_pct = (net + 1.0) / capital * 100.0
    assert eng._signal_exit_gated(exit_signal()) is True
    eng.config.exit_profit_gate_pct = max((net - 1.0) / capital * 100.0, 1e-9)
    eng._exit_gate_last_log = None
    assert eng._signal_exit_gated(exit_signal()) is False


def test_pct_overrides_disabled_usd():
    # usd = -1 alone disables the gate, but pct > 0 re-arms it (pct wins).
    eng = make_engine(gate=-1.0, gate_pct=0.5)   # in the fee hole at entry mids
    assert eng._signal_exit_gated(exit_signal()) is True


def test_gate_release_level_in_spread_levels():
    from core.trading_engine import exit_spread_levels
    # gate 0 -> release == BE; gate > 0 -> release sits past BE, at net == gate.
    lv0 = exit_spread_levels(-2723.57, 0.054, "SHORT", 1.10, 2.74, 6.68, gate_usd=0.0)
    assert lv0['gate_release'] == pytest_approx(lv0['break_even'])
    lv = exit_spread_levels(-2723.57, 0.054, "SHORT", 1.10, 2.74, 6.68, gate_usd=2.0)
    d = 1.0  # SHORT favorable = up
    net_at_release = d * (lv['gate_release'] - (-2723.57)) * 0.054 - 1.10
    assert net_at_release == pytest_approx(2.0)
    assert lv['gate_release'] > lv['break_even']


def pytest_approx(x):
    import pytest
    return pytest.approx(x, abs=1e-9)
