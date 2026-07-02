"""
Post-trade analysis using the Anthropic API.

After each closed (non-paper) trade the analyzer fires in a background
thread, calls Claude with tool_use to get structured JSON, stores the
learning in the database, then optionally hands off to AutoTuner.

Recommendation types Claude can return:
  PARAMETER_CHANGE   — numeric param within a safe corridor (auto-applied)
  FILTER_TOGGLE      — enable/disable hurst_enabled / std_filter_enabled
                       (auto-applied after 5-learning consensus)
  POSITION_SIZE_CHANGE — reduce position_size_usd only, never increase
                         (auto-applied after 4-learning consensus)
  OBSERVATION        — free-form insight surfaced to the human via dashboard
                       (never auto-applied)
"""

import os
import json
import logging
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from models import Trade, TradingConfig
    from database.manager import DatabaseManager

logger = logging.getLogger(__name__)

_ANALYSIS_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 2048

# ── Tool schema ──────────────────────────────────────────────────────────────
# Forces Claude to return structured JSON via tool_use.
# Adding execution_quality, regime_assessment, health_score alongside
# typed recommendations so we capture the full picture, not just param nudges.
_ANALYSIS_TOOL = {
    "name": "record_trade_analysis",
    "description": (
        "Record a SHORT post-trade verdict plus structured recommendations. "
        "The exact numbers (P&L, fees, z-scores, win rate, cost ratio) are shown to the "
        "operator separately as a scorecard, so DO NOT re-list them. Your job is the concise "
        "judgement on top of those numbers, and any actionable parameter changes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "description": (
                    "1-2 sentences, MAX ~45 words. The numbers are already on the scorecard — "
                    "do NOT restate them all. Say only (a) what worked or didn't and WHY in "
                    "cause->effect terms, and (b) if this was a stop, whether it cut a REAL "
                    "divergence (z kept widening) or NOISE that would have reverted (z barely "
                    "moved / already turning). Each clause must cite at least one number. "
                    "Example: 'Stopped when the gap widened 2.8->3.4 instead of reverting — a "
                    "real divergence, so the stop worked. Taker fees ($0.41, 36% of gross) were "
                    "the only avoidable cost.'"
                )
            },
            "recommendations": {
                "type": "array",
                "description": (
                    "Up to 4 sharp, actionable recommendations. "
                    "Each rationale MUST: (1) state the current number and what is wrong with it, "
                    "(2) state the suggested number and exactly why that number, "
                    "(3) explain the expected improvement in plain English with a number. "
                    "Example rationale for PARAMETER_CHANGE: 'Entry threshold is 2.5. "
                    "In the last 5 trades it triggered at gaps that closed only 40% before "
                    "the stop fired. Raising to 2.7 means we only enter at larger gaps — "
                    "back-testing across the last 20 trades suggests win rate improves from "
                    "60% to ~70% at this level.' "
                    "PARAMETER_CHANGE: numeric param within a safe corridor. "
                    "FILTER_TOGGLE: param is 'hurst_enabled' or 'std_filter_enabled', "
                    "  current_value/suggested_value use 1.0=on 0.0=off. "
                    "POSITION_SIZE_CHANGE: param is 'position_size_usd', only suggest lower values. "
                    "OBSERVATION: human-review insight — must include supporting numbers, "
                    "  param is a short plain-English label, current_value and suggested_value are 0."
                ),
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "PARAMETER_CHANGE",
                                "FILTER_TOGGLE",
                                "POSITION_SIZE_CHANGE",
                                "OBSERVATION",
                            ],
                        },
                        "param":           {"type": "string"},
                        "current_value":   {"type": "number"},
                        "suggested_value": {"type": "number"},
                        "confidence":      {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "rationale":       {"type": "string"},
                    },
                    "required": [
                        "type", "param", "current_value",
                        "suggested_value", "confidence", "rationale",
                    ],
                },
            },
            "health_score": {
                "type": "integer",
                "description": (
                    "Composite strategy health 0-100. "
                    "Score based on: win rate (×20), profit factor (×20), "
                    "cost efficiency (×20), regime alignment (×20), "
                    "learning consistency (×20)."
                ),
                "minimum": 0,
                "maximum": 100,
            },
            "confidence_score": {
                "type": "integer",
                "description": "Confidence in this specific analysis 1–10.",
                "minimum": 1,
                "maximum": 10,
            },
            "summary": {
                "type": "string",
                "description": (
                    "One plain-English sentence with the key number: outcome in dollars, "
                    "the single most important pattern, and the one most urgent action."
                )
            },
        },
        "required": [
            "verdict", "recommendations",
            "health_score", "confidence_score", "summary",
        ],
    },
}


