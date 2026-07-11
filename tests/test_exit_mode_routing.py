"""
Tests for exit-mode routing (MARKET vs maker-first) and the shadow-arm scope.

Two live-trade findings drove these:
  - #91 exited via TRAILING_STOP and was forced to MARKET (taker), and the
    taker fee ate ~$0.5 of a ~$0.65 win. A trailing stop banks a PROFIT — it
    should get a maker probe first, like DOLLAR_STOP, not be forced to taker.
  - #91 (a small WIN) never appeared in the shadow tracker because it only
    armed on losers/stops. Under-captured WINS are exactly the "did we exit
    too early?" case the tracker should catch — arm on everything except a
    clean profit-target hit.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from core.trading_engine import TradingEngine, MAKER_PROBE_EXIT_REASONS
from models import Trade, TradingConfig


# ── exit-reason routing constant ─────────────────────────────────────────────

def test_trailing_and_dollar_stop_are_maker_probe():
    assert "TRAILING_STOP" in MAKER_PROBE_EXIT_REASONS
    assert "DOLLAR_STOP" in MAKER_PROBE_EXIT_REASONS


def test_loss_stops_are_not_maker_probe():
    # Genuine loss stops must stay straight-to-MARKET (urgent).
    assert "STOP_LOSS" not in MAKER_PROBE_EXIT_REASONS
    assert "DAILY_LOSS" not in MAKER_PROBE_EXIT_REASONS


def test_use_market_decision_matrix():
    # Reproduce the branch from _close_position's exit-mode selection.
    NON_URGENT = ("EXIT", "PROFIT_TARGET", "MAX_HOLD")

    # MARKET_AFTER mirrors engine._EXIT_POSTONLY_MARKET_AFTER (3): non-urgent
    # exits probe maker up to 3 times before MARKET, to save the taker fee.
    def use_market(reason, maker_attempts, MAX_ATTEMPTS=1, postonly_rejects=0, MARKET_AFTER=3):
        r = reason.upper()
        is_stop = r not in NON_URGENT
        is_probe = r in MAKER_PROBE_EXIT_REASONS
        if is_probe:
            return maker_attempts >= MAX_ATTEMPTS
        if is_stop:
            return True
        return postonly_rejects >= MARKET_AFTER

    # Trailing stop: first attempt is MAKER (not market), escalates after the probe.
    assert use_market("TRAILING_STOP", maker_attempts=0) is False
    assert use_market("TRAILING_STOP", maker_attempts=1) is True
    # Dollar stop unchanged: maker probe then market.
    assert use_market("DOLLAR_STOP", maker_attempts=0) is False
    assert use_market("DOLLAR_STOP", maker_attempts=1) is True
    # Loss stop: straight to market.
    assert use_market("STOP_LOSS", maker_attempts=0) is True
    # Reversion / target / max-hold: maker-first, and now given more room —
    # 1 or 2 rejections still retry maker; only the 3rd falls back to MARKET.
    assert use_market("EXIT", maker_attempts=0, postonly_rejects=0) is False
    assert use_market("EXIT", maker_attempts=0, postonly_rejects=1) is False
    assert use_market("EXIT", maker_attempts=0, postonly_rejects=2) is False
    assert use_market("PROFIT_TARGET", maker_attempts=0, postonly_rejects=3) is True


# ── shadow-arm scope: winners that under-captured are now tracked ────────────

def _engine():
    eng = TradingEngine.__new__(TradingEngine)
    eng.config = TradingConfig(hedge_ratio=35.58)
    eng._shadow_holds = []
    eng.on_shadow_hold = None
    eng._override_exit_reason = None
    eng._effective_exit_targets = lambda t: {"target_usd": 1.81, "stop_usd": 1.6,
                                             "max_hold_periods": 0.0, "max_hold_minutes": 20.0,
                                             "round_trip_cost": 0.0}
    eng._round_trip_fees = lambda t: 0.8
    eng.spot_tick = SimpleNamespace(mid=1769.0)
    eng.futures_tick = SimpleNamespace(mid=63865.0)
    return eng


def _trade(pnl, exit_reason):
    now = datetime.utcnow()
    return Trade(asset="ETH/BTC", position_type="LONG",
                 entry_time=now - timedelta(minutes=11), exit_time=now,
                 entry_spot_price=1769.0, entry_futures_price=63865.0,
                 quantity=0.02, spot_qty=0.7, pnl_usd=pnl, exit_reason=exit_reason)


def test_trailing_stop_win_is_now_tracked():
    # #91: a small WIN via trailing stop must be armed (under-capture case).
    eng = _engine()
    eng._open_shadow_hold(_trade(pnl=0.15, exit_reason="TRAILING_STOP"), "STOP_LOSS")
    assert len(eng._shadow_holds) == 1


def test_sub_target_reversion_win_is_tracked():
    eng = _engine()
    eng._open_shadow_hold(_trade(pnl=0.30, exit_reason="EXIT"), "EXIT")
    assert len(eng._shadow_holds) == 1


def test_clean_target_hit_is_not_tracked():
    # A full PROFIT_TARGET capture got the intended profit — nothing to learn.
    eng = _engine()
    eng._override_exit_reason = "PROFIT_TARGET"
    eng._open_shadow_hold(_trade(pnl=1.81, exit_reason="PROFIT_TARGET"), "STOP_LOSS")
    assert eng._shadow_holds == []


def test_loss_still_tracked():
    eng = _engine()
    eng._open_shadow_hold(_trade(pnl=-2.05, exit_reason="DOLLAR_STOP"), "STOP_LOSS")
    assert len(eng._shadow_holds) == 1
