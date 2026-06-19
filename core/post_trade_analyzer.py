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
        "Record a complete, structured post-trade analysis. "
        "Use all four recommendation types: PARAMETER_CHANGE, FILTER_TOGGLE, "
        "POSITION_SIZE_CHANGE, and OBSERVATION where appropriate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "root_cause": {
                "type": "string",
                "description": "1-2 sentences: the fundamental reason this trade won or lost."
            },
            "patterns": {
                "type": "string",
                "description": (
                    "1-2 sentences: repeating failure/success patterns visible "
                    "in the cross-trade history provided."
                )
            },
            "execution_quality": {
                "type": "string",
                "description": (
                    "Assessment of entry/exit execution: fill efficiency, "
                    "cost-to-opportunity ratio, leg synchronisation. 1-2 sentences."
                )
            },
            "regime_assessment": {
                "type": "string",
                "description": (
                    "Market regime at trade time: mean-reverting vs trending, "
                    "spread volatility, any structural shift observed. 1-2 sentences."
                )
            },
            "recommendations": {
                "type": "array",
                "description": (
                    "Up to 4 recommendations across all four types. "
                    "PARAMETER_CHANGE: numeric param within a safe corridor. "
                    "FILTER_TOGGLE: param is 'hurst_enabled' or 'std_filter_enabled', "
                    "  current_value/suggested_value use 1.0=on 0.0=off. "
                    "POSITION_SIZE_CHANGE: param is 'position_size_usd', only suggest lower values. "
                    "OBSERVATION: human-review insight, param is a short label, "
                    "  current_value and suggested_value are 0."
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
                "description": "One sentence: outcome, key pattern, most important next action."
            },
        },
        "required": [
            "root_cause", "patterns", "execution_quality", "regime_assessment",
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
        if not self._api_key:
            logger.warning("ANTHROPIC_API_KEY not set — post-trade analysis disabled")

    # ── Public ───────────────────────────────────────────────────────────────

    def analyze_async(self, trade: "Trade") -> None:
        """Fire-and-forget analysis in a daemon thread."""
        if trade.is_open or trade.is_paper:
            return
        if not self._api_key:
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
                "Post-trade analysis: trade=%d health=%d/100 score=%d/10 recs=%d",
                trade.id,
                analysis_data.get("health_score", 0),
                analysis_data.get("confidence_score", 0),
                len(recs),
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

            # Hand off to AutoTuner
            if self.auto_tuner:
                self.auto_tuner.check_and_apply(trade.id)

        except Exception:
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
        duration_str = "unknown"
        if trade.entry_time and trade.exit_time:
            secs = (trade.exit_time - trade.entry_time).total_seconds()
            duration_str = f"{secs / 60:.1f} min"

        outcome = "WIN" if trade.pnl_usd > 0 else "LOSS"

        # ── Win/loss stats ──
        wins   = sum(1 for t in recent_trades if t.pnl_usd > 0)
        losses = len(recent_trades) - wins
        wr_last5  = sum(1 for t in recent_trades[:5]  if t.pnl_usd > 0) / max(len(recent_trades[:5]),  1)
        wr_last20 = sum(1 for t in recent_trades[:20] if t.pnl_usd > 0) / max(len(recent_trades[:20]), 1)
        wr_trend  = "IMPROVING" if wr_last5 > wr_last20 + 0.05 else \
                    "DECLINING"  if wr_last5 < wr_last20 - 0.05 else "STABLE"

        # ── Profit factor ──
        gross_wins   = sum(t.pnl_usd for t in recent_trades if t.pnl_usd > 0)
        gross_losses = abs(sum(t.pnl_usd for t in recent_trades if t.pnl_usd < 0))
        profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 0.0

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
                streak = -streak   # negative = consecutive losses

        # ── Avg hold time ──
        def _hold_min(t: "Trade") -> Optional[float]:
            if t.entry_time and t.exit_time:
                return (t.exit_time - t.entry_time).total_seconds() / 60
            return None

        winner_holds = [_hold_min(t) for t in recent_trades if t.pnl_usd > 0 and _hold_min(t)]
        loser_holds  = [_hold_min(t) for t in recent_trades if t.pnl_usd < 0 and _hold_min(t)]
        avg_hold_w   = f"{sum(winner_holds)/len(winner_holds):.1f} min" if winner_holds else "n/a"
        avg_hold_l   = f"{sum(loser_holds)/len(loser_holds):.1f} min"   if loser_holds  else "n/a"

        # ── Cost analysis ──
        entry_mode = getattr(config, "entry_execution_mode", "MARKET")
        exit_mode  = getattr(config, "exit_execution_mode", "MARKET")
        spot_ef  = config.spot_maker_fee_bps if entry_mode == "LIMIT" else config.spot_taker_fee_bps
        fut_ef   = config.futures_maker_fee_bps if entry_mode == "LIMIT" else config.futures_taker_fee_bps
        spot_xf  = config.spot_maker_fee_bps if exit_mode == "LIMIT" else config.spot_taker_fee_bps
        fut_xf   = config.futures_maker_fee_bps if exit_mode == "LIMIT" else config.futures_taker_fee_bps
        total_fees_bps = spot_ef + fut_ef + spot_xf + fut_xf
        total_slip_bps = config.slippage_bps * 4
        total_cost_bps = total_fees_bps + total_slip_bps

        notional = getattr(trade, "notional_usd", None) or config.position_size_usd
        if notional and notional > 0:
            gross_pnl_bps = (trade.pnl_usd / notional) * 10000
            cor = round(total_cost_bps / abs(gross_pnl_bps), 2) if gross_pnl_bps != 0 else "∞"
        else:
            gross_pnl_bps = 0.0
            cor = "unknown"

        # ── Trade history lines ──
        history_lines = []
        for t in recent_trades:
            flag = "W" if t.pnl_usd > 0 else "L"
            hold = _hold_min(t)
            hold_str = f"{hold:.0f}m" if hold else "?"
            history_lines.append(
                f"  [{flag}] {t.position_type:<5}  "
                f"entry_z={t.entry_zscore:+.2f}  exit_z={t.exit_zscore:+.2f}  "
                f"pnl=${t.pnl_usd:+.2f}  hold={hold_str}  reason={t.exit_reason}"
            )

        # ── Prior learnings with typed recs ──
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

        return f"""You are a quant analyst reviewing a crypto statistical-arbitrage (spot-futures basis) trade.
Be thorough. Use all four recommendation types where the evidence warrants it.
Call record_trade_analysis to record your structured analysis.

═══ THIS TRADE · {outcome} ═══
Asset:            {trade.asset}
Direction:        {trade.position_type} spread
Entry Z-score:    {trade.entry_zscore:+.4f}
Exit  Z-score:    {trade.exit_zscore:+.4f}
Entry Spread:     {trade.entry_spread:.4f}
Exit  Spread:     {trade.exit_spread:.4f}
Spot @ entry:     ${trade.entry_spot_price:,.2f}
Futures @ entry:  ${trade.entry_futures_price:,.2f}
Duration:         {duration_str}
P&L (gross):      ${trade.pnl_usd:+.2f}  ({trade.pnl_percent:+.2f}%)
Exit reason:      {trade.exit_reason}
Cost-to-Opp:      {cor}  (round-trip cost {total_cost_bps:.1f} bps vs {abs(gross_pnl_bps):.1f} bps gross move)

═══ STRATEGY PERFORMANCE ({len(recent_trades)} closed trades) ═══
Win rate:         last-5={wr_last5:.0%}  last-20={wr_last20:.0%}  trend={wr_trend}
Profit factor:    {profit_factor}
Current streak:   {'+' if streak >= 0 else ''}{streak}  ({'consecutive wins' if streak > 0 else 'consecutive losses' if streak < 0 else 'n/a'})
Avg hold (wins):  {avg_hold_w}
Avg hold (losses):{avg_hold_l}
Exit breakdown:   {exit_breakdown}

═══ CURRENT CONFIG ═══
entry_threshold:      {config.entry_threshold}   (safe: 1.8–3.5, step ≤0.2)
exit_threshold:       {config.exit_threshold}   (safe: 0.3–1.0, step ≤0.1)
stop_loss_threshold:  {config.stop_loss_threshold}   (safe: 3.0–5.5, step ≤0.3)
min_std_multiple:     {config.min_std_multiple}   (edge gate: min expected-move ÷ cost; safe: 1.0–2.5, step ≤0.15)
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

═══ RECOMMENDATION GUIDE ═══
PARAMETER_CHANGE  — numeric param within corridor, auto-applied at 3+ consensus, conf ≥0.70
FILTER_TOGGLE     — hurst_enabled or std_filter_enabled (1.0=on, 0.0=off)
                    auto-applied at 5+ consensus, conf ≥0.72
                    Only recommend disabling if repeated evidence the filter is blocking good trades
                    or enabling if regime evidence shows filter would have prevented losses
POSITION_SIZE_CHANGE — position_size_usd only, ONLY suggest values lower than current
                    auto-applied at 4+ consensus, conf ≥0.75
                    Use this if the strategy is clearly in a losing streak / wrong regime
OBSERVATION       — any insight for human review: execution anomalies, market structure,
                    funding rate concerns, time-of-day patterns, regime warnings
                    These are surfaced on the dashboard, never auto-applied

Call record_trade_analysis now with your full analysis."""
