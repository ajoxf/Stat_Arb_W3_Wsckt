"""
Tests for the shadow "what-if-held" tracker.

After a stopped/losing trade closes, the engine keeps marking the REAL held P&L
of the position it just exited (per_leg_gross_pnl on the actual filled legs) for
a fixed window, and logs whether it would have reverted to break-even / the
profit target — the measured answer to "the price always reverts, just wait."

Covers: per-tick accumulation (peak/trough + BE/target crossings), finalize on
window elapse, the arm gate (skip winners, arm losses/stops), the active-watch
cap, and the DB round-trip + summary aggregation.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.trading_engine import (
    TradingEngine, ShadowHold, _SHADOW_HOLD_MINUTES, _SHADOW_HOLD_MAX_ACTIVE,
)
from database.manager import DatabaseManager
from models import Trade, TradingConfig


def _tick(mid):
    return SimpleNamespace(mid=mid)


def _bare_engine():
    eng = TradingEngine.__new__(TradingEngine)      # bypass heavy __init__
    eng.config = TradingConfig(hedge_ratio=35.58)
    eng._shadow_holds = []
    eng.on_shadow_hold = None
    eng.on_shadow_pending = None
    eng._override_exit_reason = None
    eng.spot_tick = None
    eng.futures_tick = None
    return eng


def _hold(**kw):
    now = datetime.utcnow()
    base = dict(trade_id=1, position_type="LONG",
                entry_time=now - timedelta(minutes=10), exit_time=now,
                entry_spot=100.0, entry_fut=100.0, spot_qty=1.0, futures_qty=1.0,
                fees_usd=0.0, target_usd=5.0, exit_net=-2.0, exit_reason="DOLLAR_STOP")
    base.update(kw)
    return ShadowHold(**base)


# ── per-tick accumulation ──────────────────────────────────────────────────

def test_update_accumulates_peak_trough_and_crossings():
    eng = _bare_engine()
    # LONG with fut held at 100 => held net == (spot_mid - 100).
    h = _hold()
    eng._shadow_holds = [h]

    eng.spot_tick, eng.futures_tick = _tick(103.0), _tick(100.0)
    eng._update_shadow_holds()
    assert h.hit_be is True and h.hit_target is False   # net +3: past BE, under +5
    assert h.peak_net == pytest.approx(3.0)
    assert h.hit_be_min is not None

    eng.spot_tick = _tick(98.0)                          # net -2: new trough, still under target
    eng._update_shadow_holds()
    assert h.trough_net == pytest.approx(-2.0)
    assert h.hit_target is False
    assert h.hit_be is True                              # sticky once crossed
    assert eng._shadow_holds == [h]                      # still active (target not hit)

    eng.spot_tick = _tick(106.0)                         # net +6 >= target -> finalizes early
    eng._update_shadow_holds()
    assert h.hit_target is True
    assert h.peak_net == pytest.approx(6.0)
    assert eng._shadow_holds == []                       # finalized the moment it hit target


def test_short_direction_uses_real_leg_pnl():
    eng = _bare_engine()
    # SHORT held net = spot_qty*(entry_spot-exit_spot) + fut*(exit_fut-entry_fut).
    h = _hold(position_type="SHORT")
    eng._shadow_holds = [h]
    eng.spot_tick, eng.futures_tick = _tick(96.0), _tick(100.0)   # +4 from spot leg
    eng._update_shadow_holds()
    assert h.peak_net == pytest.approx(4.0)
    assert h.hit_be is True


# ── finalize ────────────────────────────────────────────────────────────────

def test_finalize_emits_record_when_window_elapsed():
    eng = _bare_engine()
    captured = []
    eng.on_shadow_hold = captured.append
    h = _hold(trade_id=7,
              entry_time=datetime.utcnow() - timedelta(minutes=90),
              exit_time=datetime.utcnow() - timedelta(minutes=_SHADOW_HOLD_MINUTES + 1))
    eng._shadow_holds = [h]
    eng.spot_tick, eng.futures_tick = _tick(106.0), _tick(100.0)   # net +6 -> target
    eng._update_shadow_holds()

    assert len(captured) == 1
    rec = captured[0]
    assert rec["trade_id"] == 7
    assert rec["hit_target"] is True
    assert rec["verdict"] == "REVERTED TO TARGET"
    assert eng._shadow_holds == []                       # drained after finalize


def test_verdict_kept_bleeding_when_never_positive():
    eng = _bare_engine()
    captured = []
    eng.on_shadow_hold = captured.append
    h = _hold(exit_time=datetime.utcnow() - timedelta(minutes=_SHADOW_HOLD_MINUTES + 1))
    eng._shadow_holds = [h]
    eng.spot_tick, eng.futures_tick = _tick(97.0), _tick(100.0)    # net -3, never +
    eng._update_shadow_holds()
    assert captured[0]["verdict"] == "KEPT BLEEDING"
    assert captured[0]["hit_break_even"] is False


# ── arm gate ────────────────────────────────────────────────────────────────

def _armable_engine():
    eng = _bare_engine()
    eng._effective_exit_targets = lambda t: {
        "target_usd": 5.0, "stop_usd": 2.0, "max_hold_periods": 0.0,
        "max_hold_minutes": 20.0, "round_trip_cost": 0.0}
    eng._round_trip_fees = lambda t: 0.5
    eng.spot_tick, eng.futures_tick = _tick(1800.0), _tick(64000.0)
    return eng


def _trade(pnl):
    now = datetime.utcnow()
    return Trade(asset="ETH/BTC", position_type="LONG",
                 entry_time=now - timedelta(minutes=5), exit_time=now,
                 entry_spot_price=1800.0, entry_futures_price=64000.0,
                 quantity=0.03, spot_qty=1.06, pnl_usd=pnl)


def test_open_skips_clean_target_hit():
    # A full PROFIT_TARGET capture got the intended profit — nothing to learn.
    eng = _armable_engine()
    eng._override_exit_reason = "PROFIT_TARGET"
    eng._open_shadow_hold(_trade(pnl=2.5), "STOP_LOSS")
    assert eng._shadow_holds == []


def test_open_arms_early_win():
    # A small win that exited early (reversion EXIT / trailing, not a target
    # hit) IS now tracked — it may have left money on the table (live #91).
    eng = _armable_engine()
    eng._open_shadow_hold(_trade(pnl=0.15), "EXIT")
    assert len(eng._shadow_holds) == 1


def test_open_arms_on_loss():
    eng = _armable_engine()
    eng._open_shadow_hold(_trade(pnl=-2.72), "EXIT")
    assert len(eng._shadow_holds) == 1
    assert eng._shadow_holds[0].target_usd == 5.0
    assert eng._shadow_holds[0].exit_net == pytest.approx(-2.72)


def test_open_arms_on_stop_even_if_flat_pnl():
    eng = _armable_engine()
    eng._open_shadow_hold(_trade(pnl=0.0), "STOP_LOSS")   # stop reason overrides
    assert len(eng._shadow_holds) == 1


def test_active_watches_are_capped():
    eng = _armable_engine()
    for _ in range(_SHADOW_HOLD_MAX_ACTIVE + 5):
        eng._open_shadow_hold(_trade(pnl=-1.0), "STOP_LOSS")
    assert len(eng._shadow_holds) == _SHADOW_HOLD_MAX_ACTIVE


# ── DB layer ─────────────────────────────────────────────────────────────────

def test_db_roundtrip_and_summary(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "sh.db"))
    db.save_shadow_hold({
        "trade_id": 1, "position_type": "LONG", "exit_reason": "DOLLAR_STOP",
        "exit_net_usd": -2.72, "target_usd": 1.2, "window_min": 60,
        "peak_net_usd": 1.5, "peak_min": 12.0, "trough_net_usd": -3.0,
        "trough_min": 6.0, "hit_break_even": True, "hit_be_min": 8.0,
        "hit_target": True, "hit_target_min": 12.0, "final_net_usd": 0.9,
        "verdict": "REVERTED TO TARGET"})
    db.save_shadow_hold({
        "trade_id": 2, "position_type": "SHORT", "exit_reason": "STOP_LOSS",
        "exit_net_usd": -3.0, "target_usd": 1.2, "window_min": 60,
        "peak_net_usd": -0.5, "peak_min": 4.0, "trough_net_usd": -5.0,
        "trough_min": 40.0, "hit_break_even": False, "hit_be_min": None,
        "hit_target": False, "hit_target_min": None, "final_net_usd": -4.0,
        "verdict": "KEPT BLEEDING"})

    rows = db.get_shadow_holds()
    assert len(rows) == 2

    s = db.get_shadow_summary()
    assert s["count"] == 2
    assert s["reverted_be"] == 1
    assert s["reverted_target"] == 1
    assert s["kept_bleeding"] == 1
    assert s["revert_be_rate"] == pytest.approx(0.5)
    assert s["median_be_min"] == pytest.approx(8.0)
    assert s["avg_peak_usd"] == pytest.approx((1.5 + -0.5) / 2)


def test_empty_summary(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "empty.db"))
    assert db.get_shadow_summary() == {
        "count": 0, "reverted_be": 0, "reverted_target": 0, "kept_bleeding": 0,
        "revert_be_rate": 0.0, "revert_target_rate": 0.0, "median_be_min": None,
        "median_target_min": None, "avg_peak_usd": 0.0}
