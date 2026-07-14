"""
Tests for the read-only drawdown analytics (core/analytics.py):
portfolio max drawdown of the realised-P&L curve, its % of capital, and
per-trade adverse/favourable excursion (MAE / MFE).
"""
from types import SimpleNamespace

import pytest

from core.analytics import max_drawdown, drawdown_pct, trade_excursions


# ── Portfolio max drawdown ───────────────────────────────────────────────────

def test_empty_series_is_all_zero():
    d = max_drawdown([])
    assert d["max_drawdown_usd"] == 0.0
    assert d["current_drawdown_usd"] == 0.0
    assert d["peak_equity_usd"] == 0.0
    assert d["n"] == 0


def test_all_winners_have_no_drawdown():
    d = max_drawdown([5.0, 3.0, 2.0])
    assert d["max_drawdown_usd"] == 0.0
    assert d["current_drawdown_usd"] == 0.0
    assert d["peak_equity_usd"] == 10.0
    assert d["final_equity_usd"] == 10.0


def test_dip_then_recover_measures_the_trough_but_current_is_zero():
    # equity curve: 10 -> 6 -> 3 -> 11. Worst drop peak(10)->trough(3) = 7.
    # Ends at a fresh high (11) so current drawdown is 0.
    d = max_drawdown([10.0, -4.0, -3.0, 8.0])
    assert d["max_drawdown_usd"] == pytest.approx(7.0)
    assert d["current_drawdown_usd"] == pytest.approx(0.0)
    assert d["peak_equity_usd"] == pytest.approx(11.0)


def test_ends_in_drawdown_sets_current_equal_when_it_is_the_worst():
    # equity: 10 -> 4. Peak 10, ends 4 -> both max and current drawdown = 6.
    d = max_drawdown([10.0, -6.0])
    assert d["max_drawdown_usd"] == pytest.approx(6.0)
    assert d["current_drawdown_usd"] == pytest.approx(6.0)


def test_monotonic_losses_drawdown_from_zero_start():
    # Never rises above the 0 starting equity, so peak stays 0 and the
    # drawdown is the full cumulative loss.
    d = max_drawdown([-2.0, -3.0, -5.0])
    assert d["max_drawdown_usd"] == pytest.approx(10.0)
    assert d["current_drawdown_usd"] == pytest.approx(10.0)
    assert d["peak_equity_usd"] == 0.0


def test_none_values_are_treated_as_zero():
    d = max_drawdown([10.0, None, -4.0])
    assert d["final_equity_usd"] == pytest.approx(6.0)
    assert d["max_drawdown_usd"] == pytest.approx(4.0)


# ── Drawdown as % of total capital ───────────────────────────────────────────

def test_drawdown_pct_basic():
    assert drawdown_pct(10.0, 1000.0) == pytest.approx(1.0)


def test_drawdown_pct_none_when_capital_unknown():
    assert drawdown_pct(10.0, None) is None
    assert drawdown_pct(10.0, 0.0) is None
    assert drawdown_pct(10.0, -5.0) is None


# ── Per-trade excursion (MAE / MFE) ──────────────────────────────────────────

def _trade(trough, peak, pnl, cap):
    return SimpleNamespace(trough_net_usd=trough, peak_net_usd=peak,
                           pnl_usd=pnl, capital_locked_usd=cap)


def test_underwater_but_recovered_is_flagged():
    ex = trade_excursions(_trade(trough=-5.0, peak=8.0, pnl=3.0, cap=100.0))
    assert ex["mae_usd"] == pytest.approx(5.0)      # went 5 against us
    assert ex["mae_pct"] == pytest.approx(5.0)      # 5 / 100 capital
    assert ex["mfe_usd"] == pytest.approx(8.0)
    assert ex["recovered"] is True                  # dipped, closed green


def test_never_negative_has_zero_mae_and_no_recovery():
    ex = trade_excursions(_trade(trough=2.0, peak=8.0, pnl=5.0, cap=100.0))
    assert ex["mae_usd"] == 0.0
    assert ex["recovered"] is False


def test_loser_that_stayed_down_is_not_recovered():
    ex = trade_excursions(_trade(trough=-10.0, peak=1.0, pnl=-4.0, cap=100.0))
    assert ex["mae_usd"] == pytest.approx(10.0)
    assert ex["recovered"] is False


