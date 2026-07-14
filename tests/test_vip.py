"""
Tests for the dashboard VIP-tier volume card:
  • core/vip.py  — pure tier-threshold math (AED→USD floors, tier parsing,
                   next-tier lookup, current-month seed gating).
  • DatabaseManager.get_month_volume_usd — this bot's monthly executed notional,
    counting each trade's opening (by entry month) and closing (by exit month).
"""
from datetime import datetime

import pytest

from core import vip
from database.manager import DatabaseManager
from models import Trade


# ── core/vip.py: tier thresholds & parsing ───────────────────────────────────

def test_usd_floors_convert_from_aed_at_the_peg():
    # VIP 4 floor 720,000,001 AED / 3.6725 ≈ $196.05M
    assert vip.VIP_VOLUME_FLOOR_USD[4] == pytest.approx(720_000_001 / 3.6725, rel=1e-9)
    assert vip.VIP_VOLUME_FLOOR_USD[4] == pytest.approx(196_051_736.6, abs=1.0)
    assert vip.VIP_VOLUME_FLOOR_USD[5] == pytest.approx(588_155_208.7, abs=1.0)


def test_parse_vip_tier_accepts_common_formats():
    assert vip.parse_vip_tier("VIP4") == 4
    assert vip.parse_vip_tier("Lv4") == 4
    assert vip.parse_vip_tier("4") == 4
    assert vip.parse_vip_tier("VIP10") == 10


def test_parse_vip_tier_defaults_to_zero():
    assert vip.parse_vip_tier("") == 0
    assert vip.parse_vip_tier(None) == 0
    assert vip.parse_vip_tier("Regular") == 0


def test_maintain_floor_is_the_tier_floor_and_regular_points_at_vip1():
    assert vip.maintain_floor_usd(4) == vip.VIP_VOLUME_FLOOR_USD[4]
    assert vip.maintain_floor_usd(0) == vip.VIP_VOLUME_FLOOR_USD[1]   # Regular → first milestone
    assert vip.maintain_floor_usd(99) is None                        # off the top of the table


def test_next_tier_floor_walks_up_and_caps_at_top():
    assert vip.next_tier_floor_usd(4) == (5, vip.VIP_VOLUME_FLOOR_USD[5])
    assert vip.next_tier_floor_usd(0) == (1, vip.VIP_VOLUME_FLOOR_USD[1])
    assert vip.next_tier_floor_usd(9) == (None, None)


def test_month_baseline_only_applies_to_its_tagged_month():
    assert vip.month_baseline_usd(vip.VIP_MONTH_BASELINE_MONTH) == vip.VIP_MONTH_BASELINE_USD
    assert vip.month_baseline_usd("1999-01") == 0.0


# ── DatabaseManager.get_month_volume_usd ─────────────────────────────────────

def _open(db, entry, notional, paper=False):
    """INSERT an open trade (entry-only), mirroring the real lifecycle."""
    t = Trade(asset="ETH/BTC", position_type="LONG", entry_time=entry,
              notional_usd=notional, is_open=True, is_paper=paper)
    t.id = db.save_trade(t)
    return t


def _close(db, t, exit_):
    """UPDATE the trade to closed with an exit_time (the real close path)."""
    t.exit_time = exit_
    t.is_open = False
    db.save_trade(t)


def test_month_volume_counts_open_and_close_executions(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "vol.db"))
    # A: opened AND closed in July → counts twice (2 × 10,000).
    a = _open(db, datetime(2026, 7, 5, 10, 0), 10_000)
    _close(db, a, datetime(2026, 7, 6, 10, 0))
    # B: opened in July, still open → counts once (8,000).
    _open(db, datetime(2026, 7, 20, 9, 0), 8_000)
    # C: opened in June, closed in July → only the July CLOSE counts here (5,000).
    c = _open(db, datetime(2026, 6, 30, 23, 0), 5_000)
    _close(db, c, datetime(2026, 7, 1, 1, 0))
    # D: paper trade in July → excluded entirely.
    d = _open(db, datetime(2026, 7, 10, 12, 0), 99_999, paper=True)
    _close(db, d, datetime(2026, 7, 10, 13, 0))

    july = db.get_month_volume_usd("2026-07")
    assert july == pytest.approx(10_000 + 10_000 + 8_000 + 5_000)   # 33,000

    june = db.get_month_volume_usd("2026-06")
    assert june == pytest.approx(5_000)   # only C's June open


def test_month_volume_zero_when_no_trades(tmp_path):
    db = DatabaseManager(db_path=str(tmp_path / "empty.db"))
    assert db.get_month_volume_usd("2026-07") == 0.0
