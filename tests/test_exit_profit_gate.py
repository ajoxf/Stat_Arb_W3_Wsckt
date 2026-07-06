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


def make_engine(gate=0.0, position="LONG", spot_mid=ENTRY_SPOT, fut_mid=ENTRY_FUT):
    eng = TradingEngine.__new__(TradingEngine)          # bypass heavy __init__
    eng.config = TradingConfig(hedge_ratio=BETA, exit_profit_gate_usd=gate)
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