class PostTradeAnalyzer:
    """
    Calls the Anthropic API after each closed trade.
    Stores a structured learning, emits real-time socket events,
    and hands off to AutoTuner for any auto-applicable recommendations.
    """

    def __init__(
        self,
        db: "DatabaseManager",
        socketio=None,
        auto_tuner=None,
    ) -> None:
        self.db = db
        self.socketio = socketio
        self.auto_tuner = auto_tuner
        self._api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
        self._credits_exhausted: bool = False
        # Set by app.py: (inst_id, begin_ms, end_ms) -> list[fill dict] with real
        # fee + execType (M/T). None => analysis uses config-estimated fees.
        self.fills_fetcher = None
        if not self._api_key:
            logger.warning("ANTHROPIC_API_KEY not set — post-trade analysis disabled")

    def _gather_real_fills(self, trade: "Trade", config: "TradingConfig"):
        """Pull ACTUAL OKX fills for both legs over the trade window and reduce to
        per-leg real fee + maker/taker. Returns a dict {spot,futures} or None."""
        if not self.fills_fetcher or not trade.entry_time or not trade.exit_time:
            return None
        try:
            et = trade.entry_time.replace(tzinfo=None) if trade.entry_time.tzinfo else trade.entry_time
            xt = trade.exit_time.replace(tzinfo=None) if trade.exit_time.tzinfo else trade.exit_time
            begin_ms = int(et.timestamp() * 1000) - 10_000
            end_ms = int(xt.timestamp() * 1000) + 30_000
        except Exception:
            return None
        legs: Dict[str, Any] = {}
        for label, sym in (("spot", config.spot_symbol), ("futures", config.futures_symbol)):
            try:
                fills = self.fills_fetcher(sym, begin_ms, end_ms) or []
            except Exception:
                fills = []
            if not fills:
                legs[label] = None
                continue
            total_fee = sum(abs(f.get("fee", 0.0)) for f in fills)
            ex = [f.get("execType", "") for f in fills]
            n_m, n_t = ex.count("M"), ex.count("T")
            kind = ("maker" if n_t == 0 and n_m > 0 else
                    "taker" if n_m == 0 and n_t > 0 else
                    f"mixed ({n_m} maker / {n_t} taker)")
            legs[label] = {"symbol": sym, "fee_usd": total_fee, "kind": kind, "n_fills": len(fills)}
        if legs.get("spot") is None and legs.get("futures") is None:
            return None
        return legs

    # ── Public ───────────────────────────────────────────────────────────────

    def analyze_async(self, trade: "Trade") -> None:
        """Fire-and-forget analysis in a daemon thread."""
        if trade.is_open or trade.is_paper:
            return
        if not self._api_key:
            return
        if self._credits_exhausted:
            return
        threading.Thread(
            target=self._run_analysis,
            args=(trade,),
            daemon=True,
            name=f"post-trade-{trade.id}",
        ).start()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _run_analysis(self, trade: "Trade") -> None:
        try:
            import anthropic
        except ImportError:
            logger.error("anthropic package not installed. Run: pip install anthropic")
            return

        try:
            client = anthropic.Anthropic(api_key=self._api_key)

            # Pull context
            recent_trades: List["Trade"] = [
                t for t in self.db.get_trades(limit=30) if not t.is_open
            ][:20]
            past_learnings: List[Dict[str, Any]] = self.db.get_recent_learnings(limit=10)
            config = self.db.get_config()

            real_fills = self._gather_real_fills(trade, config)
            prompt, scorecard = self._build_prompt(trade, recent_trades, past_learnings, config, real_fills)

            message = client.messages.create(
                model=_ANALYSIS_MODEL,
                max_tokens=_MAX_TOKENS,
                tools=[_ANALYSIS_TOOL],
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract structured result
            analysis_data: Optional[Dict[str, Any]] = None
            for block in message.content:
                if block.type == "tool_use" and block.name == "record_trade_analysis":
                    analysis_data = block.input
                    break

            if not analysis_data:
                logger.warning("No tool_use block for trade %s", trade.id)
                return

            # Attach the exact, system-computed scorecard so every consumer
            # (Telegram, dashboard, DB) shows the same numbers — never the LLM's.
            analysis_data = dict(analysis_data)
            analysis_data["scorecard"] = scorecard

            recs = analysis_data.get("recommendations", [])
            logger.info(
                "Post-trade analysis: trade=%d health=%d/100 score=%d/10 recs=%d "
                "tokens(in=%d out=%d)",
                trade.id,
                analysis_data.get("health_score", 0),
                analysis_data.get("confidence_score", 0),
                len(recs),
                message.usage.input_tokens,
                message.usage.output_tokens,
            )

            # Persist learning
            learning_id = self.db.save_learning(trade.id, analysis_data)

            # Backward-compat raw analysis store
            self.db.save_trade_analysis(
                trade.id,
                json.dumps(analysis_data, indent=2),
                _ANALYSIS_MODEL,
            )

            # Emit to dashboard
            if self.socketio:
                self.socketio.emit(
                    "trade_analysis",
                    {
                        "trade_id": trade.id,
                        "learning_id": learning_id,
                        "analysis": analysis_data,
                        "timestamp": datetime.utcnow().isoformat(),
                        "model": _ANALYSIS_MODEL,
                    },
                    namespace="/",
                )

            # Push analysis to Telegram
            try:
                from core.telegram_bot import get_notifier
                get_notifier().notify_trade_analysis(trade.id, analysis_data)
            except Exception as _te:
                logger.debug("Telegram analysis notification failed: %s", _te)

            # Hand off to AutoTuner
            if self.auto_tuner:
                self.auto_tuner.check_and_apply(trade.id)

        except Exception as exc:
            msg = str(exc)
            if "credit balance is too low" in msg or "insufficient_quota" in msg:
                self._credits_exhausted = True
                logger.error(
                    "Anthropic API credits exhausted — post-trade analysis disabled. "
                    "Add credits at https://console.anthropic.com/settings/billing"
                )
            else:
                logger.exception("Post-trade analysis failed for trade %s", trade.id)

    # ── Prompt builder ────────────────────────────────────────────────────────

    @staticmethod
    def _build_prompt(
        trade: "Trade",
        recent_trades: List["Trade"],
        past_learnings: List[Dict[str, Any]],
        config: "TradingConfig",
        real_fills: Optional[Dict[str, Any]] = None,
    ) -> str:
        # ── Duration ──
        duration_secs = None
        duration_str = "unknown"
        if trade.entry_time and trade.exit_time:
            et = trade.entry_time.replace(tzinfo=None) if trade.entry_time.tzinfo else trade.entry_time
            xt = trade.exit_time.replace(tzinfo=None)  if trade.exit_time.tzinfo  else trade.exit_time
            duration_secs = (xt - et).total_seconds()
            if duration_secs < 120:
                duration_str = f"{duration_secs:.0f} sec"
            else:
                duration_str = f"{duration_secs / 60:.1f} min"

        outcome = "WIN" if trade.pnl_usd > 0 else "LOSS"

        # ── Gross / net / fee split ──
        gross_usd = trade.pnl_gross_usd if trade.pnl_gross_usd else trade.pnl_usd
        fees_usd  = trade.fees_usd or 0.0
        net_usd   = trade.pnl_usd
        fee_pct_of_gross = (fees_usd / abs(gross_usd) * 100) if gross_usd else 0.0

        # ── Capital / return-on-capital ──
        capital = trade.capital_locked_usd or 0.0
        roc_pct  = trade.pnl_pct_on_capital or 0.0

        # ── Notional & bps ──
        notional = trade.notional_usd or config.position_size_usd or 0.0

        # ── Z-score reversion (how much of entry gap closed) ──
        ez = abs(trade.entry_zscore)
        xz = abs(trade.exit_zscore)
        z_rev_pct = ((ez - xz) / ez * 100) if ez > 0 else 0.0

        # ── Spread dollar move ──
        spread_move = trade.entry_spread - trade.exit_spread  # positive for LONG if spread fell
        spread_move_usd = spread_move * trade.quantity if trade.quantity else 0.0

        # ── Entry spread context ──
        e_std  = trade.entry_spread_std
        e_mean = trade.entry_spread_mean

        # ── Execution latency ──
        entry_lat = f"{trade.entry_latency_ms:.0f} ms" if trade.entry_latency_ms else "n/a"
        exit_lat  = f"{trade.exit_latency_ms:.0f} ms"  if trade.exit_latency_ms  else "n/a"

        # ── Cost analysis (config round-trip estimate) ──
        entry_mode = getattr(config, "entry_execution_mode", "MARKET")
        exit_mode  = getattr(config, "exit_execution_mode", "MARKET")

        # Actual modes recorded at close time (fall back to config if pre-migration trade)
        actual_entry_mode = getattr(trade, 'actual_entry_mode', None) or entry_mode
        actual_exit_mode  = getattr(trade, 'actual_exit_mode',  None) or exit_mode

        # Determine per-leg fee schedule (both legs are SWAP futures in this strategy)
        from core.trading_engine import is_derivative
        leg_a_deriv = is_derivative(getattr(config, 'spot_symbol',    ''))
        leg_b_deriv = is_derivative(getattr(config, 'futures_symbol', ''))
        def _leg_bps(is_deriv: bool, mode: str) -> float:
            if mode == "LIMIT":
                return config.futures_maker_fee_bps if is_deriv else config.spot_maker_fee_bps
            return config.futures_taker_fee_bps if is_deriv else config.spot_taker_fee_bps

        spot_ef  = _leg_bps(leg_a_deriv, actual_entry_mode)
        fut_ef   = _leg_bps(leg_b_deriv, actual_entry_mode)
        spot_xf  = _leg_bps(leg_a_deriv, actual_exit_mode)
        fut_xf   = _leg_bps(leg_b_deriv, actual_exit_mode)
        total_fees_bps = spot_ef + fut_ef + spot_xf + fut_xf
        total_slip_bps = config.slippage_bps * 4
        total_cost_bps = total_fees_bps + total_slip_bps

        # Maker-only baseline (best case: all 4 legs fill as maker)
        maker_baseline_bps = (
            _leg_bps(leg_a_deriv, "LIMIT") + _leg_bps(leg_b_deriv, "LIMIT") +
            _leg_bps(leg_a_deriv, "LIMIT") + _leg_bps(leg_b_deriv, "LIMIT")
        )
        maker_baseline_usd = notional * maker_baseline_bps / 10000.0
        taker_drag_bps = total_fees_bps - maker_baseline_bps   # extra bps paid due to taker fills
        taker_drag_usd = fees_usd - maker_baseline_usd         # $ lost to taker vs maker ideal

        if taker_drag_usd > 0.01:
            fee_efficiency_block = (
                f"Actual modes:    entry={actual_entry_mode}, exit={actual_exit_mode}\n"
                f"Maker baseline:  {maker_baseline_bps:.1f} bps = ${maker_baseline_usd:.2f}\n"
                f"Actual fees:     {total_fees_bps:.1f} bps = ${fees_usd:.2f}\n"
                f"Taker drag:     +{taker_drag_bps:.1f} bps = +${taker_drag_usd:.2f}  "
                f"← POST_ONLY rejection forced exit to MARKET taker"
            )
        else:
            fee_efficiency_block = (
                f"Actual modes:    entry={actual_entry_mode}, exit={actual_exit_mode}\n"
                f"Maker baseline:  {maker_baseline_bps:.1f} bps = ${maker_baseline_usd:.2f}\n"
                f"Actual fees:     {total_fees_bps:.1f} bps = ${fees_usd:.2f}\n"
                f"Taker drag:      none — all limit orders filled as maker"
            )

        gross_pnl_bps = (gross_usd / notional * 10000) if notional > 0 else 0.0
        cor = round(total_cost_bps / abs(gross_pnl_bps), 2) if gross_pnl_bps != 0 else "∞"

        # ── Win/loss stats ──
        wins5   = sum(1 for t in recent_trades[:5]  if t.pnl_usd > 0)
        wins20  = sum(1 for t in recent_trades[:20] if t.pnl_usd > 0)
        wr_last5  = wins5  / max(len(recent_trades[:5]),  1)
        wr_last20 = wins20 / max(len(recent_trades[:20]), 1)
        wr_trend  = "IMPROVING" if wr_last5 > wr_last20 + 0.05 else \
                    "DECLINING"  if wr_last5 < wr_last20 - 0.05 else "STABLE"

        # ── P&L totals ──
        total_pnl_5  = sum(t.pnl_usd for t in recent_trades[:5])
        total_pnl_20 = sum(t.pnl_usd for t in recent_trades[:20])

        # ── Avg win / avg loss ──
        win_pnls  = [t.pnl_usd for t in recent_trades if t.pnl_usd > 0]
        loss_pnls = [t.pnl_usd for t in recent_trades if t.pnl_usd < 0]
        avg_win   = f"${sum(win_pnls)/len(win_pnls):+.2f}"   if win_pnls  else "n/a"
        avg_loss  = f"${sum(loss_pnls)/len(loss_pnls):+.2f}" if loss_pnls else "n/a"
        max_win   = f"${max(win_pnls):+.2f}"   if win_pnls  else "n/a"
        max_loss  = f"${min(loss_pnls):+.2f}"  if loss_pnls else "n/a"

        # ── Profit factor ──
        gross_wins   = sum(t.pnl_usd for t in recent_trades if t.pnl_usd > 0)
        gross_losses = abs(sum(t.pnl_usd for t in recent_trades if t.pnl_usd < 0))
        profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 0.0
        expected_val  = round(total_pnl_20 / max(len(recent_trades), 1), 2)

        # ── Consecutive streak ──
        streak = 0
        if recent_trades:
            first_win = recent_trades[0].pnl_usd > 0
            for t in recent_trades:
                if (t.pnl_usd > 0) == first_win:
                    streak += 1
                else:
                    break
            if not first_win:
                streak = -streak

        # ── Avg hold time ──
        def _hold_min(t: "Trade") -> Optional[float]:
            if t.entry_time and t.exit_time:
                _et = t.entry_time.replace(tzinfo=None) if t.entry_time.tzinfo else t.entry_time
                _xt = t.exit_time.replace(tzinfo=None)  if t.exit_time.tzinfo  else t.exit_time
                return (_xt - _et).total_seconds() / 60
            return None

        winner_holds = [_hold_min(t) for t in recent_trades if t.pnl_usd > 0 and _hold_min(t)]
        loser_holds  = [_hold_min(t) for t in recent_trades if t.pnl_usd < 0 and _hold_min(t)]
        avg_hold_w = f"{sum(winner_holds)/len(winner_holds):.1f} min" if winner_holds else "n/a"
        avg_hold_l = f"{sum(loser_holds)/len(loser_holds):.1f} min"   if loser_holds  else "n/a"

        # ── Avg fees per trade (recent) ──
        fee_samples = [t.fees_usd for t in recent_trades if t.fees_usd]
        avg_fee_recent = f"${sum(fee_samples)/len(fee_samples):.2f}" if fee_samples else "n/a"

        # ── Expected Value (EV) calculation ──
        # Target and stop as configured; R:R = target / stop.
        # Break-even win rate = stop / (target + stop)
        # Actual EV = win_rate × target - (1-win_rate) × stop
        cfg_target = getattr(config, 'profit_target_usd', 0.0) or 0.0
        cfg_rr     = getattr(config, 'min_entry_rr_multiple', 0.0) or 0.0
        cfg_stop   = (cfg_target / cfg_rr) if (cfg_rr > 0 and cfg_target > 0) else (
                     getattr(config, 'max_loss_usd', 0.0) or 0.0)
        if cfg_target > 0 and cfg_stop > 0:
            rr_ratio          = cfg_target / cfg_stop
            breakeven_wr      = cfg_stop / (cfg_target + cfg_stop)
            actual_ev_last20  = wr_last20 * cfg_target - (1 - wr_last20) * cfg_stop
            actual_ev_last5   = wr_last5  * cfg_target - (1 - wr_last5)  * cfg_stop
            ev_block = (
                f"Target: ${cfg_target:.2f}  |  Stop: ${cfg_stop:.2f}  |  R:R = {rr_ratio:.2f}x\n"
                f"Break-even win rate:   {breakeven_wr:.1%} (need this win rate just to break even)\n"
                f"EV at last-20 win rate ({wr_last20:.0%}): ${actual_ev_last20:+.2f} per trade\n"
                f"EV at last-5  win rate ({wr_last5:.0%}):  ${actual_ev_last5:+.2f} per trade"
            )
        else:
            rr_ratio = 0.0
            breakeven_wr = 0.0
            actual_ev_last20 = 0.0
            ev_block = "(no profit target or stop configured — EV not calculable)"

        # ── Trade history lines ──
        history_lines = []
        for t in recent_trades:
            flag = "W" if t.pnl_usd > 0 else "L"
            hold = _hold_min(t)
            hold_str = f"{hold:.0f}m" if hold else "?"
            fee_str = f"  fees=${t.fees_usd:.2f}" if t.fees_usd else ""
            history_lines.append(
                f"  [{flag}] {t.position_type:<5}  "
                f"entry_z={t.entry_zscore:+.2f}  exit_z={t.exit_zscore:+.2f}  "
                f"gross=${t.pnl_gross_usd:+.2f}  net=${t.pnl_usd:+.2f}{fee_str}  "
                f"hold={hold_str}  reason={t.exit_reason}"
            )

        # ── Prior learnings ──
        learnings_lines = []
        for lrn in past_learnings:
            recs = json.loads(lrn.get("recommendations", "[]"))
            rec_str = "; ".join(
                f"[{r.get('type','?')[:3]}] {r['param']}→{r['suggested_value']} "
                f"(conf={r['confidence']:.2f})"
                for r in recs
            ) or "none"
            hs = lrn.get("health_score")
            hs_str = f"  health={hs}" if hs is not None else ""
            learnings_lines.append(
                f"  [{lrn.get('timestamp','')[:16]}]{hs_str}  "
                f"{lrn.get('summary','')}  |  recs: {rec_str}"
            )

        # ── Exit reason breakdown ──
        from collections import Counter
        exit_reasons = Counter(t.exit_reason for t in recent_trades)
        exit_breakdown = "  " + "  |  ".join(
            f"{reason}: {cnt}" for reason, cnt in exit_reasons.most_common()
        )

        # ── REAL OKX fills (actual fee + maker/taker, from fills-history) ──
        if real_fills:
            _lines, _real_total = [], 0.0
            for _lbl in ("spot", "futures"):
                _leg = real_fills.get(_lbl)
                if _leg:
                    _real_total += _leg["fee_usd"]
                    _lines.append(
                        f"  {_lbl:<8} {_leg['symbol']}: ${_leg['fee_usd']:.4f} fee  ·  "
                        f"{_leg['kind']}  ·  {_leg['n_fills']} fill(s)")
                else:
                    _lines.append(f"  {_lbl:<8} (no fills returned by OKX)")
            real_fills_block = (
                "\n".join(_lines)
                + f"\n  REAL round-trip fee (OKX): ${_real_total:.4f}   |   "
                  f"engine estimate: ${fees_usd:.4f}")
        else:
            real_fills_block = ("  (real OKX fill data unavailable — the fee numbers above "
                                "are the engine's estimate, not confirmed maker/taker)")

        # ── Dollar-stop diagnosis (operator's #1 concern) ──
        is_stop = (trade.exit_reason or "").upper() in ("DOLLAR_STOP", "STOP_LOSS", "DAILY_LOSS")
        if is_stop:
            diverged = xz > ez
            stop_block = (
                f"This exit was a STOP ({trade.exit_reason}). Diagnose it directly:\n"
                f"  - Z-score went {trade.entry_zscore:+.2f} -> {trade.exit_zscore:+.2f} "
                f"({'DIVERGED further from zero' if diverged else 'was reverting'}; "
                f"{z_rev_pct:+.0f}% back toward zero).\n"
                f"  - Spread moved {spread_move:+.6f} (~${spread_move_usd:+.2f}); "
                f"net ${net_usd:+.2f} = gross ${gross_usd:+.2f} minus ${fees_usd:.2f} fees.\n"
                f"  - Decide: REAL divergence (z kept widening -> the stop correctly capped a loser) "
                f"or NOISE the stop cut before it could revert (z barely moved / already turning)?\n"
                f"  - If noise: say how much wider the dollar stop should be, in $ AND as % of the "
                f"${notional:,.0f} notional, to survive normal fluctuation (entry std {e_std:.6f})."
            )
        else:
            stop_block = "  (not a stop exit)"

        # ── SCORECARD (exact, system-computed — shown to the operator verbatim;
        #    the LLM is told NOT to re-list these) ──
        _kind_short = {"maker": "maker", "taker": "taker"}
        if real_fills:
            _rt, _parts = 0.0, []
            for _lbl in ("spot", "futures"):
                _leg = real_fills.get(_lbl)
                if _leg:
                    _rt += _leg["fee_usd"]
                    _parts.append(f"{_lbl} {_kind_short.get(_leg['kind'], _leg['kind'])}")
            real_fee_str = f"${_rt:.4f} ({', '.join(_parts)})" if _parts else f"~${fees_usd:.2f} (est)"
        else:
            real_fee_str = f"~${fees_usd:.2f} (engine est, unconfirmed)"

        scorecard = [
            {"label": "Result",     "value": f"{outcome} · net ${net_usd:+.2f} ({trade.pnl_percent:+.2f}% notional, {roc_pct:+.2f}% cap)"},
            {"label": "P&L split",  "value": f"gross ${gross_usd:+.2f} − fees ${fees_usd:.2f} ({fee_pct_of_gross:.0f}% of gross)"},
            {"label": "Real fees",  "value": real_fee_str},
            {"label": "Z-score",    "value": f"{trade.entry_zscore:+.2f} → {trade.exit_zscore:+.2f} (reverted {z_rev_pct:.0f}%)"},
            {"label": "Spread",     "value": f"{spread_move:+.6f} (~${spread_move_usd:+.2f})"},
            {"label": "Hold",       "value": duration_str},
            {"label": "Cost ratio", "value": f"{cor} ({total_cost_bps:.1f}bps cost vs {abs(gross_pnl_bps):.1f}bps move)"},
            {"label": "Exit",       "value": str(trade.exit_reason)},
            {"label": "Win rate",   "value": f"{wr_last5:.0%} last-5 · {wr_last20:.0%} last-20 ({wr_trend})"},
            {"label": "Prof.factor","value": f"{profit_factor} · EV ${expected_val:+.2f}/trade"},
            {"label": "Streak",     "value": f"{'+' if streak >= 0 else ''}{streak}"},
        ]
        if is_stop:
            scorecard.insert(4, {
                "label": "Stop check",
                "value": f"z {'DIVERGED (widened)' if xz > ez else 'was reverting'}, {z_rev_pct:+.0f}% toward 0",
            })

        prompt = f"""You are reviewing a statistical-arbitrage trade. The operator ALREADY sees a
scorecard of exact numbers (P&L, fees, z-scores, win rate, cost ratio). Your output is
the short judgement ON TOP of those numbers — not a re-listing of them.
RULES:
1. verdict = 1-2 sentences, ~45 words MAX. Do NOT restate the scorecard numbers; cite only
   the 1-2 numbers that drive your judgement.
2. Frame it "what worked / what didn't" with cause→effect. Prefer the REAL OKX FILLS numbers
   (actual fee, maker vs taker) over the engine estimate.
3. REGIME NOTE (standing guidance): even when Hurst reads above 0.5 ("trending"), this ETH/BTC
   pair still tends to mean-revert. Do NOT recommend disabling entries or the strategy on the
   Hurst number alone — weight actual reversion outcomes (z-reversion %, win rate) instead.
4. The operator's BIGGEST concern is the DOLLAR STOP firing too early. If this was a stop, use
   the STOP DIAGNOSIS section and state plainly in the verdict: real divergence, or noise cut early?
5. Recommendations must be sharp: current number → problem → suggested number → why. No padding.

Call record_trade_analysis to record your analysis.

═══ THIS TRADE · {outcome} ═══
Asset:                {trade.asset}
Direction:            {trade.position_type} spread
Entry Z-score:        {trade.entry_zscore:+.4f}  (entry threshold: {config.entry_threshold})
Exit  Z-score:        {trade.exit_zscore:+.4f}   (exit threshold: {config.exit_threshold})
Z-score reverted:     {z_rev_pct:.1f}% of the way back to zero
Entry Spread:         {trade.entry_spread:.6f}  (mean={e_mean:.6f}, std={e_std:.6f})
Exit  Spread:         {trade.exit_spread:.6f}
Spread move:          {spread_move:+.6f}  (~${spread_move_usd:+.2f} at qty {trade.quantity:.4f})
Spot @ entry:         ${trade.entry_spot_price:,.2f}
Futures @ entry:      ${trade.entry_futures_price:,.2f}
Duration:             {duration_str}
Notional (Leg A):     ${notional:,.2f}
Capital locked:       ${capital:,.2f}
P&L gross:            ${gross_usd:+.2f}
Fees paid:            ${fees_usd:.2f}  ({fee_pct_of_gross:.1f}% of gross)
P&L net:              ${net_usd:+.2f}  ({trade.pnl_percent:+.2f}% of notional, {roc_pct:+.2f}% of capital)
Exit reason:          {trade.exit_reason}
Entry latency:        {entry_lat}
Exit latency:         {exit_lat}
Cost-to-Opp ratio:    {cor}  (round-trip cost {total_cost_bps:.1f} bps vs {abs(gross_pnl_bps):.1f} bps gross move)

═══ FEE EFFICIENCY (engine estimate) ═══
{fee_efficiency_block}

═══ REAL OKX FILLS (actual fee & maker/taker — use these numbers) ═══
{real_fills_block}

═══ STOP DIAGNOSIS ═══
{stop_block}

═══ EXPECTED VALUE (EV) ANALYSIS ═══
{ev_block}

═══ STRATEGY PERFORMANCE ({len(recent_trades)} closed trades) ═══
Win rate:             last-5={wr_last5:.0%} ({wins5}/{len(recent_trades[:5])})  last-20={wr_last20:.0%} ({wins20}/{len(recent_trades[:20])})  trend={wr_trend}
Profit factor:        {profit_factor}  (every $1 lost → ${profit_factor} recovered)
EV/trade (last-20):   ${expected_val:+.2f}
Avg win:              {avg_win}    max win: {max_win}
Avg loss:             {avg_loss}    max loss: {max_loss}
Total P&L last-5:     ${total_pnl_5:+.2f}
Total P&L last-20:    ${total_pnl_20:+.2f}
Current streak:       {'+' if streak >= 0 else ''}{streak}  ({'consecutive wins' if streak > 0 else 'consecutive losses' if streak < 0 else 'n/a'})
Avg hold (wins):      {avg_hold_w}
Avg hold (losses):    {avg_hold_l}
Avg fees/trade:       {avg_fee_recent}
Exit breakdown:       {exit_breakdown}

═══ CURRENT CONFIG ═══
entry_threshold:      {config.entry_threshold}   (safe: 1.8–3.5, step ≤0.2)
exit_threshold:       {config.exit_threshold}   (safe: 0.3–1.0, step ≤0.1)
stop_loss_threshold:  {config.stop_loss_threshold}   (safe: 3.0–5.5, step ≤0.3)
min_std_multiple:     {config.min_std_multiple}   (edge gate; safe: 1.0–2.5, step ≤0.15)
slippage_bps:         {config.slippage_bps}   (safe: 1.0–10.0, step ≤1.0)
hurst_enabled:        {config.hurst_enabled}   (threshold: {config.hurst_threshold})
std_filter_enabled:   {config.std_filter_enabled}
position_size_usd:    ${config.position_size_usd:,.0f}  (max: ${config.max_position_size_usd:,.0f})
entry_execution_mode: {entry_mode}  |  exit_execution_mode: {exit_mode}
round-trip fees:      {total_fees_bps:.1f} bps fees + {total_slip_bps:.1f} bps slippage = {total_cost_bps:.1f} bps total

═══ RECENT TRADE HISTORY ═══
{chr(10).join(history_lines) if history_lines else "  (no prior trades)"}

═══ ACCUMULATED LEARNINGS (most recent first) ═══
{chr(10).join(learnings_lines) if learnings_lines else "  (no prior learnings)"}

═══ OUTPUT ═══
verdict          — 1-2 sentences, ~45 words MAX. The scorecard already lists the numbers; cite only
                   the 1-2 that drive the judgement. What worked / what didn't, cause→effect. If a
                   stop: from STOP DIAGNOSIS, state plainly — real divergence or noise cut early?
                   Prefer REAL OKX FILLS (actual fee, maker vs taker) over estimates. Honour the
                   regime note (a "trending" Hurst reading does not mean this pair stopped reverting).
summary          — ONE short headline line (used in logs), e.g. "Stop cut a real loser; fees fine."
recommendations  — Up to 4. Each rationale: current number → problem → suggested number → why → expected
                   improvement. No vague statements.
  PARAMETER_CHANGE     — numeric setting, auto-applied at 3+ consensus, conf ≥0.70
  FILTER_TOGGLE        — hurst_enabled or std_filter_enabled, needs 5+ consensus
  POSITION_SIZE_CHANGE — reduce position_size_usd only, needs 4+ consensus
  OBSERVATION          — human-review note with supporting numbers, shown on dashboard
health_score     — 0-100 composite.   confidence_score — 1-10 for THIS analysis.

Call record_trade_analysis now."""
        return prompt, scorecard
