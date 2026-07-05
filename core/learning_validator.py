"""
LearningValidator: measures every applied auto-tune change against realized
outcomes and reverts the ones that made things worse.

Closes the learning loop that PostTradeAnalyzer + AutoTuner leave open:
analyzer recommends → tuner applies → trades happen → **validator judges**
→ verdict feeds the next analysis prompt (confidence calibration) and, for
FAILED changes, restores the prior value.

Verdicts (terminal — recorded once per learning_log entry):
  VALIDATED — win rate and expectancy both improved after the change
  FAILED    — win rate and expectancy both degraded → auto-revert
  NEUTRAL   — anything in between (no action)

Entries stay unvalidated until MIN_TRADES_AFTER closed trades exist to judge
them on, so early noise can't trigger a premature verdict. Circuit-breaker
pauses (algo_enabled) and the validator's own reverts are never re-judged —
the former is an operator decision, the latter would ping-pong.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from database.manager import DatabaseManager

logger = logging.getLogger(__name__)

MIN_TRADES_BEFORE = 5     # closed trades needed pre-change for a baseline
MIN_TRADES_AFTER = 6      # closed trades needed post-change for a verdict
MAX_WINDOW = 20           # cap either side so old regimes don't dominate
WIN_RATE_IMPROVE = 0.05   # +5pp win rate (with better expectancy) = VALIDATED
WIN_RATE_DEGRADE = 0.10   # -10pp win rate (with worse expectancy) = FAILED

# Params the validator may restore.
BOOL_PARAMS = {"hurst_enabled", "std_filter_enabled"}
# Never judged: algo_enabled (un-pausing is the operator's call) and
# position_size_usd (size changes fire in RESPONSE to performance — a breaker
# cut coincides with losses by construction, so before/after stats carry
# fatal selection bias; the breaker/recovery ladder own that param).
SKIP_PARAMS = {"algo_enabled", "position_size_usd"}
# Never judged: our own reverts (ping-pong) and breaker/recovery actions
# (selection bias, same reason as position_size_usd).
SKIP_RATIONALE_PREFIXES = ("Auto-revert:", "Circuit breaker:", "Recovery ladder:")
REVERT_RATIONALE_PREFIX = "Auto-revert:"


class LearningValidator:
    """Judges applied parameter changes and reverts proven failures."""

    def __init__(self, db: "DatabaseManager", engine=None, socketio=None) -> None:
        self.db = db
        self.engine = engine
        self.socketio = socketio

    # ── Public ───────────────────────────────────────────────────────────────

    def run(self, trigger_trade_id: int, revert_allowed: bool) -> None:
        """
        Evaluate all unvalidated learning_log entries. Verdicts are always
        recorded; reverts additionally require revert_allowed (operator has
        auto-tune enabled) so turning tuning off freezes the config.
        """
        try:
            entries = self.db.get_learning_log(limit=25)
            if not entries:
                return
            already_judged = self.db.get_validated_log_ids()

            closed = sorted(
                (t for t in self.db.get_trades(limit=300)
                 if not t.is_open and t.exit_time),
                key=lambda t: t.exit_time,
            )

            for entry in entries:
                if entry["id"] in already_judged:
                    continue
                if entry.get("reverted"):
                    continue
                if entry.get("param") in SKIP_PARAMS:
                    continue
                if (entry.get("rationale") or "").startswith(SKIP_RATIONALE_PREFIXES):
                    continue

                changed_at = self._parse_ts(entry.get("timestamp"))
                if changed_at is None:
                    continue

                before = [t for t in closed if t.exit_time <= changed_at][-MAX_WINDOW:]
                after = [t for t in closed if t.exit_time > changed_at][:MAX_WINDOW]
                if len(before) < MIN_TRADES_BEFORE or len(after) < MIN_TRADES_AFTER:
                    continue

                wr_b, exp_b = self._stats(before)
                wr_a, exp_a = self._stats(after)
                verdict = self._verdict(wr_b, exp_b, wr_a, exp_a)

                action = "none"
                if verdict == "FAILED" and revert_allowed:
                    action = self._revert(entry)

                self.db.save_learning_validation(
                    log_id=entry["id"], verdict=verdict,
                    n_before=len(before), n_after=len(after),
                    win_rate_before=round(wr_b, 4), win_rate_after=round(wr_a, 4),
                    expectancy_before=round(exp_b, 4), expectancy_after=round(exp_a, 4),
                    action=action,
                )
                logger.info(
                    "Validation: log_id=%d %s %s→%s  verdict=%s  "
                    "WR %.0f%%→%.0f%%  exp $%.2f→$%.2f  action=%s",
                    entry["id"], entry.get("param"),
                    entry.get("old_value"), entry.get("new_value"),
                    verdict, wr_b * 100, wr_a * 100, exp_b, exp_a, action,
                )
                self._emit("learning_validation", {
                    "log_id": entry["id"],
                    "param": entry.get("param"),
                    "old_value": entry.get("old_value"),
                    "new_value": entry.get("new_value"),
                    "verdict": verdict,
                    "win_rate_before": round(wr_b, 4),
                    "win_rate_after": round(wr_a, 4),
                    "expectancy_before": round(exp_b, 4),
                    "expectancy_after": round(exp_a, 4),
                    "action": action,
                    "timestamp": datetime.utcnow().isoformat(),
                    "trigger_trade_id": trigger_trade_id,
                })
        except Exception:
            logger.exception("LearningValidator.run failed")

    # ── Internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _stats(trades: List[Any]) -> Tuple[float, float]:
        """(win_rate, expectancy) — expectancy is mean net P&L per trade."""
        wins = sum(1 for t in trades if t.pnl_usd > 0)
        expectancy = sum(t.pnl_usd for t in trades) / len(trades)
        return wins / len(trades), expectancy

    @staticmethod
    def _verdict(wr_b: float, exp_b: float, wr_a: float, exp_a: float) -> str:
        if wr_a >= wr_b + WIN_RATE_IMPROVE and exp_a > exp_b:
            return "VALIDATED"
        if wr_a <= wr_b - WIN_RATE_DEGRADE and exp_a < exp_b:
            return "FAILED"
        return "NEUTRAL"

    @staticmethod
    def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        # learning_log uses sqlite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS",
        # UTC); trades store datetime.utcnow() isoformat. Both naive UTC.
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("Unparseable learning_log timestamp: %r", raw)
            return None

    def _revert(self, entry: Dict[str, Any]) -> str:
        """
        Restore old_value for a FAILED change. Only acts if the change is
        still in force — if the param moved again since (operator edit or a
        later tune), reverting would stomp the newer value, so we stand down.
        Returns 'reverted' or 'stale'.
        """
        param = entry["param"]
        old_value = float(entry["old_value"])
        new_value = float(entry["new_value"])

        config = self.db.get_config()
        current = getattr(config, param, None)
        if current is None:
            return "stale"

        if param in BOOL_PARAMS:
            still_in_force = bool(current) == (new_value >= 0.5)
            restore: Any = old_value >= 0.5
        else:
            still_in_force = abs(float(current) - new_value) < 1e-6
            restore = old_value

        if not still_in_force:
            return "stale"

        setattr(config, param, restore)
        self.db.save_config(config)
        if self.engine:
            setattr(self.engine.config, param, restore)

        self.db.mark_learning_log_reverted(entry["id"])
        rationale = (
            f"{REVERT_RATIONALE_PREFIX} {param} {old_value}→{new_value} FAILED "
            f"validation — restored to {old_value}"
        )
        self.db.save_learning_log(
            param=param,
            old_value=new_value,
            new_value=old_value,
            avg_confidence=1.0,
            rationale=rationale,
            learning_ids=[],
            trigger_trade_id=entry.get("trigger_trade_id") or 0,
        )
        logger.warning("AutoTuner %s", rationale)
        self._emit("auto_tune", {
            "param": param,
            "old_value": new_value,
            "new_value": old_value,
            "avg_confidence": 1.0,
            "rationale": rationale,
            "timestamp": datetime.utcnow().isoformat(),
            "trigger_trade_id": entry.get("trigger_trade_id") or 0,
            "action_type": "VALIDATION_REVERT",
        })
        return "reverted"

    def _emit(self, event: str, data: Dict[str, Any]) -> None:
        if self.socketio:
            try:
                self.socketio.emit(event, data, namespace="/")
            except Exception:
                logger.exception("Socket emit failed for %s", event)
