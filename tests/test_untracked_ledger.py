"""
Tests for the untracked-close ledger: cleanup closes (orphan auto-closes) that
move money on the exchange OUTSIDE any recorded trade must be persisted,
counted against the daily-loss tracker, and summarized for the dashboard.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.trading_engine import TradingEngine
from database.manager import DatabaseManager
from models import TradingConfig


# ── DB layer ──────────────────────────────────────────────────────────────────

def test_ledger_roundtrip_and_today_totals(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "test.db"))
    db.save_untracked_close(source="ORPHAN_AUTO_CLOSE", symbol="BTC-USDT-SWAP",
                            side="LONG", quantity=0.03, pnl_usd=-0.39,
                            fee_est_usd=0.95, note="test")
    db.save_untracked_close(source="ORPHAN_AUTO_CLOSE", symbol="ETH-USDT-SWAP",
                            side="SHORT", quantity=1.0, pnl_usd=0.74,
                            fee_est_usd=0.48)

    rows = db.get_untracked_closes()
    assert len(rows) == 2
    assert {r["symbol"] for r in rows} == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}

    totals = db.get_untracked_totals_today()
    assert totals["count"] == 2
    assert totals["pnl_usd"] == pytest.approx(0.35)
    assert totals["fee_est_usd"] == pytest.approx(1.43)
    assert totals["net_usd"] == pytest.approx(-1.08)


def test_today_totals_empty(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "empty.db"))
    totals = db.get_untracked_totals_today()
    assert totals == {"count": 0, "pnl_usd": 0, "fee_est_usd": 0, "net_usd": 0}


def test_trade_lifecycle_extremes_roundtrip(tmp_path):
    """peak/trough (net USD + minutes-after-entry) persist on the trade row —
    the data 'did profit come before the loss?' is answered from."""
    from datetime import datetime
    from models import Trade

    db = DatabaseManager(db_path=str(tmp_path / "lc.db"))
    t = Trade(asset="ETH/BTC", position_type="SHORT",
              entry_time=datetime(2026, 7, 7, 16, 26),
              entry_spot_price=1801.8, entry_futures_price=64000.1,
              entry_spread=-107.94, entry_zscore=-3.84, quantity=0.03,
              spot_order_id="s78", futures_order_id="f78", is_open=True)
    t.id = db.save_trade(t)

    t.exit_time = datetime(2026, 7, 7, 17, 54)
    t.exit_reason = "STOP_LOSS"
    t.pnl_usd = -4.46
    t.is_open = False
    t.peak_net_usd = 1.19
    t.trough_net_usd = -4.46
    t.peak_minutes = 6.0
    t.trough_minutes = 88.0
    db.save_trade(t)

    loaded = db.get_trades(limit=1)[0]
    assert loaded.peak_net_usd == pytest.approx(1.19)
    assert loaded.trough_net_usd == pytest.approx(-4.46)
    assert loaded.peak_minutes == pytest.approx(6.0)
    assert loaded.trough_minutes == pytest.approx(88.0)


# ── engine emission ───────────────────────────────────────────────────────────

def _make_engine(captured):
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = TradingConfig(futures_taker_fee_bps=5.0)
    eng._daily_loss_usd = 0.0
    eng.on_untracked_close = captured.append
    eng.futures_adapter = SimpleNamespace(
        close_position=AsyncMock(return_value=SimpleNamespace(success=True, error=None)))
    return eng


def _orphan(symbol="BTC-USDT-SWAP", side="LONG", qty=0.03,
            entry_price=63000.0, upl=-0.39):
    return {"symbol": symbol, "side": side, "quantity": qty,
            "entry_price": entry_price, "unrealized_pnl": upl}


def test_auto_close_emits_ledger_entry_and_books_daily_loss():
    captured = []
    eng = _make_engine(captured)
    asyncio.run(eng._auto_close_orphan_positions([_orphan()]))

    assert len(captured) == 1
    e = captured[0]
    assert e["source"] == "ORPHAN_AUTO_CLOSE"
    assert e["symbol"] == "BTC-USDT-SWAP"
    assert e["pnl_usd"] == pytest.approx(-0.39)
    # fee est = 5 bps × 0.03 BTC × $63,000 = $0.945
    assert e["fee_est_usd"] == pytest.approx(0.945)
    # daily tracker charged with pnl − fee
    assert eng._daily_loss_usd == pytest.approx(-0.39 - 0.945)


def test_failed_close_books_nothing():
    captured = []
    eng = _make_engine(captured)
    eng.futures_adapter.close_position = AsyncMock(
        return_value=SimpleNamespace(success=False, error="boom"))
    asyncio.run(eng._auto_close_orphan_positions([_orphan()]))
    assert captured == []
    assert eng._daily_loss_usd == 0.0


def test_non_derivative_positions_never_touched():
    captured = []
    eng = _make_engine(captured)
    asyncio.run(eng._auto_close_orphan_positions([_orphan(symbol="BTC-USDT")]))
    assert captured == []
    assert eng.futures_adapter.close_position.await_count == 0
