"""
Tests for shadow-watch persistence across a restart.

The shadow tracker held active watches in memory only, so a restart mid-window
silently dropped them — during a tuning session with frequent restarts, watches
rarely survived their 60-min window and the shadow "never updated". Now each
armed watch is persisted (shadow_pending) and resumed on startup.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from core.trading_engine import (
    TradingEngine, ShadowHold, _SHADOW_HOLD_MINUTES,
)
from database.manager import DatabaseManager
from models import Trade, TradingConfig


# ── DB round-trip ─────────────────────────────────────────────────────────────

def _rec(key="2026-07-10T11:40:15", trade_id=92):
    return {"pending_key": key, "trade_id": trade_id, "position_type": "SHORT",
            "entry_time": "2026-07-10T10:22:02", "exit_time": key,
            "entry_spot": 1794.2, "entry_fut": 64304.05, "spot_qty": 0.7,
            "futures_qty": 0.02, "fees_usd": 0.41, "target_usd": 1.8,
            "exit_net": -0.16, "exit_reason": "EXIT"}


def test_pending_upsert_and_delete(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "sp.db"))
    db.save_shadow_pending(_rec())
    db.save_shadow_pending(_rec())            # same key -> upsert, still one row
    rows = db.get_shadow_pending()
    assert len(rows) == 1
    assert rows[0]["trade_id"] == 92 and rows[0]["exit_reason"] == "EXIT"

    db.save_shadow_pending(_rec(key="2026-07-10T12:00:00", trade_id=93))
    assert len(db.get_shadow_pending()) == 2

    db.delete_shadow_pending("2026-07-10T11:40:15")
    keys = [r["pending_key"] for r in db.get_shadow_pending()]
    assert keys == ["2026-07-10T12:00:00"]


# ── engine arm persists; restore resumes ─────────────────────────────────────

def _engine(captured):
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = TradingConfig(hedge_ratio=35.58)
    eng._shadow_holds = []
    eng.on_shadow_hold = None
    eng.on_shadow_pending = lambda action, payload: captured.append((action, payload))
    eng._override_exit_reason = None
    eng._effective_exit_targets = lambda t: {"target_usd": 1.8, "stop_usd": 1.6,
                                             "max_hold_periods": 0.0, "max_hold_minutes": 20.0,
                                             "round_trip_cost": 0.0}
    eng._round_trip_fees = lambda t: 0.41
    eng.spot_tick = SimpleNamespace(mid=1794.2)
    eng.futures_tick = SimpleNamespace(mid=64304.0)
    return eng


def test_arm_persists_watch():
    captured = []
    eng = _engine(captured)
    now = datetime.utcnow()
    t = Trade(asset="ETH/BTC", position_type="SHORT",
              entry_time=now - timedelta(minutes=78), exit_time=now,
              entry_spot_price=1794.2, entry_futures_price=64304.05,
              quantity=0.02, spot_qty=0.7, pnl_usd=-0.16, exit_reason="EXIT")
    eng._open_shadow_hold(t, "EXIT")
    assert len(eng._shadow_holds) == 1
    saves = [p for a, p in captured if a == "save"]
    assert len(saves) == 1
    assert saves[0]["pending_key"] == eng._shadow_holds[0].pending_key
    assert saves[0]["exit_reason"] == "EXIT"


def test_restore_resumes_open_window():
    eng = _engine([])
    now = datetime.utcnow()
    row = _rec(key=(now - timedelta(minutes=10)).isoformat())
    row["exit_time"] = (now - timedelta(minutes=10)).isoformat()  # 10m ago -> still open
    resumed = eng.restore_shadow_holds([row])
    assert resumed == 1
    assert len(eng._shadow_holds) == 1
    h = eng._shadow_holds[0]
    assert h.position_type == "SHORT" and h.pending_key == row["pending_key"]
    # It continues tracking: an update marks it against current mids.
    eng._update_shadow_holds()
    assert h.ticks >= 1


def test_restore_drops_elapsed_window():
    captured = []
    eng = _engine(captured)
    now = datetime.utcnow()
    stale = (now - timedelta(minutes=_SHADOW_HOLD_MINUTES + 5)).isoformat()
    row = _rec(key=stale)
    row["exit_time"] = stale
    resumed = eng.restore_shadow_holds([row])
    assert resumed == 0
    assert eng._shadow_holds == []
    # The stale record is cleaned up, not left to linger.
    assert ("delete", stale) in captured


def test_finalize_deletes_pending():
    captured = []
    eng = _engine(captured)
    now = datetime.utcnow()
    h = ShadowHold(trade_id=1, position_type="SHORT",
                   entry_time=now - timedelta(minutes=90),
                   exit_time=now - timedelta(minutes=_SHADOW_HOLD_MINUTES + 1),
                   entry_spot=1794.2, entry_fut=64304.05, spot_qty=0.7, futures_qty=0.02,
                   fees_usd=0.41, target_usd=1.8, exit_net=-0.16, exit_reason="EXIT",
                   pending_key="k1")
    eng._shadow_holds = [h]
    eng._update_shadow_holds()                 # window elapsed -> finalize
    assert eng._shadow_holds == []
    assert ("delete", "k1") in captured
