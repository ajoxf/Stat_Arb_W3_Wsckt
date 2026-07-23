"""
Tests for the hard max-hold (loss-side time stop).

Unlike the profit-gated MAX_HOLD, this fires REGARDLESS of P&L once a trade has
been open longer than hard_max_hold_minutes — it exists to cut the fat-tail
non-reverters (backtest: the worst trade, −$296 held 26h, becomes ~−$5 if capped
at 90 min). DOLLAR_STOP still wins when both would fire (risk before time).
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from core.trading_engine import TradingEngine
from models import Trade, TradingConfig

BETA = 35.09


def _engine(net_pnl, hard_min, entry_age_min, stop_usd=0.0, target_usd=0.0,
            position="SHORT"):
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = TradingConfig(hedge_ratio=BETA, hard_max_hold_minutes=hard_min)
    eng._peak_pnl = 0.0
    eng._trough_pnl = 0.0
    eng._peak_at = None
    eng._trough_at = None
    eng._z_seen_min = None
    eng._z_seen_max = None
    eng._override_exit_reason = None
    eng._entry_tick_count = None
    eng.state = SimpleNamespace(current_position=position)
    tr = Trade(position_type=position, entry_spot_price=1770.0,
               entry_futures_price=62000.0, quantity=0.1, spot_qty=3.5,
               entry_zscore=-3.6)
    tr.entry_time = datetime.utcnow() - timedelta(minutes=entry_age_min)
    eng.open_trade = tr
    eng._effective_exit_targets = lambda t: {
        "target_usd": target_usd, "stop_usd": stop_usd,
        "max_hold_periods": 0.0, "max_hold_minutes": 0.0, "round_trip_cost": 0.0}
    eng._live_net_pnl = lambda t: net_pnl
    eng._round_trip_fees = lambda t, *a, **k: 2.0
    eng.signal_generator = SimpleNamespace(current_half_life=float("inf"),
                                           total_ticks=0)
    return eng


def _sig(z=-1.0, spread=200.0):
    return SimpleNamespace(zscore=z, spread=spread, spread_mean=180.0,
                           spread_std=25.0, hurst=None, regime="TRENDING",
                           signal_type="NONE")


def test_fires_on_stale_losing_trade():
    # 100 min held, −$5 P&L, 90-min cap, no dollar stop armed.
    eng = _engine(net_pnl=-5.0, hard_min=90, entry_age_min=100)
    out = eng._check_override_exit(_sig())
    assert out is not None, "HARD_MAX_HOLD did not fire on a stale losing trade"
    assert out.signal_type == "EXIT"
    assert eng._override_exit_reason == "HARD_MAX_HOLD"


def test_does_not_fire_before_cap():
    eng = _engine(net_pnl=-5.0, hard_min=90, entry_age_min=30)
    assert eng._check_override_exit(_sig()) is None


def test_fires_regardless_of_pnl_sign():
    # The whole point vs the profit-gated MAX_HOLD: it cuts a LOSER.
    eng = _engine(net_pnl=-40.0, hard_min=60, entry_age_min=90)  # no stop armed
    out = eng._check_override_exit(_sig())
    assert out is not None and eng._override_exit_reason == "HARD_MAX_HOLD"


def test_dollar_stop_takes_precedence():
    # Both would fire; DOLLAR_STOP is checked first (risk before time).
    eng = _engine(net_pnl=-50.0, hard_min=60, entry_age_min=90, stop_usd=40.0)
    out = eng._check_override_exit(_sig())
    assert out is not None
    assert eng._override_exit_reason == "DOLLAR_STOP"


def test_disabled_when_zero():
    eng = _engine(net_pnl=-100.0, hard_min=0.0, entry_age_min=9999)
    assert eng._check_override_exit(_sig()) is None


def test_routed_as_non_urgent_maker_first():
    # Regression guard: a stale bleed must exit maker-first, not straight-to-market.
    import inspect
    import core.trading_engine as te
    src = inspect.getsource(te)
    assert '"HARD_MAX_HOLD"' in src
    assert ('_NON_URGENT_EXITS = ("EXIT", "PROFIT_TARGET", "MAX_HOLD", '
            '"MANUAL_LIMIT", "HARD_MAX_HOLD")') in src
