"""
Tests for the trailing-stop anchor (_trail_peak).

Enabling a trailing stop mid-trade must NOT force-close on a stale pre-enable
high — it re-anchors to the current P&L and only arms once a FRESH peak clears
the floor. Regression: live #103 was switched on at -$31 with a stale +$7.38
high-water and instantly force-closed the loss, labelled TRAILING_STOP.
"""
from types import SimpleNamespace

from core.trading_engine import TradingEngine
from models import Trade, TradingConfig

BETA = 35.28


def _engine(target_usd=8.96, trail_pct=30.0, floor_pct=40.0, position="SHORT"):
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = TradingConfig(hedge_ratio=BETA,
                               trailing_stop_pct=trail_pct,
                               trailing_stop_floor_pct=floor_pct)
    eng._peak_pnl = 0.0
    eng._trail_peak = None
    eng._trough_pnl = 0.0
    eng._peak_at = None
    eng._trough_at = None
    eng._z_seen_min = None
    eng._z_seen_max = None
    eng._override_exit_reason = None
    eng.state = SimpleNamespace(current_position=position)
    eng.open_trade = Trade(position_type=position, entry_spot_price=1765.14,
                           entry_futures_price=62292.70, quantity=0.1, spot_qty=3.5,
                           entry_zscore=-3.97)
    # Stub targets: fixed profit target, NO dollar stop / max hold, so only the
    # trailing stop can fire and the test isolates its behaviour.
    eng._effective_exit_targets = lambda tr: {
        "target_usd": target_usd, "stop_usd": 0.0,
        "max_hold_periods": 0.0, "max_hold_minutes": 0.0, "round_trip_cost": 0.0}
    eng._round_trip_fees = lambda tr, *a, **k: 4.5
    return eng


def _sig(z=-1.0):
    return SimpleNamespace(zscore=z, spread=80.0, spread_mean=194.0,
                           spread_std=25.0, hurst=None, regime="MEAN_REVERTING",
                           signal_type="NONE")


def _tick(eng, net):
    """Run one override-exit check at the given live net P&L."""
    eng._live_net_pnl = lambda tr: net
    return eng._check_override_exit(_sig())


def test_enabling_midtrade_underwater_does_not_force_close():
    eng = _engine()
    eng._peak_pnl = 7.38          # true lifecycle high (recorded), from earlier
    eng._trail_peak = None        # trailing stop freshly enabled THIS tick
    out = _tick(eng, net=-31.0)
    assert out is None, "trailing stop force-closed on a stale pre-enable peak"
    assert eng._trail_peak == -31.0            # re-anchored to current P&L
    assert eng._override_exit_reason is None


def test_arms_and_fires_only_on_a_fresh_peak_after_reanchor():
    eng = _engine()
    eng._peak_pnl = 7.38
    eng._trail_peak = None
    assert _tick(eng, net=-31.0) is None       # anchor -31, inactive (peak <= 0)
    assert _tick(eng, net=-5.0) is None         # climbing, still not positive
    assert _tick(eng, net=8.0) is None          # fresh peak +8, armed, no pullback
    assert eng._trail_peak == 8.0
    out = _tick(eng, net=5.0)                    # 5 < 8 * 0.70 = 5.6 -> fire
    assert out is not None
    assert out.signal_type == "EXIT"
    assert eng._override_exit_reason == "TRAILING_STOP"


def test_does_not_fire_above_trigger():
    eng = _engine()
    eng._trail_peak = None
    assert _tick(eng, net=8.0) is None          # peak +8
    assert _tick(eng, net=6.0) is None          # 6 > 5.6 -> hold
    assert eng._override_exit_reason is None


def test_below_floor_never_arms():
    # Peak of +$2 never reaches the floor (40% of $8.96 = $3.58), so even a hard
    # pullback books nothing via the trailing stop.
    eng = _engine()
    eng._trail_peak = None
    assert _tick(eng, net=2.0) is None
    assert _tick(eng, net=0.1) is None
    assert eng._override_exit_reason is None


def test_fresh_trade_trails_from_open_as_before():
    # Trailing on from the start (peak builds from open): unchanged behaviour —
    # a +$9 peak then a pullback past 30% still fires. target raised to $20 so
    # the +$9 peak doesn't trip PROFIT_TARGET first and mask the trailing path.
    eng = _engine(target_usd=20.0)
    eng._trail_peak = None
    assert _tick(eng, net=0.5) is None           # near open
    assert _tick(eng, net=9.0) is None           # peak +9, armed (>= 40% of $20 = $8)
    out = _tick(eng, net=6.0)                     # 6 < 9 * 0.70 = 6.3 -> fire
    assert out is not None
    assert eng._override_exit_reason == "TRAILING_STOP"
