"""
Telegram Bot Integration - Real-time trade notifications and interactive commands.

Sends trade entry/exit alerts, signals, and errors to a Telegram chat.
Supports interactive commands: /status, /positions, /trades, /balance, /pnl,
/eod, /pause, /resume, /settings, /set, /optimize, /closeall

Only requires the 'requests' library (already in requirements.txt).
"""

import html
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

# Commands published to Telegram's chat menu (the "Menu" button next to the
# input field, and the popup shown when typing "/"). Displayed in this order.
# Bot API rules: names lowercase a-z0-9_, descriptions 1-256 chars.
BOT_COMMAND_MENU = [
    ("dashboard", "Full system snapshot"),
    ("status",    "Engine & algo state"),
    ("positions", "Open positions: live P&L + exit levels"),
    ("trades",    "Recent closed trades"),
    ("pnl",       "P&L summary"),
    ("stats",     "Edge stats + max drawdown"),
    ("shadow",    "What-if-held: did the exits revert?"),
    ("balance",   "Account balance"),
    ("pause",     "Algo OFF — halt new entries"),
    ("resume",    "Algo ON — allow new entries"),
    ("restart",   "Restart the program (if it feels stuck)"),
    ("eod",       "End-of-day report"),
    ("settings",  "Show all tunable settings"),
    ("set",       "Change a setting: /set key value"),
    ("optimize",  "Run parameter grid search"),
    ("closeall",  "EMERGENCY: market-close all positions"),
    ("help",      "List all commands"),
    ("ping",      "Alive check"),
]


def _parse_exit_mode(raw: str) -> str:
    """Validator for /set exit_signal_mode — one of zscore | spread | hybrid."""
    v = str(raw).strip().lower()
    if v not in ("zscore", "spread", "hybrid"):
        raise ValueError("exit mode must be one of: zscore, spread, hybrid")
    return v


def _parse_onoff(raw: str) -> bool:
    """Validator for /set on a boolean config field. Plain bool(str) is wrong
    (bool('off') is True), so parse the words explicitly."""
    v = str(raw).strip().lower()
    if v in ("on", "true", "yes", "1", "enabled", "enable"):
        return True
    if v in ("off", "false", "no", "0", "disabled", "disable"):
        return False
    raise ValueError("expected on/off (also accepts true/false, yes/no, 1/0)")


