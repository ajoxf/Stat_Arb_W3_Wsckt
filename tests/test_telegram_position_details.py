"""
Tests for the in-position details in Telegram messages: the entry notification
carries the trade's exit geometry (BE/EX/TP/SL levels + level P&L), and the
/positions command renders a live snapshot (net P&L, Δ spread, per-leg %
change, levels, age vs max-hold) from the engine status payload.
"""
from datetime import datetime

from core.telegram_bot import TelegramNotifier
from models import Trade


def make_notifier(captured):
    n = TelegramNotifier()
    n.is_ready = lambda: True          # bypass token/chat requirements
    n._notify_trades = True
    n._send = lambda text, **kw: captured.append(text)
    return n


def _trade():
    return Trade(
        id=99, asset="ETH/BTC", position_type="SHORT",
        entry_time=datetime(2026, 7, 7, 12, 0),
        entry_spot_price=1801.80, entry_futures_price=64000.10,
        entry_spread=-107.94, entry_zscore=-3.842,
        quantity=0.03, spot_qty=1.1, notional_usd=3722.0, margin_usd=186.0,
    )


LEVELS = {'entry': -107.94, 'break_even': -88.08, 'gate_release': -47.76,
          'take_profit': -28.75, 'stop': -228.90, 'favorable': 'up'}


def test_entry_notification_includes_geometry():
    captured = []
    n = make_notifier(captured)
    n.notify_trade_entry(_trade(), None, details={
        'levels': LEVELS, 'target_usd': 1.78, 'stop_usd': 3.63,
        'gate_usd': 1.86, 'capital': 484.0,
    })
    assert len(captured) == 1
    msg = captured[0]
    assert "Levels" in msg and "BE -88.08" in msg and "SL -228.90" in msg
    assert "EX -47.76" in msg                      # gate active -> EX chip shown
    assert "Level P&L" in msg and "TP +$1.78" in msg and "SL -$3.63 gross" in msg
    assert "Capital" in msg and "$484.00 at risk" in msg
    assert "Leg A Lots" in msg and "1.100000" in msg
    assert "Exec Ratio" in msg and "36.67" in msg


def test_entry_notification_without_details_still_sends():
    captured = []
    n = make_notifier(captured)
    n.notify_trade_entry(_trade(), None)           # legacy call, no details
    assert len(captured) == 1
    assert "TRADE ENTRY" in captured[0]
    assert "Levels" not in captured[0]


def test_positions_command_renders_live_snapshot():
    captured = []
    n = make_notifier(captured)
    n.get_status_cb = lambda: {
        "position": "SHORT",
        "asset": "ETH/BTC",
        "open_trade": {
            "entry_time": "2026-07-07 12:00:00",
            "entry_spot_price": 1801.80, "entry_futures_price": 64000.10,
            "entry_spread": -107.94, "entry_zscore": -3.842,
            "quantity": 0.03, "spot_qty": 1.1,
            "notional_usd": 3722.0, "margin_usd": 186.0,
            "unrealized_pnl": -2.69, "held_minutes": 23.0,
            "max_hold_minutes": 18.6,
            "exit_target_usd": 1.78, "exit_stop_usd": 3.63,
            "exit_gate_floor_usd": 1.86,
            "spread_levels": LEVELS,
            "is_paper": False,
        },
        "signal": {"zscore": -1.8691, "spread": -188.19},
        "spot_tick": {"last": 1804.02},
        "futures_tick": {"last": 63997.95},
    }
    n._cmd_positions()
    assert len(captured) == 1
    msg = captured[0]
    assert "Net P&L" in msg and "$-2.69" in msg
    assert "Δ Spread" in msg and "-80.25" in msg and "against" in msg
    assert "Levels" in msg and "BE -88.08" in msg
    assert "Level P&L" in msg and "BE $0.00" in msg
    assert "+0.12%" in msg                          # spot leg change since entry
    assert "EXPIRED" in msg                         # held 23m > max 18.6m
    assert "Leg A Lots" in msg and "ratio 36.67" in msg


def test_positions_command_flat():
    captured = []
    n = make_notifier(captured)
    n.get_status_cb = lambda: {"position": "NONE", "open_trade": None}
    n._cmd_positions()
    assert len(captured) == 1
    assert "No open positions" in captured[0]


# ── crisp lifecycle analysis (trade #78 regression numbers) ──────────────────

def _closed_trade_78():
    t = _trade()
    t.exit_time = datetime(2026, 7, 7, 17, 54)
    t.exit_spot_price = 1808.5
    t.exit_futures_price = 63875.0
    t.exit_spread = -213.3
    t.exit_zscore = -5.6682
    t.exit_reason = "STOP_LOSS"
    t.pnl_usd = -4.46
    t.pnl_gross_usd = -3.16
    t.is_open = False
    return t


STATS_78 = {'peak_net': 1.19, 'trough_net': -4.46, 'z_min': -5.67, 'z_max': 1.23,
            'gate_holds': 52, 'gate_held_min': 79.0, 'gate_floor': 1.21,
            'held_min': 88.0, 'max_hold_min': 20.0, 'available_usd': 6.15,
            'exit_threshold': 0.5}


def test_exit_notification_renders_crisp_analysis():
    captured = []
    n = make_notifier(captured)
    n.notify_trade_exit(_closed_trade_78(), stats=STATS_78)
    msg = captured[0]
    assert "ANALYSIS" in msg
    assert "STOPPED AFTER FULL REVERSION" in msg      # z_max 1.23 crossed -0.5
    assert "Peak/Trough" in msg and "+$1.19" in msg and "-4.46" in msg
    assert "held 52" in msg and "floor $1.21" in msg
    assert "88m" in msg and "×4.4" in msg
    assert "range -5.67" in msg and "+1.23" in msg


def test_outcome_tag_distinguishes_trend_stop():
    t = _closed_trade_78()
    stats = dict(STATS_78, z_max=-2.9)                # never reverted
    tag = TelegramNotifier._outcome_tag(t, stats)
    assert "STOPPED IN TREND" in tag


def test_outcome_tag_reversion_banked():
    t = _closed_trade_78()
    t.exit_reason = "EXIT"
    t.pnl_usd = 1.41
    assert "REVERSION BANKED" in TelegramNotifier._outcome_tag(t, STATS_78)


def test_analyzer_scorecard_includes_lifecycle_rows():
    from core.post_trade_analyzer import PostTradeAnalyzer
    from models import TradingConfig
    t = _closed_trade_78()
    t.entry_spread_mean = -20.0
    t.entry_spread_std = 25.0
    t.lifecycle_stats = STATS_78
    pa = PostTradeAnalyzer.__new__(PostTradeAnalyzer)
    _, scorecard, _ = pa._build_prompt(t, [t], [], TradingConfig(), None)
    labels = [s["label"] for s in scorecard]
    assert "Peak/Trough" in labels and "Z range" in labels
    assert "Gate" in labels and "Hold vs max" in labels
