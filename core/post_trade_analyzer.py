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
        "Record a data-rich, plain-English post-trade analysis. "
        "Every sentence must include specific numbers from the trade data provided. "
        "No jargon, but do not omit numbers — dollars, percentages, durations, z-scores, "
        "win rates, and counts must all appear where relevant. "
        "Write as if briefing a smart non-trader who wants the full picture in plain language."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "what_happened": {
                "type": "string",
                "description": (
                    "3-4 sentences covering the full factual outcome with every key number. "
                    "MUST include: net P&L in dollars, hold time, gross P&L vs fees/slippage cost, "
                    "entry gap size (z-score) and exit gap size, and the exit reason. "
                    "Example style: 'The trade made $1.15 net ($1.56 gross minus $0.41 in fees) "
                    "and closed in 48 seconds. We entered when the ETH-BTC price gap was 2.80 "
                    "standard deviations wide and exited at 1.53 — the gap closed 45% of the way "
                    "back to normal. Fees and slippage consumed 26% of the gross profit.'"
                )
            },
            "why": {
                "type": "string",
                "description": (
                    "3-4 sentences explaining the cause with supporting numbers from both "
                    "this trade and the recent history. "
                    "MUST reference: win rate (last-5 and last-20), profit factor, current streak, "
                    "cost-to-opportunity ratio, and whether the market was behaving normally. "
                    "Example style: 'The gap closed quickly because the market was behaving "
                    "normally — prices snapping back is what this strategy depends on. "
                    "Across the last 20 trades the win rate is 60% with a profit factor of 1.4, "
                    "meaning for every $1 lost we make $1.40 on wins. "
                    "This is the 3rd win in a row. The fee cost was 36% of gross profit — "
                    "higher than the healthy target of under 25%.'"
                )
            },
            "what_could_be_better": {
                "type": "string",
                "description": (
                    "3-4 sentences with specific, numbered improvements. "
                    "MUST include concrete numbers: what entry gap threshold would have improved "
                    "the reward vs risk, what the cost ratio was vs target, what the current "
                    "position size is and whether it is appropriate given recent performance, "
                    "and any pattern visible across the last N trades. "
                    "Example style: 'We entered at a gap of 2.80 — waiting for 3.00 would give "
                    "7% more profit potential for the same risk. Fees were 36% of gross vs the "
                    "target of under 25%; switching to limit orders on exit could save ~5 bps. "
                    "The last 5 trades all closed within 5 minutes, suggesting the position "
                    "size of $1,000 could be increased once fees are under control.'"
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
            "what_happened", "why", "what_could_be_better",
            "recommendations", "health_score", "confidence_score", "summary",
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
        if not self._api_key:
            logger.warning("ANTHROPIC_API_KEY not set — post-trade analysis disabled")

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

            prompt = self._build_prompt(trade, recent_trades, past_learnings, config)

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
        spot_ef  = config.spot_maker_fee_bps  if entry_mode == "LIMIT" else config.spot_taker_fee_bps
        fut_ef   = config.futures_maker_fee_bps if entry_mode == "LIMIT" else config.futures_taker_fee_bps
        spot_xf  = config.spot_maker_fee_bps  if exit_mode  == "LIMIT" else config.spot_taker_fee_bps
        fut_xf   = config.futures_maker_fee_bps if exit_mode  == "LIMIT" else config.futures_taker_fee_bps
        total_fees_bps = spot_ef + fut_ef + spot_xf + fut_xf
        total_slip_bps = config.slippage_bps * 4
        total_cost_bps = total_fees_bps + total_slip_bps

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

        return f"""You are reviewing a crypto trading strategy for someone who is not a financial or technical expert.
RULES:
1. Plain English only — no jargon, no acronyms, no formulas.
2. Every sentence must contain specific numbers pulled from the data below.
   Vague statements like "fees were high" are not acceptable.
   Correct: "Fees consumed $0.41 of the $1.56 gross profit — that is 26%."
3. Recommendations must be sharp and actionable: state the current number,
   the suggested number, and in one sentence why that specific change is justified
   by the data (reference win rate, streak, cost ratio, or trade count).
4. Do not pad with generic advice. Every point must be earned by the numbers.

Use the four-section format and call record_trade_analysis to record your analysis.

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

═══ ANALYSIS FORMAT ═══
Four sections. Every sentence must contain at least one number from the data above.

what_happened       — Net P&L $, hold time, gross vs fees ($ and %), z-reversion %, exit reason.
why                 — Win rate last-5 vs last-20, profit factor, streak, EV/trade (from EV section),
                      break-even win rate vs actual win rate, cost-to-opp ratio vs target (<0.30),
                      avg win vs avg loss. Explain whether this strategy has positive expected value.
what_could_be_better — Specific numbers: R:R ratio, whether actual win rate exceeds break-even win
                       rate, fee % vs 25% target, avg hold winners vs losers, whether position size
                       fits current EV per trade.
recommendations     — Up to 4 items. Each rationale: current number → problem → suggested number
                      → why that number → expected improvement. No vague statements.
  PARAMETER_CHANGE     — numeric setting, auto-applied at 3+ consensus, conf ≥0.70
  FILTER_TOGGLE        — hurst_enabled or std_filter_enabled, needs 5+ consensus
  POSITION_SIZE_CHANGE — reduce position_size_usd only, needs 4+ consensus
  OBSERVATION          — human-review note with supporting numbers, shown on dashboard

Call record_trade_analysis now."""