def test_mae_pct_is_none_without_capital():
    ex = trade_excursions(_trade(trough=-10.0, peak=0.0, pnl=-4.0, cap=0.0))
    assert ex["mae_pct"] is None


def test_verdict_clean_win():
    # Won, never underwater
    assert trade_excursions(_trade(trough=0.5, peak=8.0, pnl=6.0, cap=100.0))["verdict"] == "clean"


def test_verdict_recovered():
    # Dipped underwater, still closed green
    assert trade_excursions(_trade(trough=-5.0, peak=8.0, pnl=3.0, cap=100.0))["verdict"] == "recovered"


def test_verdict_round_tripped():
    # Reached real unrealised profit, gave it all back to a loss
    assert trade_excursions(_trade(trough=-6.0, peak=7.0, pnl=-4.0, cap=100.0))["verdict"] == "round_tripped"


def test_verdict_loss():
    # Never in profit, closed red
    assert trade_excursions(_trade(trough=-10.0, peak=0.0, pnl=-4.0, cap=100.0))["verdict"] == "loss"


def test_mae_eq_pct_uses_account_equity_not_trade_capital():
    ex = trade_excursions(_trade(trough=-5.0, peak=8.0, pnl=3.0, cap=100.0), equity=1000.0)
    assert ex["mae_usd"] == pytest.approx(5.0)
    assert ex["mae_pct"] == pytest.approx(5.0)       # 5 / 100 (trade's locked capital)
    assert ex["mae_eq_pct"] == pytest.approx(0.5)    # 5 / 1000 (whole account)


def test_mae_eq_pct_none_without_equity():
    ex = trade_excursions(_trade(trough=-5.0, peak=8.0, pnl=3.0, cap=100.0))
    assert ex["mae_eq_pct"] is None


def test_pnl_eq_pct_uses_account_equity_not_trade_capital():
    ex = trade_excursions(_trade(trough=-5.0, peak=8.0, pnl=3.0, cap=100.0), equity=1000.0)
    assert ex["pnl_pct"] == pytest.approx(3.0)       # 3 / 100 (trade's locked capital)
    assert ex["pnl_eq_pct"] == pytest.approx(0.3)    # 3 / 1000 (whole account)


def test_pnl_eq_pct_negative_for_a_loss():
    ex = trade_excursions(_trade(trough=-10.0, peak=1.0, pnl=-4.0, cap=100.0), equity=800.0)
    assert ex["pnl_eq_pct"] == pytest.approx(-0.5)   # -4 / 800


def test_pnl_eq_pct_none_without_equity():
    ex = trade_excursions(_trade(trough=-5.0, peak=8.0, pnl=3.0, cap=100.0))
    assert ex["pnl_eq_pct"] is None


def test_mfe_pct_and_pnl_pct_on_trade_capital():
    ex = trade_excursions(_trade(trough=-5.0, peak=8.0, pnl=3.0, cap=100.0))
    assert ex["mfe_pct"] == pytest.approx(8.0)    # 8 / 100 locked capital
    assert ex["pnl_pct"] == pytest.approx(3.0)    # 3 / 100 locked capital


def test_pnl_pct_negative_for_a_loss():
    ex = trade_excursions(_trade(trough=-10.0, peak=1.0, pnl=-4.0, cap=100.0))
    assert ex["pnl_pct"] == pytest.approx(-4.0)
    assert ex["mfe_pct"] == pytest.approx(1.0)


def test_utilization_pct_is_trade_capital_over_equity():
    ex = trade_excursions(_trade(trough=-5.0, peak=8.0, pnl=3.0, cap=800.0), equity=1600.0)
    assert ex["utilization_pct"] == pytest.approx(50.0)   # 800 capital / 1600 equity


def test_utilization_pct_none_without_equity_or_capital():
    assert trade_excursions(_trade(trough=-5.0, peak=8.0, pnl=3.0, cap=800.0))["utilization_pct"] is None
    assert trade_excursions(_trade(trough=-5.0, peak=8.0, pnl=3.0, cap=0.0),
                            equity=1600.0)["utilization_pct"] is None