def send_telegram_message(token: str, chat_id: str, text: str,
                          parse_mode: Optional[str] = "HTML") -> bool:
    """
    Send a message to a Telegram chat.

    If parse_mode='HTML' (default) and Telegram rejects the message (HTTP 400
    — usually because the body contains a stray '<', '>' or '&' that doesn't
    parse as HTML), automatically retries as plain text. That way an error
    message always reaches the user; the previous behavior silently dropped
    parse-failed messages, which is why handler crashes looked like 'no reply
    at all' instead of a visible error.
    """
    if not token or not chat_id:
        return False
    try:
        url = f"{TELEGRAM_API_BASE.format(token=token)}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True

        # Telegram rejected — most commonly an HTML parse error. Fall back to
        # plain text so the user actually sees what we tried to say.
        if parse_mode and resp.status_code == 400:
            logger.warning("Telegram HTML send failed (400): %s — retrying as plain text",
                           resp.text[:200])
            payload.pop("parse_mode", None)
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True

        logger.warning("Telegram sendMessage failed (%d): %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.error("Telegram sendMessage error: %s", e)
        return False


class TelegramNotifier:
    """
    Sends trade notifications to a Telegram chat and handles interactive commands.

    Instantiate once and keep alive for the session. Call update_config() whenever
    the trading config changes. Call start_polling() to begin command handling.
    """

    def __init__(self):
        self._enabled = False
        self._token = ""
        self._chat_id = ""
        self._notify_trades = True
        self._notify_signals = False
        self._notify_errors = True

        # Fee-aware PnL accounting (read from TradingConfig).
        # Defaults match OKX VIP 0 Global; OKX UAE Regular tier is ~5× higher.
        # Update these in Settings to reflect your actual venue/tier.
        self._spot_maker_bps    = 8.0
        self._spot_taker_bps    = 10.0
        self._futures_maker_bps = 2.0
        self._futures_taker_bps = 5.0
        self._entry_mode = "LIMIT"
        self._exit_mode  = "LIMIT"

        # Callbacks: set by app.py after engine is available
        self.get_status_cb: Optional[Callable[[], Dict[str, Any]]] = None
        self.get_trades_cb: Optional[Callable[[], list]] = None
        self.get_balance_cb: Optional[Callable[[], Dict[str, Any]]] = None
        self.get_config_cb: Optional[Callable[[], Dict[str, Any]]] = None
        self.close_all_cb: Optional[Callable[[], Dict[str, Any]]] = None
        # Restart the whole program (the "it's stuck" button). Signature:
        # restart_cb() -> Dict. Re-launches the process; an open position is
        # left on the exchange and re-adopted from the DB on startup.
        self.restart_cb: Optional[Callable[[], Dict[str, Any]]] = None
        # Returns result of SignalGenerator.optimize_parameters()
        self.optimize_cb: Optional[Callable[[], Dict[str, Any]]] = None
        # Flip algo_enabled on/off from Telegram (panic switch).
        # Signature: toggle_algo_cb(enabled: bool) -> bool (new state).
        self.toggle_algo_cb: Optional[Callable[[bool], bool]] = None
        # Update a single config field from Telegram.
        # Signature: set_config_cb(key: str, value) -> Dict with 'ok', 'message'.
        self.set_config_cb: Optional[Callable[[str, Any], Dict[str, Any]]] = None
        # Shadow "what-if-held" summary (same shape as /api/shadow-summary):
        # aggregate + 'tracking' (live watches) + 'recent' (completed verdicts).
        self.shadow_cb: Optional[Callable[[], Dict[str, Any]]] = None
        # Full analysis stats for /stats — get_trade_statistics() (win rate,
        # reward:risk, profit factor, expectancy, edge quality) plus the
        # portfolio max/current drawdown ($ and % of equity).
        self.stats_cb: Optional[Callable[[], Dict[str, Any]]] = None

        # Pending /set input: key waiting for a value message.
        self._pending_set_key: Optional[str] = None

        # Polling state
        self._poll_thread: Optional[threading.Thread] = None
        self._polling = False
        self._last_update_id = 0

        # Token the command menu was last registered under (via setMyCommands).
        # None until the first successful registration; compared against the
        # live token each poll cycle so a token change re-registers the menu.
        self._menu_token: Optional[str] = None

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def update_config(self, config) -> None:
        """Update from a TradingConfig object."""
        self._enabled = getattr(config, 'telegram_enabled', False)
        self._token = getattr(config, 'telegram_bot_token', '')
        self._chat_id = getattr(config, 'telegram_chat_id', '')
        self._notify_trades = getattr(config, 'telegram_notify_trades', True)
        self._notify_signals = getattr(config, 'telegram_notify_signals', False)
        self._notify_errors = getattr(config, 'telegram_notify_errors', True)

        # Fee + execution-mode config drives the "Est. Fees" line in exit
        # notifications. Previously hardcoded to 0.20% — that under-reports
        # by ~5× on OKX UAE Regular tier (real round-trip is ~1.00%).
        legacy_maker = getattr(config, 'maker_fee_bps', 2.0)
        legacy_taker = getattr(config, 'taker_fee_bps', 5.0)
        self._spot_maker_bps    = float(getattr(config, 'spot_maker_fee_bps',    legacy_maker))
        self._spot_taker_bps    = float(getattr(config, 'spot_taker_fee_bps',    legacy_taker))
        self._futures_maker_bps = float(getattr(config, 'futures_maker_fee_bps', legacy_maker))
        self._futures_taker_bps = float(getattr(config, 'futures_taker_fee_bps', legacy_taker))
        order_mode = getattr(config, 'order_execution_mode', 'LIMIT')
        self._entry_mode = getattr(config, 'entry_execution_mode', order_mode)
        self._exit_mode  = getattr(config, 'exit_execution_mode',  order_mode)

    def is_ready(self) -> bool:
        return bool(self._enabled and self._token and self._chat_id)

    def _estimate_round_trip_fees(self, notional_usd: float) -> tuple:
        """
        Estimate a round-trip's fee cost from the configured fee schedule.

        Returns (fee_usd, fee_bps_total) where fee_bps_total is the sum of
        all four legs (spot entry + spot exit + futures entry + futures exit)
        based on the entry/exit execution modes.

        For LIMIT exits we assume maker fills (best case). Actual fills can
        be takers if the price crosses the spread mid-cycle, so this is a
        lower bound — but it's the same convention `core/signals.py` uses
        to evaluate trade profitability, keeping the two reports consistent.
        """
        spot_entry  = self._spot_maker_bps    if self._entry_mode == "LIMIT" else self._spot_taker_bps
        fut_entry   = self._futures_maker_bps if self._entry_mode == "LIMIT" else self._futures_taker_bps
        spot_exit   = self._spot_maker_bps    if self._exit_mode  == "LIMIT" else self._spot_taker_bps
        fut_exit    = self._futures_maker_bps if self._exit_mode  == "LIMIT" else self._futures_taker_bps
        total_bps = spot_entry + fut_entry + spot_exit + fut_exit
        # notional_usd is the COMBINED two-leg notional, and total_bps sums all
        # four per-leg fills. Each leg's fee applies to ~half that combined
        # notional (the legs are ~equal, delta-neutral), so divide by 2 — else
        # the four per-leg rates get charged on the full two-leg notional and the
        # estimate DOUBLES (live #104: a real +$1.77 win was shown as -$0.18 net).
        fee_usd = notional_usd * total_bps / 10000.0 / 2.0
        # Report the round-trip cost as bps of the full notional so the $ and the
        # bps label reconcile: fee_usd == notional_usd * fee_bps_roundtrip / 1e4.
        fee_bps_roundtrip = total_bps / 2.0
        return fee_usd, fee_bps_roundtrip

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    @staticmethod
    def _geometry_rows(R, levels, target_usd, stop_usd, gate_usd):
        """Rows for the trade's exit geometry: absolute BE/EX/TP/SL spread
        levels plus the net dollar value each level equals (BE = $0 by
        definition; SL is quoted as the GROSS move that trips the stop)."""
        rows = []
        lv = levels or {}
        if lv.get("break_even") is None:
            return rows

        def _f(x):
            return f"{x:.2f}" if isinstance(x, (int, float)) else "off"

        fav = "↑" if lv.get("favorable") == "up" else "↓"
        show_ex = isinstance(gate_usd, (int, float)) and gate_usd > 0.005
        chips = [f"BE {_f(lv.get('break_even'))}"]
        if show_ex:
            chips.append(f"EX {_f(lv.get('gate_release'))}")
        chips.append(f"TP {_f(lv.get('take_profit'))}")
        chips.append(f"SL {_f(lv.get('stop'))}")
        rows.append(R("Levels", " · ".join(chips) + f"  (profit {fav})"))

        usd = ["BE $0.00"]
        if show_ex:
            usd.append(f"EX +${gate_usd:.2f}")
        usd.append(f"TP +${target_usd:.2f}" if (target_usd or 0) > 0 else "TP off")
        usd.append(f"SL -${stop_usd:.2f} gross" if (stop_usd or 0) > 0 else "SL off")
        rows.append(R("Level P&L", " · ".join(usd)))
        return rows

    def notify_trade_entry(self, trade, signal=None, details=None) -> None:
        """Send a trade entry notification.

        details (optional, provided by the engine): {'levels': BE/EX/TP/SL
        dict, 'target_usd', 'stop_usd', 'gate_usd', 'capital'} — the same
        exit geometry as the dashboard position card, so the trade can be
        followed from Telegram alone.
        """
        if not self.is_ready() or not self._notify_trades:
            return
        try:
            direction = trade.position_type
            C = 13
            SEP = "\u2500" * 24

            entry_str = (
                trade.entry_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                if trade.entry_time else "—"
            )
            placed_str = (
                trade.entry_placed_at.strftime("%H:%M:%S.%f")[:-3] + " UTC"
                if trade.entry_placed_at else ("simulated" if trade.is_paper else "—")
            )
            filled_str = (
                trade.entry_filled_at.strftime("%H:%M:%S.%f")[:-3] + " UTC"
                if trade.entry_filled_at else ("simulated" if trade.is_paper else "—")
            )
            latency_str = (
                f"{trade.entry_latency_ms:.0f} ms"
                if trade.entry_latency_ms is not None else ("simulated" if trade.is_paper else "—")
            )

            spread_bps = 0.0
            if trade.entry_spot_price > 0:
                spread_bps = (trade.entry_spread / trade.entry_spot_price) * 10000

            leverage_x = round(trade.notional_usd / trade.margin_usd) if trade.margin_usd > 0 else 0
            margin_str = (
                f"${trade.margin_usd:,.2f}  ({leverage_x}x)"
                if leverage_x > 0 else f"${trade.margin_usd:,.2f}"
            )

            R = self._R
            rows = [
                R("ID", f"#{trade.id or 'pending'}"),
                R("Entry Time", entry_str),
                "",
                R("Lots", f"{trade.quantity:.6f} {trade.asset}"),
                R("Notional", f"${trade.notional_usd:,.2f}"),
                R("Margin Req", margin_str),
                "",
                R("Spot Entry", f"${trade.entry_spot_price:,.4f}"),
                R("Fut Entry", f"${trade.entry_futures_price:,.4f}"),
                R("Spread", f"{trade.entry_spread:+.4f}  ({spread_bps:+.2f} bps)"),
                "",
                R("Z-score", f"{trade.entry_zscore:+.4f}"),
            ]
            if signal:
                std = getattr(signal, 'spread_std', None)
                spread_mean = getattr(signal, 'spread_mean', None)
                hurst = getattr(signal, 'hurst', None)
                hurst_ok = getattr(signal, 'hurst_ok', None)
                regime = getattr(signal, 'regime', None)
                hl = getattr(signal, 'half_life', None)
                if std is not None:
                    rows.append(R("Spread SD", f"{std:.6f}"))
                if spread_mean is not None:
                    rows.append(R("Spread Mean", f"{spread_mean:+.4f}"))
                if hurst is not None:
                    hurst_tag = "  [mean-rev]" if hurst_ok else "  [trending]" if hurst_ok is False else ""
                    rows.append(R("Hurst", f"{hurst:.4f}{hurst_tag}"))
                if hl is not None and hl != float('inf'):
                    rows.append(R("Half-Life", f"{hl:.1f} periods"))
                if regime:
                    rows.append(R("Regime", regime))
            # Exit geometry — the absolute levels this trade lives and dies at,
            # so it can be tracked from Telegram without the dashboard.
            if details:
                geo = self._geometry_rows(
                    R,
                    details.get("levels"),
                    details.get("target_usd") or 0.0,
                    details.get("stop_usd") or 0.0,
                    details.get("gate_usd"),
                )
                if geo:
                    rows.append("")
                    rows.extend(geo)
                cap = details.get("capital")
                if cap:
                    rows.append(R("Capital", f"${cap:,.2f} at risk"))
            spot_qty = getattr(trade, "spot_qty", 0) or 0
            if spot_qty > 0 and trade.quantity > 0:
                rows.append(R("Leg A Lots", f"{spot_qty:.6f}"))
                rows.append(R("Exec Ratio", f"{spot_qty / trade.quantity:.2f}"))
            # Surface the expected round-trip fee up front so the trader can
            # see at entry whether the spread captured is enough to overcome
            # costs. Same calculation used in the exit notification.
            est_fees, fee_bps_total = self._estimate_round_trip_fees(trade.notional_usd)
            breakeven_spread = (
                est_fees / trade.quantity if trade.quantity > 0 else 0.0
            )

            rows += [
                "",
                R("Est. Fees", f"-${est_fees:,.4f}  ({fee_bps_total:.1f} bps RT)"),
                R("Breakeven", f"{breakeven_spread:+.4f} spread move"),
                "",
                R("Orders at", placed_str),
                R("Filled at", filled_str),
                R("Latency", latency_str),
            ]
            parts = [
                f"<b>TRADE ENTRY  ·  {direction} {trade.asset}</b>",
                "\n".join(rows),
            ]
            if trade.is_paper:
                parts.append("<i>Paper Trading</i>")
            self._send("\n".join(parts))
        except Exception as e:
            logger.error("Error building trade entry notification: %s", e)

    @staticmethod
    def _outcome_tag(trade, stats) -> str:
        """One-line, rule-based verdict of what actually happened — exact and
        crisp, no LLM prose. The stop cases distinguish 'never reverted'
        (trend ran us over) from 'reverted but price never paid' (mean drift /
        spent edge), which is the distinction that matters for tuning."""
        reason = (trade.exit_reason or "").upper()
        pnl = trade.pnl_usd or 0.0
        if reason == "PROFIT_TARGET":
            return "TARGET HIT — banked on P&L, no z needed"
        if reason == "MAX_HOLD":
            return "TIME EXIT — profitable but slow, cut at max-hold"
        if reason == "EXIT":
            return ("REVERSION BANKED — z came home, gate satisfied" if pnl > 0
                    else "reversion exit below floor (gate released)")
        # stop family
        exit_thr = (stats or {}).get('exit_threshold', 0.5)
        z_entry = trade.entry_zscore or 0.0
        z_min = (stats or {}).get('z_min')
        z_max = (stats or {}).get('z_max')
        reverted = False
        if z_entry < 0 and z_max is not None:
            reverted = z_max >= -exit_thr
        elif z_entry > 0 and z_min is not None:
            reverted = z_min <= exit_thr
        if reverted:
            return ("STOPPED AFTER FULL REVERSION — z came home but price "
                    "never recovered BE (mean drift; edge was spent)")
        return "STOPPED IN TREND — z never reverted, divergence was real"

    def notify_trade_exit(self, trade, stats=None) -> None:
        """Send a trade exit notification with full P&L breakdown."""
        if not self.is_ready() or not self._notify_trades:
            return
        try:
            direction = trade.position_type
            exit_reason = trade.exit_reason or "EXIT"
            C = 14
            SEP = "\u2500" * 24

            exit_str = "—"
            duration_str = "—"
            if trade.exit_time:
                exit_str = trade.exit_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                if trade.entry_time:
                    total_sec = int((trade.exit_time - trade.entry_time).total_seconds())
                    if total_sec < 3600:
                        duration_str = f"{total_sec // 60}m {total_sec % 60}s"
                    elif total_sec < 86400:
                        duration_str = f"{total_sec // 3600}h {(total_sec % 3600) // 60}m"
                    else:
                        duration_str = f"{total_sec // 86400}d {(total_sec % 86400) // 3600}h"

            placed_str = (
                trade.exit_placed_at.strftime("%H:%M:%S.%f")[:-3] + " UTC"
                if trade.exit_placed_at else ("simulated" if trade.is_paper else "—")
            )
            filled_str = (
                trade.exit_filled_at.strftime("%H:%M:%S.%f")[:-3] + " UTC"
                if trade.exit_filled_at else ("simulated" if trade.is_paper else "—")
            )
            latency_str = (
                f"{trade.exit_latency_ms:.0f} ms"
                if trade.exit_latency_ms is not None else ("simulated" if trade.is_paper else "—")
            )

            entry_spread = trade.entry_spread
            exit_spread = trade.exit_spread
            spread_change = (
                (exit_spread - entry_spread) if direction == "SHORT"
                else (entry_spread - exit_spread)
            )
            # Gross PnL = spread move × qty (fee-free).
            gross_pnl = spread_change * trade.quantity
            # trade.pnl_usd is the engine's NET P&L (gross − fees). Use it as
            # the authoritative figure; the fee estimate here is for reference.
            est_fees, fee_bps_total = self._estimate_round_trip_fees(trade.notional_usd)
            net_pnl_est = gross_pnl - est_fees
            net_pct_est = (
                (net_pnl_est / trade.notional_usd) * 100
                if trade.notional_usd > 0 else 0.0
            )
            result = "PROFIT" if trade.pnl_usd >= 0 else "LOSS"

            fee_mode_label = (
                f"{self._entry_mode.lower()}→{self._exit_mode.lower()}, "
                f"{fee_bps_total:.1f} bps round-trip"
            )

            R = self._R
            rows = [
                R("Reason", exit_reason),
                R("Duration", duration_str),
                R("Exit Time", exit_str),
                "",
                R("Spot Entry", f"${trade.entry_spot_price:,.4f}"),
                R("Spot Exit", f"${trade.exit_spot_price:,.4f}"),
                R("Fut Entry", f"${trade.entry_futures_price:,.4f}"),
                R("Fut Exit", f"${trade.exit_futures_price:,.4f}"),
                "",
                R("Entry Spread", f"{entry_spread:+.4f}  (Z: {trade.entry_zscore:+.4f})"),
                R("Exit Spread", f"{exit_spread:+.4f}  (Z: {trade.exit_zscore:+.4f})"),
                R("Spread Chg", f"{spread_change:+.4f}"),
                "",
                R("Orders at", placed_str),
                R("Filled at", filled_str),
                R("Latency", latency_str),
                "",
                R("Gross PnL", f"${gross_pnl:+.4f}"),
                R("Est. Fees", f"-${est_fees:,.4f}  ({fee_mode_label})"),
                R("Net PnL (est)", f"${net_pnl_est:+.4f}  ({net_pct_est:+.4f}%)"),
                R("Engine Net PnL", f"${trade.pnl_usd:+.4f}"),
            ]
            # ── Crisp lifecycle analysis: exact numbers, rule-based verdict ──
            if stats:
                rows += ["", "<b>ANALYSIS</b>",
                         R("Outcome", self._outcome_tag(trade, stats))]
                # Which stop actually fired — dollar (capital cap) vs z backstop —
                # so the reason never needs forensic log reading.
                reason_u = (trade.exit_reason or "").upper()
                s_usd = stats.get('stop_usd') or 0
                s_z = stats.get('stop_z') or 0
                if reason_u == "DOLLAR_STOP" and s_usd:
                    rows.append(R("Stop type",
                                  f"DOLLAR stop — gross ≤ -${s_usd:.2f} (capital cap)"))
                elif reason_u in ("STOP_LOSS", "DAILY_LOSS") and (s_usd or s_z):
                    note = f"z-stop (|z| ≥ {s_z:g})" if s_z else "z-stop"
                    if s_usd:
                        note += (f" — dollar stop -${s_usd:.2f} NOT reached "
                                 f"(gross ${trade.pnl_gross_usd:+.2f})")
                    rows.append(R("Stop type", note))
                peak, trough = stats.get('peak_net'), stats.get('trough_net')
                if peak is not None and trough is not None:
                    pm, tm = stats.get('peak_min'), stats.get('trough_min')
                    pk = f"+${peak:.2f}" + (f" ({pm:.0f}m)" if pm is not None else "")
                    tr = f"{trough:+.2f}" + (f" ({tm:.0f}m)" if tm is not None else "")
                    rows.append(R("Peak/Trough", f"{pk} / {tr}"))
                avail = stats.get('available_usd') or 0
                if avail > 0:
                    cap_pct = (trade.pnl_gross_usd or 0) / avail * 100
                    rows.append(R("Capture", f"gross ${trade.pnl_gross_usd:+.2f} "
                                             f"of ${avail:.2f} avail ({cap_pct:+.0f}%)"))
                held, mh = stats.get('held_min') or 0, stats.get('max_hold_min') or 0
                hold_str = f"{held:.0f}m"
                if mh > 0:
                    hold_str += f"  (max {mh:.0f}m ×{held / mh:.1f})"
                rows.append(R("Hold", hold_str))
                z_min, z_max = stats.get('z_min'), stats.get('z_max')
                if z_min is not None and z_max is not None:
                    rows.append(R("Z path", f"{trade.entry_zscore:+.2f} → "
                                            f"{trade.exit_zscore:+.2f}  "
                                            f"(range {z_min:+.2f}…{z_max:+.2f})"))
                holds = stats.get('gate_holds') or 0
                if holds:
                    rows.append(R("Gate", f"held {holds}× over "
                                          f"{stats.get('gate_held_min', 0):.0f}m "
                                          f"(floor ${stats.get('gate_floor', 0):.2f})"))
            parts = [
                f"<b>TRADE EXIT  ·  {direction} {trade.asset}  ·  {result}</b>",
                "\n".join(rows),
            ]
            if trade.is_paper:
                parts.append("<i>Paper Trading</i>")
            self._send("\n".join(parts))
        except Exception as e:
            logger.error("Error building trade exit notification: %s", e)

    def notify_trade_analysis(self, trade_id: int, analysis: dict) -> None:
        """Send AI post-trade analysis to Telegram: exact NUMBERS first
        (system-computed scorecard), then a 1-2 line verdict, then recs."""
        if not self.is_ready() or not self._notify_trades:
            return
        try:
            esc             = html.escape
            health          = analysis.get("health_score", 0)
            conf            = analysis.get("confidence_score", 0)
            verdict         = (analysis.get("verdict") or analysis.get("summary")
                               or analysis.get("what_happened") or "—")[:400]
            scorecard       = analysis.get("scorecard") or []
            recs            = analysis.get("recommendations") or []

            if health >= 70:
                health_icon = "🟢"
            elif health >= 40:
                health_icon = "🟡"
            else:
                health_icon = "🔴"

            R = self._R
            SEP = "─" * 24

            # ── NUMBERS FIRST (exact scorecard) ──
            rows = [f"<b>🔬 AI Trade Review · #{trade_id}</b>", SEP]
            for item in scorecard:
                # _R now HTML-escapes label and value itself, so pass raw text
                # (double-escaping here would turn '&' into '&amp;amp;').
                rows.append(R(str(item.get("label", "")),
                              str(item.get("value", ""))))
            rows.append(R("Health", f"{health_icon} {health}/100  ·  conf {conf}/10"))

            # ── SETTINGS & LOGIC AUDIT (deterministic — the bug/misconfig catcher) ──
            # ── CRISP MODE: findings and recommendations as one-liners only.
            # The full rationale text stays in the dashboard analysis record —
            # Telegram gets numbers and headlines, not paragraphs.
            diags = analysis.get("diagnostics") or []
            shown = [d for d in diags if d.get("severity") in ("HIGH", "MED")]
            if shown:
                _ic = {"HIGH": "🔴", "MED": "🟠", "LOW": "⚪"}
                rows += ["", "<b>⚙️ Audit</b>"]
                for d in shown[:3]:
                    ic = _ic.get(d.get("severity"), "•")
                    finding = str(d.get('finding', ''))
                    head = finding.split(" — ")[0].split(". ")[0][:110]
                    rows.append(f"  {ic} <i>{esc(head)}</i>")
                _more = len(shown) - 3 + sum(1 for d in diags if d.get("severity") == "LOW")
                if _more > 0:
                    rows.append(f"  <i>+{_more} more (see dashboard)</i>")

            # ── One-sentence verdict ──
            v = verdict.split(". ")[0][:200]
            rows += ["", f"<b>Verdict</b>  <i>{esc(v)}</i>"]

            if recs:
                rows += ["", "<b>Recs</b>"]
                for rec in recs[:3]:
                    rtype = rec.get("type", "")
                    param = esc(str(rec.get("param", "")))
                    cur   = esc(str(rec.get("current_value", "")))
                    sug   = esc(str(rec.get("suggested_value", "")))
                    if rtype == "PARAMETER_CHANGE":
                        rows.append(f"  ⚙️ <code>{param}</code>: {cur} → <b>{sug}</b>")
                    elif rtype == "FILTER_TOGGLE":
                        rows.append(f"  🔀 <code>{param}</code> → <b>{sug}</b>")
                    elif rtype == "POSITION_SIZE_CHANGE":
                        rows.append(f"  📏 size ${cur} → <b>${sug}</b>")
                    elif rtype == "OBSERVATION":
                        head = esc((rec.get("rationale") or "")[:110])
                        rows.append(f"  💡 <i>{head}</i>")
                rows.append("  <i>full rationale on dashboard</i>")

            self._send("\n".join(rows))
        except Exception as e:
            logger.error("Error building trade analysis notification: %s", e)

    def notify_signal(self, signal) -> None:
        """Send a trading signal notification (non-NONE signals only)."""
        if not self.is_ready() or not self._notify_signals:
            return
        if not signal or signal.signal_type == "NONE":
            return
        try:
            sig_type = signal.signal_type
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            R = self._R
            rows = [
                R("Z-score", f"{signal.zscore:+.4f}"),
                R("Spread", f"{signal.spread:+.6f}"),
            ]
            spread_mean = getattr(signal, 'spread_mean', None)
            spread_std = getattr(signal, 'spread_std', None)
            hurst = getattr(signal, 'hurst', None)
            hurst_ok = getattr(signal, 'hurst_ok', None)
            std_ok = getattr(signal, 'std_filter_ok', None)
            if spread_mean is not None:
                rows.append(R("Spread Mean", f"{spread_mean:+.6f}"))
            if spread_std is not None:
                rows.append(R("Spread SD", f"{spread_std:.6f}"))
            if hurst is not None:
                hurst_tag = "  [mean-rev]" if hurst_ok else "  [trending]" if hurst_ok is False else ""
                rows.append(R("Hurst", f"{hurst:.4f}{hurst_tag}"))
            hl = getattr(signal, 'half_life', None)
            if hl is not None and hl != float('inf'):
                rows.append(R("Half-Life", f"{hl:.1f} periods"))
            filters = []
            if hurst_ok is not None:
                filters.append(f"hurst={'OK' if hurst_ok else 'FAIL'}")
            if std_ok is not None:
                filters.append(f"std={'OK' if std_ok else 'FAIL'}")
            if filters:
                rows.append(R("Filters", ", ".join(filters)))
            rows += [
                R("Regime", getattr(signal, 'regime', 'N/A')),
                R("Time", ts),
            ]
            self._send(
                f"<b>SIGNAL  ·  {sig_type}</b>\n" + "\n".join(rows)
            )
        except Exception as e:
            logger.error("Error building signal notification: %s", e)

    def notify_error(self, error_msg: str) -> None:
        """Send a system alert in a readable, wrapping form.

        The old format wrapped the whole body in <pre>, which Telegram renders
        as non-wrapping monospace — a long sentence ran off-screen on mobile.
        This renders normal (wrapping) text, a short header + time, and splits
        any 'Suggested action:' tail onto its own line.
        """
        if not self.is_ready() or not self._notify_errors:
            return
        try:
            ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
            body = (error_msg or "").strip()
            action = ""
            for marker in ("Suggested action:", "Suggested Action:", "Action:"):
                if marker in body:
                    body, _, action = body.partition(marker)
                    body, action = body.strip(), action.strip()
                    break
            rows = [f"⚠️ <b>System Alert</b>  ·  <i>{ts}</i>", "",
                    html.escape(body[:600])]
            if action:
                rows.append(f"\n💡 <b>Action:</b> {html.escape(action[:250])}")
            self._send("\n".join(rows))
        except Exception as e:
            logger.error("Error building error notification: %s", e)

    def notify_test(self) -> bool:
        """Send a test notification. Returns True if successful."""
        if not self._token or not self._chat_id:
            return False
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        C = 16
        cmd_rows = [
            f"{'/dashboard':<{C}}full system snapshot",
            f"{'/ping':<{C}}alive check (always responds)",
            f"{'/status':<{C}}engine &amp; algo state",
            f"{'/settings':<{C}}show all tunable settings",
            f"{'/set key val':<{C}}change a setting live",
            f"{'/pause':<{C}}halt new entries",
            f"{'/resume':<{C}}re-enable new entries",
            f"{'/positions':<{C}}open positions",
            f"{'/trades':<{C}}recent closed trades",
            f"{'/balance':<{C}}account balance",
            f"{'/pnl':<{C}}P&amp;L summary",
            f"{'/stats':<{C}}edge stats + max drawdown",
            f"{'/shadow':<{C}}what-if-held: did exits revert?",
            f"{'/eod':<{C}}end-of-day report",
            f"{'/closeall':<{C}}emergency: close all",
        ]
        msg = (
            "<b>Nexus Stat-Arb</b>\n"
            f"Connected.  <i>{ts}</i>\n"
            "<pre>" + "\n".join(cmd_rows) + "</pre>"
        )
        return send_telegram_message(self._token, self._chat_id, msg)

    # ------------------------------------------------------------------
    # Command Polling
    # ------------------------------------------------------------------

    def start_polling(self) -> None:
        """Start background thread that polls Telegram for commands."""
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._polling = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="telegram-poll",
            daemon=True,
        )
        self._poll_thread.start()
        logger.info("Telegram command polling started")

    def stop_polling(self) -> None:
        """Stop the command polling thread."""
        self._polling = False
        if self._poll_thread:
            self._poll_thread.join(timeout=3)
        logger.info("Telegram command polling stopped")

    def _register_command_menu(self) -> bool:
        """Publish BOT_COMMAND_MENU to Telegram via setMyCommands.

        This is what makes commands appear under the chat's "Menu" button and
        in the autocomplete popup when typing "/". Without it, only commands
        configured through BotFather (if any) are visible — /pause and /resume
        existed as handlers but were invisible in the menu. On failure the
        method leaves _menu_token unset so the poll loop retries next cycle.
        """
        try:
            url = f"{TELEGRAM_API_BASE.format(token=self._token)}/setMyCommands"
            payload = {"commands": [{"command": c, "description": d}
                                    for c, d in BOT_COMMAND_MENU]}
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200 and resp.json().get("ok"):
                self._menu_token = self._token
                logger.info("Telegram command menu registered (%d commands)",
                            len(BOT_COMMAND_MENU))
                return True
            logger.warning("Telegram setMyCommands failed (%d): %s",
                           resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("Telegram setMyCommands error: %s", e)
        return False

    def _poll_loop(self) -> None:
        """Long-poll Telegram getUpdates endpoint for commands."""
        while self._polling:
            if not self.is_ready():
                time.sleep(5)
                continue
            if self._menu_token != self._token:
                self._register_command_menu()
            try:
                url = f"{TELEGRAM_API_BASE.format(token=self._token)}/getUpdates"
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                }
                resp = requests.get(url, params=params, timeout=35)
                if resp.status_code != 200:
                    time.sleep(5)
                    continue

                data = resp.json()
                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]
                    self._handle_update(update)

            except requests.exceptions.Timeout:
                pass  # Normal for long-polling
            except Exception as e:
                logger.error("Telegram poll error: %s", e)
                time.sleep(10)

    def _handle_update(self, update: Dict[str, Any]) -> None:
        """Route an incoming Telegram update to the appropriate command handler."""
        msg = update.get("message", {})
        if not msg:
            return

        # Security: only respond to the authorised chat ID
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            logger.warning("Ignoring message from unknown chat_id: %s", chat_id)
            return

        text = msg.get("text", "").strip().lower()
        command = text.split("@")[0]  # Strip bot username suffix if present
        # The command TOKEN only (first word). Needed because a prefix test like
        # startswith("/set") also matches "/settings" — which used to swallow
        # /settings into the /set branch and show the usage text instead.
        cmd_word = command.split()[0] if command.split() else ""

        # If we're waiting for a free-text value after /set <key>, treat any
        # non-command message as the value input.
        if self._pending_set_key and not text.startswith("/"):
            try:
                self._apply_set(self._pending_set_key, msg.get("text", "").strip())
            except Exception as e:
                self._send(f"<b>Error</b>  <code>{html.escape(str(e))}</code>")
            finally:
                self._pending_set_key = None
            return

        handlers = {
            "/start": self._cmd_start,
            "/help": self._cmd_start,
            "/ping": self._cmd_ping,
            "/dashboard": self._cmd_dashboard,
            "/status": self._cmd_status,
            "/positions": self._cmd_positions,
            "/trades": self._cmd_trades,
            "/balance": self._cmd_balance,
            "/pnl": self._cmd_pnl,
            "/stats": self._cmd_stats,
            "/shadow": self._cmd_shadow,
            "/eod": self._cmd_eod,
            "/closeall": self._cmd_closeall,
            "/optimize": self._cmd_optimize,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/restart": self._cmd_restart,
            "/settings": self._cmd_settings,
        }

        # /set can carry the key inline (/set max_loss_usd 10) or just the key
        # (/set max_loss_usd — next message is the value). Exact-match the token
        # so "/settings" is NOT captured here.
        if cmd_word == "/set":
            try:
                parts = msg.get("text", "").strip().split(None, 2)
                if len(parts) >= 3:
                    self._apply_set(parts[1], parts[2])
                elif len(parts) == 2:
                    self._pending_set_key = parts[1]
                    safe_key = html.escape(parts[1])
                    self._send(f"Enter new value for <code>{safe_key}</code>:")
                else:
                    self._send("Usage: /set &lt;key&gt; &lt;value&gt;\nSend /settings to see all keys.")
            except Exception as e:
                self._send(f"<b>Error</b>  <code>{html.escape(str(e))}</code>")
            return

        handler = handlers.get(cmd_word)
        if handler:
            try:
                handler()
            except Exception as e:
                # Always show the user *something*. Escape special chars and
                # include the command name so a silent failure is impossible.
                logger.error("Telegram command handler error (%s): %s", command, e, exc_info=True)
                safe_cmd = html.escape(command)
                safe_err = html.escape(f"{type(e).__name__}: {e}")
                self._send(
                    f"<b>Error handling {safe_cmd}</b>\n<code>{safe_err}</code>\n"
                    "The full traceback is in the server log."
                )
        elif text.startswith("/"):
            self._send(
                "Unknown command. Available:\n"
                + " ".join(f"/{c}" for c, _ in BOT_COMMAND_MENU)
            )

    # ------------------------------------------------------------------
    # Command Handlers
    # ------------------------------------------------------------------

    def _cmd_ping(self) -> None:
        """Instant alive-check — answers without any engine/exchange call.

        If /ping responds but other commands don't, the bot's polling thread
        is alive and chat_id is correct — the issue is in a specific
        handler, not in connectivity.
        """
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        self._send(f"pong  ·  {ts}")

    def _cmd_start(self) -> None:
        """Handle /start and /help commands."""
        C = 16
        cmd_rows = [
            f"{'/dashboard':<{C}}full system snapshot",
            f"{'/status':<{C}}engine &amp; algo state",
            f"{'/positions':<{C}}open positions",
            f"{'/trades':<{C}}recent closed trades",
            f"{'/balance':<{C}}account balance",
            f"{'/pnl':<{C}}P&amp;L summary",
            f"{'/stats':<{C}}edge stats + max drawdown",
            f"{'/shadow':<{C}}what-if-held: did exits revert?",
            f"{'/eod':<{C}}end-of-day report",
            f"{'/optimize':<{C}}run parameter grid search",
            f"{'/settings':<{C}}show all tunable settings",
            f"{'/set key val':<{C}}change a setting live",
            f"{'/pause':<{C}}halt new entries",
            f"{'/resume':<{C}}re-enable new entries",
            f"{'/restart':<{C}}restart if stuck",
            f"{'/closeall':<{C}}emergency: close all",
        ]
        self._send(
            "<b>Nexus Stat-Arb Bot</b>\n"
            "Notifications active.\n"
            "<pre>" + "\n".join(cmd_rows) + "</pre>"
        )

    def _cmd_dashboard(self) -> None:
        """Handle /dashboard command — full system snapshot in one message."""
        status       = self.get_status_cb()   if self.get_status_cb   else {}
        balance_data = self.get_balance_cb()  if self.get_balance_cb  else {}
        trades       = self.get_trades_cb()   if self.get_trades_cb   else []
        cfg          = self.get_config_cb()   if self.get_config_cb   else {}
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        # Engine
        is_running   = status.get("is_running", False)
        algo_enabled = status.get("algo_enabled", False)
        paper        = status.get("paper_trading", True)
        asset        = status.get("asset", "N/A")
        position     = status.get("position", "NONE")
        error        = status.get("error", "")

        # Signal / spread state
        sig         = status.get("signal") or {}
        zscore      = sig.get("zscore", 0.0)
        spread      = sig.get("spread", 0.0)
        s_mean      = sig.get("spread_mean", 0.0)
        s_std       = sig.get("spread_std", 0.0)
        hurst       = sig.get("hurst", 0.0)
        hurst_ok    = sig.get("hurst_ok")
        std_ok      = sig.get("std_filter_ok")
        std_ratio   = sig.get("std_ratio")
        std_req     = sig.get("std_ratio_required", 0.0)
        regime      = sig.get("regime", "N/A")
        hl          = sig.get("half_life")
        sugg_lb     = sig.get("suggested_lookback")
        spread_slope = sig.get("spread_slope", 0.0)
        data_pts    = sig.get("data_points", 0)
        lookback    = sig.get("lookback", 0)
        data_ready  = sig.get("data_ready", False)
        last_block  = sig.get("last_blocked_signal") or {}

        # Ticks
        spot_tick   = status.get("spot_tick") or {}
        fut_tick    = status.get("futures_tick") or {}
        spot_last   = spot_tick.get("last", 0.0)
        fut_last    = fut_tick.get("last", 0.0)

        # Config
        spot_sym  = cfg.get("spot_symbol") or "Leg A"
        fut_sym   = cfg.get("futures_symbol") or "Leg B"
        notional  = cfg.get("position_size_usd") or 0
        entry_thr = cfg.get("entry_threshold") or 0
        exit_thr  = cfg.get("exit_threshold") or 0
        sl_thr    = cfg.get("stop_loss_threshold") or 0
        hurst_en  = cfg.get("hurst_enabled", False)
        std_en    = cfg.get("std_filter_enabled", False)
        trend_en  = cfg.get("trend_direction_filter", False)
        lb        = cfg.get("lookback_period") or lookback
        hedge_r   = cfg.get("hedge_ratio") or 1.0
        mode_lbl  = "PAPER" if paper else "LIVE"

        # Open trade
        open_trade = status.get("open_trade") or {}

        # Account
        equity    = balance_data.get("total_equity") or 0
        available = balance_data.get("available_margin") or 0
        upnl      = balance_data.get("unrealized_pnl") or 0
        exchange  = balance_data.get("exchange") or "N/A"
        is_demo   = balance_data.get("is_demo", paper)
        connected = balance_data.get("connected", False)
        acct_mode = "Demo" if is_demo else ("Paper" if paper else "Live")

        # Today's P&L (from closed trades)
        closed    = [t for t in trades if not t.get("is_open", True)]
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_t   = [t for t in closed if (t.get("exit_time") or "").startswith(today_str)]
        today_net = sum(t.get("pnl_usd", 0) or 0 for t in today_t)
        today_w   = sum(1 for t in today_t if (t.get("pnl_usd", 0) or 0) > 0)
        today_l   = len(today_t) - today_w

        R   = self._R
        D   = "─" * 20
        rows = []

        # ── Header ──────────────────────────────────────────────
        engine_state = "Running" if is_running else "STOPPED"
        algo_state   = "ON" if algo_enabled else "OFF"
        rows += [
            R("Engine", f"{engine_state}  ·  Algo {algo_state}  ·  {mode_lbl}"),
            R("Pair", f"{spot_sym} / {fut_sym}"),
        ]
        if error:
            rows.append(R("Error", error[:100]))

        # ── Market prices ────────────────────────────────────────
        rows += [f"<code>{D}</code>"]
        rows.append(R("Leg A now", f"${spot_last:,.4f}") if spot_last else R("Leg A now", "—"))
        rows.append(R("Leg B now", f"${fut_last:,.4f}") if fut_last else R("Leg B now", "—"))

        # ── Signal / stats ───────────────────────────────────────
        data_str = f"{data_pts}/{lookback}  ({'READY' if data_ready else 'COLLECTING'})"
        hl_str   = f"{hl:.1f} periods" if hl is not None else "—"
        if sugg_lb:
            hl_str += f"  (suggest LB {sugg_lb})"
        rows += [
            f"<code>{D}</code>",
            R("Data", data_str),
            R("Z-Score", f"{zscore:+.4f}"),
            R("Spread", f"{spread:+.6f}"),
            R("Mean", f"{s_mean:+.6f}"),
            R("Std Dev", f"{s_std:.6f}"),
            R("Regime", regime + (" ↑" if spread_slope > 0 else " ↓" if spread_slope < 0 else "")),
            R("Hurst", f"{hurst:.4f}"),
            R("Half-Life", hl_str),
        ]
        if last_block.get("reason"):
            blk_sig = last_block.get("would_be_signal", "?")
            blk_z   = last_block.get("zscore", 0.0)
            blk_ts  = (last_block.get("timestamp") or "")[:16].replace("T", " ")
            rows.append(R("Last Blocked",
                          f"{blk_sig} Z={blk_z:+.4f}  {blk_ts}"))
            rows.append(R("Block Reason", last_block["reason"][:80]))

        # ── Filters ──────────────────────────────────────────────
        def _fstr(enabled, ok):
            if not enabled:
                return "DISABLED"
            if ok is None:
                return "COLLECTING"
            return "PASS" if ok else "FAIL"

        hurst_f = _fstr(hurst_en, hurst_ok)
        std_f   = _fstr(std_en, std_ok)
        if std_en and std_ok is not None and std_ratio is not None:
            std_f += f"  ({std_ratio:.1f}x ≥ {std_req:.1f}x)"

        rows += [
            f"<code>{D}</code>",
            R("Hurst Filter", hurst_f),
            R("Edge Filter", std_f),
            R("Trend Filter", "ON" if trend_en else "OFF"),
        ]

        # ── Position ─────────────────────────────────────────────
        rows.append(f"<code>{D}</code>")
        if position == "NONE" or not open_trade:
            rows.append(R("Position", "Flat"))
        else:
            ot_qty    = open_trade.get("quantity") or 0
            ot_notl   = open_trade.get("notional_usd") or 0
            ot_espot  = open_trade.get("entry_spot_price") or 0
            ot_efut   = open_trade.get("entry_futures_price") or 0
            ot_esprn  = open_trade.get("entry_spread") or 0
            ot_ez     = open_trade.get("entry_zscore") or 0
            ot_margin = open_trade.get("margin_usd") or 0
            ot_time   = open_trade.get("entry_time") or ""
            lev_x     = round(ot_notl / ot_margin) if ot_margin > 0 else 0

            # Estimate live PnL from spread delta
            spr_delta = spread - ot_esprn
            est_pnl   = (-spr_delta if position == "LONG" else spr_delta) * ot_qty

            age_str = "—"
            if ot_time:
                try:
                    entry_dt = datetime.fromisoformat(ot_time.replace("Z", "+00:00"))
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    sec = int((datetime.now(timezone.utc) - entry_dt).total_seconds())
                    age_str = (
                        f"{sec//86400}d {(sec%86400)//3600}h" if sec >= 86400 else
                        f"{sec//3600}h {(sec%3600)//60}m"     if sec >= 3600  else
                        f"{sec//60}m {sec%60}s"
                    )
                except Exception:
                    pass

            notl_str = f"${ot_notl:,.2f}"
            if lev_x > 0:
                notl_str += f"  (margin ${ot_margin:,.2f} @ {lev_x}x)"

            rows += [
                R("Position", f"{position} {asset}"),
                R("Entry Z", f"{ot_ez:+.4f}  →  Now {zscore:+.4f}"),
                R("Entry Spot", f"${ot_espot:,.4f}  →  Now ${spot_last:,.4f}"),
                R("Entry Fut", f"${ot_efut:,.4f}  →  Now ${fut_last:,.4f}"),
                R("Entry Spread", f"{ot_esprn:+.6f}  →  Now {spread:+.6f}"),
                R("Spread Δ", f"{spr_delta:+.6f}"),
                R("Est. PnL", f"${est_pnl:+.4f}"),
                R("Notional", notl_str),
                R("Age", age_str),
            ]

        # ── Account ──────────────────────────────────────────────
        rows.append(f"<code>{D}</code>")
        if connected and equity:
            rows += [
                R("Exchange", f"{exchange}  ({acct_mode})"),
                R("Equity", f"${equity:,.2f}"),
                R("Available", f"${available:,.2f}"),
                R("Unrealized", f"${upnl:+.2f}"),
            ]
        else:
            rows.append(R("Account", "Not connected"))

        # ── Today ─────────────────────────────────────────────────
        rows += [
            f"<code>{D}</code>",
            R("Today Trades", f"{len(today_t)}  ({today_w}W / {today_l}L)"),
            R("Today Net PnL", f"${today_net:+.2f}"),
        ]

        # ── Config snapshot ───────────────────────────────────────
        if cfg:
            rows += [
                f"<code>{D}</code>",
                R("Notional", f"${notional:,.0f}"),
                R("Hedge Ratio β", f"{hedge_r:.4f}"),
                R("Entry ±Z", f"{entry_thr}"),
                R("Exit ±Z", f"{exit_thr}"),
                R("Stop ±Z", f"{sl_thr}"),
                R("Lookback", f"{lb}"),
            ]

        self._send(f"<b>DASHBOARD  ·  {ts}</b>\n" + "\n".join(rows))

    def _cmd_status(self) -> None:
        """Handle /status command."""
        status = self.get_status_cb() if self.get_status_cb else {}
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        is_running = status.get("is_running", False)
        algo_enabled = status.get("algo_enabled", False)
        paper = status.get("paper_trading", True)
        position = status.get("position", "NONE")
        asset = status.get("asset", "N/A")
        error = status.get("error", "")

        sig = status.get("signal") or {}
        zscore = sig.get("zscore", 0.0)
        regime = sig.get("regime", "N/A")
        slope  = sig.get("spread_slope", 0.0)
        hl = sig.get("half_life")
        suggested_lb = sig.get("suggested_lookback")

        slope_arrow = " ↑" if slope > 0 else " ↓" if slope < 0 else ""

        R = self._R
        rows = [
            R("Engine", "Running" if is_running else "Stopped"),
            R("Algo", "Enabled" if algo_enabled else "Disabled"),
            R("Mode", "Paper" if paper else "Live"),
            R("Asset", asset),
            R("Position", position),
            R("Z-score", f"{zscore:+.4f}"),
            R("Regime", regime + slope_arrow),
        ]
        if hl is not None:
            hl_str = f"{hl:.1f} periods"
            if suggested_lb:
                hl_str += f"  (suggest lookback {suggested_lb})"
            rows.append(R("Half-Life", hl_str))
        if error:
            rows += ["", R("Error", error[:200])]
        self._send(
            f"<b>SYSTEM STATUS  ·  {ts}</b>\n" + "\n".join(rows)
        )

    def _cmd_positions(self) -> None:
        """Handle /positions command."""
        status = self.get_status_cb() if self.get_status_cb else {}
        position = status.get("position", "NONE")
        open_trade = status.get("open_trade")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        if position == "NONE" or not open_trade:
            self._send(f"<b>OPEN POSITIONS  ·  {ts}</b>\nNo open positions.")
            return

        # `.get(key, default)` returns the DEFAULT only if the key is missing.
        # If the value IS None (common when a Trade field hasn't been populated
        # yet — e.g. exit fields on an open trade, or entry_zscore from a
        # recovered position), `.get()` returns None and f-string formatting
        # like f"{value:,.4f}" raises TypeError, which silently nukes the reply.
        # Normalize with `or 0` to force a numeric default and avoid the crash.
        asset = status.get("asset") or "N/A"
        entry_time = open_trade.get("entry_time") or "—"
        entry_spot = open_trade.get("entry_spot_price") or 0
        entry_fut = open_trade.get("entry_futures_price") or 0
        entry_spread = open_trade.get("entry_spread") or 0
        entry_z = open_trade.get("entry_zscore") or 0
        notional = open_trade.get("notional_usd") or 0
        qty = open_trade.get("quantity") or 0

        sig = status.get("signal") or {}
        current_z = sig.get("zscore") or 0.0
        current_spread = sig.get("spread") or 0.0

        margin_usd = open_trade.get("margin_usd") or 0
        entry_latency = open_trade.get("entry_latency_ms")
        placed_str = open_trade.get("entry_placed_at") or ("simulated" if open_trade.get("is_paper") else "—")
        if placed_str and len(placed_str) > 10:
            placed_str = placed_str[11:23] + " UTC"  # ISO → HH:MM:SS.mmm UTC
        filled_str = open_trade.get("entry_filled_at") or ("simulated" if open_trade.get("is_paper") else "—")
        if filled_str and len(filled_str) > 10:
            filled_str = filled_str[11:23] + " UTC"
        latency_str = f"{entry_latency:.0f} ms" if entry_latency is not None else "—"

        leverage_x = round(notional / margin_usd) if margin_usd > 0 else 0
        margin_str = f"${margin_usd:,.2f}  ({leverage_x}x)" if leverage_x > 0 else f"${margin_usd:,.2f}"

        spot_tick = status.get("spot_tick") or {}
        futures_tick = status.get("futures_tick") or {}
        current_spot = spot_tick.get("last", 0)
        current_fut = futures_tick.get("last", 0)

        R = self._R
        rows = [
            R("Position", f"{position} {asset}"),
            "",
            R("Lots", f"{qty:.6f} {asset}"),
            R("Notional", f"${notional:,.2f}"),
            R("Margin Req", margin_str),
            R("Entry Time", entry_time),
            "",
            R("Spot Entry", f"${entry_spot:,.4f}"),
            R("Fut Entry", f"${entry_fut:,.4f}"),
            R("Entry Spread", f"{entry_spread:+.4f}  (Z: {entry_z:+.4f})"),
            "",
        ]
        if current_spot:
            chg = f"  ({(current_spot - entry_spot) / entry_spot * 100:+.2f}%)" if entry_spot else ""
            rows.append(R("Spot Now", f"${current_spot:,.4f}{chg}"))
        if current_fut:
            chg = f"  ({(current_fut - entry_fut) / entry_fut * 100:+.2f}%)" if entry_fut else ""
            rows.append(R("Fut Now", f"${current_fut:,.4f}{chg}"))
        rows.append(R("Spread Now", f"{current_spread:+.4f}  (Z: {current_z:+.4f})"))

        # Live trade state — same numbers as the dashboard position card.
        delta = current_spread - entry_spread
        favorable = (position == "LONG" and delta < 0) or (position == "SHORT" and delta > 0)
        rows.append(R("Δ Spread", f"{delta:+.4f}  ({'favorable' if favorable else 'against'})"))
        upnl = open_trade.get("unrealized_pnl")
        if upnl is not None:
            rows.append(R("Net P&L", f"${upnl:+,.2f}"))

        target_usd = open_trade.get("exit_target_usd") or 0
        stop_usd = open_trade.get("exit_stop_usd") or 0
        gate_usd = open_trade.get("exit_gate_floor_usd")
        geo = self._geometry_rows(R, open_trade.get("spread_levels"),
                                  target_usd, stop_usd, gate_usd)
        if geo:
            rows.append("")
            rows.extend(geo)
        if target_usd or stop_usd:
            rows.append(R("Target/Stop",
                          (f"+${target_usd:.2f}" if target_usd else "—")
                          + "  /  "
                          + (f"-${stop_usd:.2f}" if stop_usd else "—")))

        held_min = open_trade.get("held_minutes")
        max_hold_min = open_trade.get("max_hold_minutes")
        if held_min is not None:
            hold_str = f"{held_min:.1f}m"
            if max_hold_min:
                hold_str += (f"  (max {max_hold_min:.0f}m"
                             + (" — EXPIRED" if held_min >= max_hold_min else "")
                             + ")")
            rows.append(R("Age", hold_str))

        spot_qty = open_trade.get("spot_qty") or 0
        if spot_qty > 0 and qty > 0:
            rows.append(R("Leg A Lots", f"{spot_qty:.6f}  (ratio {spot_qty / qty:.2f})"))

        rows += [
            "",
            R("Orders at", placed_str),
            R("Filled at", filled_str),
            R("Latency", latency_str),
        ]
        self._send(
            f"<b>OPEN POSITIONS  ·  {ts}</b>\n" + "\n".join(rows)
        )

    def _cmd_trades(self) -> None:
        """Handle /trades command - show 5 most recent closed trades with full detail."""
        trades = self.get_trades_cb() if self.get_trades_cb else []
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        closed = [t for t in trades if not t.get("is_open", True)][:5]

        if not closed:
            self._send(f"<b>RECENT TRADES  ·  {ts}</b>\nNo closed trades yet.")
            return

        R = self._R
        parts = [f"<b>RECENT TRADES  ·  {ts}</b>"]
        for t in closed:
            # pnl_usd is engine's NET (gross − fees); pnl_gross_usd is before fees.
            net_pnl  = t.get("pnl_usd", 0) or 0.0
            notional = t.get("notional_usd", 0) or 0.0
            net_pct  = (net_pnl / notional * 100) if notional > 0 else 0.0
            result   = "PROFIT" if net_pnl >= 0 else "LOSS"

            gross    = t.get("pnl_gross_usd") or 0.0
            fee_usd  = t.get("fees_usd") or 0.0
            # Older / paper trades may lack pnl_gross_usd; fall back to estimate.
            if not gross:
                fee_est, _ = self._estimate_round_trip_fees(notional)
                gross   = net_pnl + fee_est
                fee_usd = fee_est

            duration_str = "—"
            entry_t = t.get("entry_time")
            exit_t = t.get("exit_time")
            if entry_t and exit_t:
                try:
                    e = datetime.fromisoformat(entry_t)
                    x = datetime.fromisoformat(exit_t)
                    total_sec = int((x - e).total_seconds())
                    if total_sec < 3600:
                        duration_str = f"{total_sec // 60}m {total_sec % 60}s"
                    elif total_sec < 86400:
                        duration_str = f"{total_sec // 3600}h {(total_sec % 3600) // 60}m"
                    else:
                        duration_str = f"{total_sec // 86400}d {(total_sec % 86400) // 3600}h"
                except Exception:
                    pass

            entry_spread = t.get("entry_spread", 0)
            exit_spread = t.get("exit_spread", 0)
            entry_z = t.get("entry_zscore", 0)
            exit_z = t.get("exit_zscore", 0)
            entry_spot = t.get("entry_spot_price", 0)
            exit_spot = t.get("exit_spot_price", 0)
            entry_fut = t.get("entry_futures_price", 0)
            exit_fut = t.get("exit_futures_price", 0)
            entry_spread_bps = (entry_spread / entry_spot * 10000) if entry_spot else 0

            trade_rows = [
                f"<b>#{t.get('id')}  {t.get('position_type')} {t.get('asset')}  {result}</b>",
                R("Exit", t.get("exit_reason", "EXIT")),
                R("Duration", duration_str),
                "",
                R("Spot Entry", f"${entry_spot:,.4f}"),
                R("Spot Exit", f"${exit_spot:,.4f}"),
                R("Fut Entry", f"${entry_fut:,.4f}"),
                R("Fut Exit", f"${exit_fut:,.4f}"),
                "",
                R("Entry Spread", f"{entry_spread:+.4f}  ({entry_spread_bps:+.2f} bps)"),
                R("Exit Spread", f"{exit_spread:+.4f}"),
                R("Entry Z", f"{entry_z:+.4f}"),
                R("Exit Z", f"{exit_z:+.4f}"),
                "",
                R("Gross PnL", f"${gross:+.2f}"),
                R("Est. Fees", f"-${fee_usd:,.2f}"),
                R("Net PnL", f"${net_pnl:+.2f}  ({net_pct:+.2f}%)  {result}"),
            ]
            exit_lat = t.get("exit_latency_ms")
            if exit_lat is not None:
                trade_rows.append(R("Exit Latency", f"{exit_lat:.0f} ms"))
            parts.append("\n".join(trade_rows))

        self._send("\n\n".join(parts))

    def _cmd_balance(self) -> None:
        """Handle /balance command."""
        balance_data = self.get_balance_cb() if self.get_balance_cb else {}
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        if not balance_data or not balance_data.get("connected"):
            self._send(
                f"<b>ACCOUNT BALANCE  ·  {ts}</b>\n"
                "Exchange not connected or API keys not configured."
            )
            return

        # Same None-safety as _cmd_positions: a None value through .get(k, default)
        # is None (not default), and f-string formatting of None blows up.
        equity = balance_data.get("total_equity") or 0
        available = balance_data.get("available_margin") or 0
        margin_used = balance_data.get("margin_used") or 0
        margin_ratio = balance_data.get("margin_ratio") or 0
        upnl = balance_data.get("unrealized_pnl") or 0
        health = balance_data.get("margin_health") or "N/A"
        exchange = balance_data.get("exchange") or "N/A"
        mode = "Demo" if balance_data.get("is_demo") else "Live"

        R = self._R
        rows = [
            R("Exchange", f"{exchange}  ({mode})"),
            R("Equity", f"${equity:,.2f}"),
            R("Available", f"${available:,.2f}"),
            R("Used", f"${margin_used:,.2f}"),
            R("Margin", f"{margin_ratio:.1f}%  [{health}]"),
            R("Unrealized", f"${upnl:+.2f}"),
        ]
        self._send(
            f"<b>ACCOUNT BALANCE  ·  {ts}</b>\n" + "\n".join(rows)
        )

    def _cmd_pnl(self) -> None:
        """Handle /pnl command - comprehensive P&L summary.

        Aggregates use the engine's gross `pnl_usd` plus a fee-aware
        estimate derived from each trade's notional and the configured
        execution mode. Both gross and net-of-fees totals are reported
        so the user can see the gap caused by the venue's fee tier.
        """
        trades = self.get_trades_cb() if self.get_trades_cb else []
        balance_data = self.get_balance_cb() if self.get_balance_cb else {}
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        closed = [t for t in trades if not t.get("is_open", True)]

        # pnl_usd is engine's NET (gross − fees already deducted).
        # pnl_gross_usd / fees_usd are stored on the trade for the breakdown.
        def _net(t):
            return t.get("pnl_usd", 0) or 0.0

        def _gross(t):
            g = t.get("pnl_gross_usd") or 0.0
            if g:
                return g
            # Older/paper trades: back-calculate from net + estimated fees
            notional = t.get("notional_usd", 0) or 0.0
            fee_est, _ = self._estimate_round_trip_fees(notional)
            return _net(t) + fee_est

        def _fee(t):
            stored = t.get("fees_usd") or 0.0
            if stored:
                return stored
            notional = t.get("notional_usd", 0) or 0.0
            fee_est, _ = self._estimate_round_trip_fees(notional)
            return fee_est

        total_net   = sum(_net(t) for t in closed)
        total_gross = sum(_gross(t) for t in closed)
        total_fees  = sum(_fee(t) for t in closed)

        winners = [t for t in closed if _net(t) > 0]
        losers = [t for t in closed if _net(t) <= 0]

        win_rate = (len(winners) / len(closed) * 100) if closed else 0
        avg_win = (sum(_net(t) for t in winners) / len(winners)) if winners else 0
        avg_loss = (sum(_net(t) for t in losers) / len(losers)) if losers else 0

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_trades = [t for t in closed if (t.get("exit_time") or "").startswith(today)]
        today_net   = sum(_net(t) for t in today_trades)
        today_gross = sum(_gross(t) for t in today_trades)
        today_fees  = sum(_fee(t) for t in today_trades)
        upnl = balance_data.get("unrealized_pnl", 0)

        R = self._R
        rows = [
            R("Closed Trades", str(len(closed))),
            R("Win Rate", f"{win_rate:.1f}%  ({len(winners)}W / {len(losers)}L net)"),
            R("Avg Win (net)", f"${avg_win:+.2f}"),
            R("Avg Loss (net)", f"${avg_loss:+.2f}"),
            "",
            R("Today Gross", f"${today_gross:+.2f}  ({len(today_trades)} trades)"),
            R("Today Fees", f"-${today_fees:,.2f}"),
            R("Today Net", f"${today_net:+.2f}"),
            "",
            R("All-time Gross", f"${total_gross:+.2f}"),
            R("All-time Fees", f"-${total_fees:,.2f}"),
            R("All-time Net", f"${total_net:+.2f}"),
            R("Unrealized", f"${upnl:+.2f}"),
        ]
        self._send(
            f"<b>P&amp;L SUMMARY  ·  {ts}</b>\n" + "\n".join(rows)
        )

    def _cmd_stats(self) -> None:
        """Handle /stats — the full edge-and-drawdown analysis, the same numbers
        as the web Analysis page: win rate vs break-even, reward:risk, profit
        factor, expectancy, and the portfolio max/current drawdown."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        s = self.stats_cb() if self.stats_cb else None
        if not s:
            self._send(f"<b>ANALYSIS  ·  {ts}</b>\nStats not available.")
            return

        wr = s.get("win_rate", 0) or 0.0
        be_wr = s.get("breakeven_win_rate")
        wr_tag = "  (need >%.0f%%%s)" % (be_wr, " ✓" if wr >= be_wr else " ✗") if be_wr is not None else ""

        dd_usd = abs(s.get("max_drawdown_usd", 0) or 0.0)
        dd_pct = s.get("max_drawdown_pct")
        dd_tail = f"  ({dd_pct:.1f}% of equity)" if dd_pct is not None else ""
        cur_dd = abs(s.get("current_drawdown_usd", 0) or 0.0)

        exp_r = s.get("expectancy_r")
        pf = s.get("profit_factor")
        rr = s.get("reward_risk")
        aw = s.get("avg_win", 0) or 0.0
        al = s.get("avg_loss", 0) or 0.0     # stored as positive magnitude
        mw = s.get("max_win", 0) or 0.0
        ml = s.get("max_loss", 0) or 0.0     # negative

        R = self._R
        rows = [
            R("Trades", f"{s.get('total_trades', 0)}  ({s.get('winning_trades', 0)}W / {s.get('losing_trades', 0)}L)"),
            R("Win Rate", f"{wr:.1f}%{wr_tag}"),
            R("Expectancy", f"{exp_r:+.2f} R" if exp_r is not None else "—"),
            R("Profit Factor", f"{pf:.2f}" if pf is not None else "—"),
            R("Reward:Risk", f"{rr:.2f}" if rr is not None else "—"),
            R("Avg W / L", f"${aw:.2f} / -${al:.2f}"),
            R("Best / Worst", f"${mw:.2f} / -${abs(ml):.2f}"),
            "",
            R("Max Drawdown", f"-${dd_usd:.2f}{dd_tail}"),
            R("Current DD", f"-${cur_dd:.2f}"),
            R("All-time Net", f"${s.get('total_pnl', 0) or 0.0:+.2f}"),
        ]
        self._send(f"<b>📊 ANALYSIS  ·  {ts}</b>\n" + "\n".join(rows))

    def _cmd_shadow(self) -> None:
        """Handle /shadow — the 'what-if-held' scenarios: of the trades we
        exited, would they have reverted if held? Live in-progress watches
        (their current held P&L) plus recent finished verdicts, and the
        aggregate revert rates (with a small-sample caveat)."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        s = self.shadow_cb() if self.shadow_cb else None
        if not s:
            self._send(f"<b>SHADOW · {ts}</b>\nWhat-if data not available.")
            return

        R = self._R

        def _money(v):
            if v is None:
                return "—"
            return ("+$" if v >= 0 else "−$") + f"{abs(v):.2f}"

        def _mins(v):
            return f"{round(v)}m" if v is not None else "—"

        def _reason(v):
            # "TRAILING_STOP" → "Trailing Stop" — readable, and escaped.
            return html.escape(str(v or "—").replace("_", " ").title())

        def _verdict(v):
            # Compact, scannable label for the stored verdict string
            # ("REVERTED TO TARGET" / "REVERTED TO BREAK-EVEN" / "KEPT BLEEDING").
            u = (v or "").upper()
            if "TARGET" in u:
                return "✓ hit target"
            if "BREAK" in u or "B/E" in u:
                return "~ back to B/E"
            if "BLEED" in u:
                return "✗ kept bleeding"
            return html.escape(str(v or "—"))

        done     = s.get("count", 0) or 0
        tracking = s.get("tracking", []) or []
        recent   = s.get("recent", []) or []
        active   = s.get("active", len(tracking))

        lines = [
            f"<b>🔮 SHADOW · WHAT-IF-HELD</b>  ·  {ts}",
            "<i>Of the exits we took, would holding have reverted them?</i>",
            "",
            f"<b>{done}</b> completed  ·  <b>{active}</b> live",
        ]

        # ── Scorecard — revert rates over the CUT trades (stops / sub-target
        # exits); a target exit trivially "hits target" so those are excluded.
        lines.append("\n<b>▸ Scorecard</b>")
        if done == 0:
            lines.append("<i>No completed watches yet — rows finalize on "
                         "target-hit or after 8 h.</i>")
        else:
            pct_t = round((s.get("revert_target_rate", 0) or 0) * 100)
            pct_b = round((s.get("revert_be_rate", 0) or 0) * 100)
            rt = s.get("reverted_target", 0) or 0
            rb = s.get("reverted_be", 0) or 0
            lines += [
                R("Hit target if held", f"{rt}/{done}  ({pct_t}%)  med {_mins(s.get('median_target_min'))}"),
                R("Back to B/E if held", f"{rb}/{done}  ({pct_b}%)  med {_mins(s.get('median_be_min'))}"),
                R("Avg peak if held", _money(s.get("avg_peak_usd"))),
            ]
            if done < 5:
                lines.append(f"<i>Only {done} completed — too few to read as a "
                             "rate; judge the rows below.</i>")
            elif pct_t >= 50:
                lines.append(f"<i>{pct_t}% would have reached target — exits may "
                             "be too early.</i>")
            else:
                lines.append(f"<i>{pct_t}% reached target — cutting looks "
                             "justified.</i>")

        # ── TP overshoot — did the spread keep running PAST the target after we
        # booked it? (Answers "is my TP set too low?".)
        tp_n = s.get("tp_count", 0) or 0
        if tp_n:
            ran = s.get("tp_ran_past", 0) or 0
            avg_x = s.get("tp_avg_extra_usd", 0.0) or 0.0
            max_x = s.get("tp_max_extra_usd", 0.0) or 0.0
            lines += [
                "\n<b>▸ TP overshoot</b>",
                R("TP exits", f"{tp_n}"),
                R("Ran past target", f"{ran} of {tp_n}"),
                R("Avg beyond TP", f"+${avg_x:.2f}"),
                R("Max beyond TP", f"+${max_x:.2f}"),
            ]
            if max_x > 0.01:
                lines.append("<i>Large overshoot = your TP is set too low.</i>")

        # ── Live in-progress watches — current held P&L, marked to latest mids.
        if tracking:
            lines.append(f"\n<b>▸ Tracking now</b>  ({len(tracking)})")
            for t in tracking[:6]:
                flags = ("BE✓" if t.get("hit_be") else "BE·") + " " + \
                        ("TP✓" if t.get("hit_target") else "TP·")
                lines.append(
                    f"<code>#{t.get('trade_id')}</code> {_reason(t.get('exit_reason'))} "
                    f"· {_mins(t.get('mins_since_entry'))}")
                lines.append(
                    f"   exit {_money(t.get('exit_net_usd'))} → now "
                    f"<b>{_money(t.get('current_net_usd'))}</b> · "
                    f"peak {_money(t.get('peak_net_usd'))} · {flags}")

        # ── Recent finished verdicts — what actually happened if we'd held.
        if recent:
            lines.append("\n<b>▸ Recent verdicts</b>")
            for r in recent[:6]:
                tail = ""
                if r.get("hit_target") and r.get("hit_target_min") is not None:
                    tail = f" · in {_mins(r.get('hit_target_min'))}"
                elif r.get("hit_break_even") and r.get("hit_be_min") is not None:
                    tail = f" · in {_mins(r.get('hit_be_min'))}"
                lines.append(
                    f"<code>#{r.get('trade_id')}</code> {_verdict(r.get('verdict'))} · "
                    f"held {_money(r.get('final_net_usd'))} · "
                    f"peak {_money(r.get('peak_net_usd'))}{tail}")

        self._send("\n".join(lines))

    def _cmd_eod(self) -> None:
        """Handle /eod command - end-of-day summary."""
        trades = self.get_trades_cb() if self.get_trades_cb else []
        balance_data = self.get_balance_cb() if self.get_balance_cb else {}
        status = self.get_status_cb() if self.get_status_cb else {}
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        closed = [t for t in trades if not t.get("is_open", True)]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_closed = [t for t in closed if (t.get("exit_time") or "").startswith(today)]
        today_pnl = sum(t.get("pnl_usd", 0) for t in today_closed)
        today_wins = sum(1 for t in today_closed if t.get("pnl_usd", 0) > 0)

        equity = balance_data.get("total_equity", 0)
        upnl = balance_data.get("unrealized_pnl", 0)
        position = status.get("position", "NONE")
        asset = status.get("asset", "N/A")

        sig = status.get("signal") or {}
        regime = sig.get("regime", "N/A")
        zscore = sig.get("zscore", 0.0)

        R = self._R
        rows = [
            R("Trades", f"{len(today_closed)}  ({today_wins} wins)"),
            R("Net PnL", f"${today_pnl:+.2f}"),
            "",
            R("Equity", f"${equity:,.2f}"),
            R("Unrealized", f"${upnl:+.2f}"),
            "",
            R("Position", f"{position}  ({asset})"),
            R("Z-score", f"{zscore:+.4f}"),
            R("Regime", regime),
        ]
        self._send(
            f"<b>END OF DAY  ·  {ts}</b>\n" + "\n".join(rows)
        )

    def _cmd_pause(self) -> None:
        """Disable algo (no new entries). Open positions continue to be monitored."""
        self._toggle_algo(False, "PAUSE ALGO")

    def _cmd_resume(self) -> None:
        """Re-enable algo (new entries allowed again)."""
        self._toggle_algo(True, "RESUME ALGO")

    def _toggle_algo(self, target: bool, title: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        if not self.toggle_algo_cb:
            self._send(f"<b>{title}  ·  {ts}</b>\n<pre>Not configured.</pre>")
            return
        try:
            new_state = bool(self.toggle_algo_cb(target))
            R = self._R
            rows = [
                R("Algo", "ON" if new_state else "OFF"),
                R("Effect", "New entries enabled" if new_state else "New entries blocked"),
            ]
            self._send(f"<b>{title}  ·  {ts}</b>\n" + "\n".join(rows))
        except Exception as e:
            self._send(f"<b>{title}  ·  {ts}</b>\n<b>Error</b>  <code>{e}</code>")

    def _cmd_closeall(self) -> None:
        """Handle /closeall emergency command - immediately close all open positions."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        if not self.close_all_cb:
            self._send(
                f"<b>EMERGENCY CLOSE  ·  {ts}</b>\n"
                "<pre>Not configured.</pre>"
            )
            return
        try:
            result = self.close_all_cb()
            R = self._R
            if result.get("success"):
                rows = [R("Status", "Executed")]
                closed_count = result.get("closed_count")
                if closed_count is not None:
                    rows.append(R("Closed", f"{closed_count} position(s)"))
                detail = result.get("message", "")
                if detail:
                    rows.append(R("Info", detail[:120]))
            else:
                rows = [
                    R("Status", "FAILED"),
                    R("Error", result.get("error", "Unknown error")[:120]),
                ]
            self._send(
                f"<b>EMERGENCY CLOSE  ·  {ts}</b>\n" + "\n".join(rows)
            )
        except Exception as e:
            self._send(
                f"<b>EMERGENCY CLOSE  ·  {ts}</b>\n"
                f"<b>Error</b>  <code>{e}</code>"
            )

    def _cmd_restart(self) -> None:
        """Restart the whole program — the 'it's stuck' button. Re-launches the
        process; any open position is left on the exchange and re-adopted from
        the DB on startup, so its stop/target resume automatically."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        if not self.restart_cb:
            self._send(f"<b>♻️ RESTART  ·  {ts}</b>\n<pre>Not configured.</pre>")
            return
        # Send the confirmation BEFORE triggering, because the process is about
        # to be replaced — the reply won't go out afterwards.
        self._send(
            f"<b>♻️ RESTART  ·  {ts}</b>\n"
            "Re-launching now — back in ~15–30s.\n"
            "Any open position stays on the exchange and is re-adopted on "
            "startup (stop &amp; target resume).\n"
            "Send <code>/ping</code> in a minute — if it answers, we're back. "
            "If it stays silent, the host itself is hung (needs the watchdog)."
        )
        try:
            self.restart_cb()
        except Exception as e:
            self._send(f"<b>♻️ RESTART</b>\n<b>Error</b>  <code>{html.escape(str(e))}</code>")

    def _cmd_optimize(self) -> None:
        """Run parameter optimisation and report the result."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        if not self.optimize_cb:
            self._send(f"<b>PARAMETER OPTIMISATION  ·  {ts}</b>\nNot configured.")
            return
        self._send(f"<b>PARAMETER OPTIMISATION  ·  {ts}</b>\nRunning grid search — please wait...")
        try:
            result = self.optimize_cb()
        except Exception as e:
            self._send(f"<b>PARAMETER OPTIMISATION  ·  {ts}</b>\n<b>Error</b>  <code>{e}</code>")
            return

        if "error" in result:
            self._send(f"<b>PARAMETER OPTIMISATION  ·  {ts}</b>\n<b>Error</b>  <code>{result['error']}</code>")
            return

        R = self._R
        hl = result.get("half_life")
        suggested_lb = result.get("suggested_lookback")
        test_pnl = result.get("test_pnl", 0)
        validated = "Generalises" if test_pnl > 0 else "Does not generalise"
        rows = [
            R("Best Lookback", str(result.get("best_lookback", "N/A"))),
            R("Best Threshold", f"{result.get('best_threshold', 0):.2f}"),
            "",
            R("Train PnL", f"{result.get('train_pnl', 0):+.4f}  ({result.get('train_size', 0)} pts)"),
            R("Test PnL", f"{test_pnl:+.4f}  ({result.get('test_size', 0)} pts)"),
            R("Out-of-sample", validated),
            "",
        ]
        if hl is not None:
            rows.append(R("Half-Life", f"{hl:.1f} periods"))
        if suggested_lb is not None:
            rows.append(R("HL Suggestion", f"lookback ≈ {suggested_lb}  (2.5x HL)"))
        self._send(f"<b>PARAMETER OPTIMISATION  ·  {ts}</b>\n" + "\n".join(rows))

    # ------------------------------------------------------------------
    # Settings commands
    # ------------------------------------------------------------------

    # Keys that can be changed via /set, with their human label and type.
    _SETTABLE: Dict[str, tuple] = {
        # (display_label, type_fn, unit_hint)
        "max_loss_usd":          ("Dollar Stop",         float, "USD"),
        "profit_target_usd":     ("Profit Target",       float, "USD"),
        "position_size_usd":     ("Position Size",       float, "USD"),
        "entry_threshold":       ("Entry Z-score",       float, "σ"),
        "exit_threshold":        ("Exit Z-score",        float, "σ"),
        "stop_loss_threshold":   ("Z-score Stop",        float, "σ"),
        "max_hold_minutes":      ("Max Hold",            float, "min"),
        "hard_max_hold_minutes": ("Hard Max Hold",       float, "min"),
        "daily_max_loss_usd":    ("Daily Loss Limit",    float, "USD"),
        "lookback_period":       ("Lookback",            int,   "bars"),
        "hedge_ratio":           ("Hedge Ratio β",       float, ""),
        "min_entry_rr_multiple": ("Min R:R",             float, "×"),
        "stop_loss_capital_pct": ("Stop % of Capital",   float, "%"),
        "rfq_notional_threshold_usd": ("RFQ Min Notional", float, "USD"),
        "rfq_max_markup_bps":    ("RFQ Max Markup",       float, "bps"),
        # ── exit tuning (the overrides that can cut a trade before TP/EX) ──
        # max_hold has TWO drivers: zero BOTH to fully disable it (the
        # ×half-life form takes precedence over the minutes fallback).
        "max_hold_halflife_mult": ("Max Hold ×half-life", float, "×"),
        "trailing_stop_pct":     ("Trailing Stop",       float, "%"),
        "trailing_stop_floor_pct": ("Trailing Floor",    float, "%"),
        "exit_profit_gate_pct":  ("Exit Profit Gate",    float, "%"),
        # Profit target: the %-of-capital form is what actually governs TP
        # (it takes precedence over profit_target_usd). The cost floor RAISES TP
        # to N× the round-trip cost when > 0 — set 0 to keep TP at the %-target.
        "profit_target_capital_pct": ("Profit Target %cap", float, "%"),
        "profit_target_min_cost_mult": ("Profit Cost Floor", float, "×"),
        "exit_signal_mode":      ("Exit Mode",           _parse_exit_mode, ""),
        "z_stop_exit_enabled":   ("Z-Stop Exit",         _parse_onoff, ""),
    }

    def _cmd_settings(self) -> None:
        """Show all tunable settings with current values."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        cfg = self.get_config_cb() if self.get_config_cb else {}
        if not cfg:
            self._send(f"<b>SETTINGS  ·  {ts}</b>\nConfig not available.")
            return
        R = self._R
        rows = []
        for key, (label, _, unit) in self._SETTABLE.items():
            val = cfg.get(key)
            if val is None:
                val_str = "—"
            elif isinstance(val, float):
                val_str = f"{val:g} {unit}".strip()
            else:
                val_str = f"{val} {unit}".strip()
            # Keep the key OUTSIDE the <code> value — Telegram HTML rejects tags
            # nested inside <code>, which silently drops the whole message to
            # unformatted plain text (raw tags shown to the user).
            rows.append(R(label, val_str) + f"  <i>{html.escape(key)}</i>")
        rows += [
            "",
            "<i>To change: /set &lt;key&gt; &lt;value&gt;</i>",
            "<i>Example: /set max_loss_usd 10</i>",
        ]
        self._send(f"<b>SETTINGS  ·  {ts}</b>\n" + "\n".join(rows))

    def _apply_set(self, key: str, raw_value: str) -> None:
        """Parse key/value, validate, call set_config_cb, and report result."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        key = key.strip().lower()
        raw_value = raw_value.strip()

        if key not in self._SETTABLE:
            known = ", ".join(f"<code>{k}</code>" for k in self._SETTABLE)
            self._send(
                f"<b>Unknown setting:</b> <code>{html.escape(key)}</code>\n"
                f"Settable keys:\n{known}"
            )
            return

        label, type_fn, unit = self._SETTABLE[key]
        try:
            value = type_fn(raw_value)
        except (ValueError, TypeError) as e:
            # Prefer the validator's own message (e.g. the allowed exit modes);
            # fall back to the expected type name for the plain float/int casts.
            hint = str(e) or f"expected {getattr(type_fn, '__name__', 'a value')}"
            self._send(
                f"<b>Invalid value for <code>{html.escape(key)}</code></b>\n"
                f"<code>{html.escape(hint)}</code>\n"
                f"got: <code>{html.escape(raw_value)}</code>"
            )
            return

        if not self.set_config_cb:
            self._send(f"<b>SET  ·  {ts}</b>\n<code>set_config_cb</code> not configured.")
            return

        result = self.set_config_cb(key, value)
        R = self._R
        if result.get("ok"):
            rows = [
                R(label, f"{value:g} {unit}".strip() if isinstance(value, float) else f"{value} {unit}".strip()),
                "",
                f"<i>{result.get('message', 'Saved.')}</i>",
            ]
            self._send(f"<b>SETTING UPDATED  ·  {ts}</b>\n" + "\n".join(rows))
        else:
            self._send(
                f"<b>SET FAILED  ·  {ts}</b>\n"
                f"<code>{html.escape(result.get('message', 'Unknown error'))}</code>"
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _R(label: str, value: str) -> str:
        """Bold label + inline-code value — renders larger than plain <pre> text.

        Both label and value are HTML-escaped here. They carry dynamic content —
        engine error strings, block reasons, exit reasons, _outcome_tag prose,
        even static labels like "Level P&L" — that can contain '<', '>' or '&'.
        Unescaped, a single stray '<' (e.g. a Python exception message such as
        "'<' not supported between instances of 'NoneType' and 'float'" landing
        in status['error']) makes Telegram reject the ENTIRE message with 400
        "Unsupported start tag", so it only survives via the plain-text fallback
        in send_telegram_message. Escaping at this single choke point — every
        label/value row goes through _R — fixes it for all callers at once.

        quote=False: label/value are tag text / <code> content, never attribute
        values, so quotes need no escaping — and leaving apostrophes intact keeps
        error text like "'<' not supported" and "can't" readable."""
        return (f"<b>{html.escape(str(label), quote=False)}</b>  "
                f"<code>{html.escape(str(value), quote=False)}</code>")

    def _send(self, text: str) -> None:
        """Send a message in a background thread to avoid blocking the caller."""
        if not self.is_ready():
            return
        token = self._token
        chat_id = self._chat_id

        def _do_send():
            send_telegram_message(token, chat_id, text)

        t = threading.Thread(target=_do_send, daemon=True)
        t.start()


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """Return the global TelegramNotifier singleton (created on first call)."""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
