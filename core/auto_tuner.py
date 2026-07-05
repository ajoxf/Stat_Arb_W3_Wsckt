"""
AutoTuner: acts on Claude's typed recommendations.

Four action types, two independent enforcement layers:

LAYER 1 — Claude consensus (requires N agreeing learnings):
  PARAMETER_CHANGE     consensus=3, conf≥0.70 — numeric param within safe corridor
  FILTER_TOGGLE        consensus=5, conf≥0.72 — hurst_enabled / std_filter_enabled
  POSITION_SIZE_CHANGE consensus=4, conf≥0.75 — position_size_usd (reduce only)
  OBSERVATION          no consensus needed    — surface to ai_insights for human review

LAYER 2 — Streak circuit breaker (independent of Claude, fires immediately):
  ≥3 consecutive losses → reduce position_size_usd by 20%
  ≥6 consecutive losses → pause algo entirely
  ≥4 consecutive wins   → step size back up 10% toward the pre-reduction
                          baseline (recovery ladder; never beyond baseline)

LAYER 3 — Validation (LearningValidator, runs every closed trade):
  Each applied change is judged once enough post-change trades exist:
  VALIDATED / NEUTRAL / FAILED. FAILED changes are auto-reverted and the
  same param+direction is blocked from re-application for a cooling-off
  window. Verdicts are fed back into the analyzer prompt.
"""

import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional, TYPE_CHECKING

from core.learning_validator import LearningValidator

if TYPE_CHECKING:
    from database.manager import DatabaseManager

logger = logging.getLogger(__name__)

# ── Consensus thresholds ──────────────────────────────────────────────────────
CONSENSUS_WINDOW   = 5    # learnings to examine for PARAMETER_CHANGE
CONSENSUS_MIN      = 3    # must agree on same param + direction
MIN_CONFIDENCE     = 0.70

FILTER_WINDOW      = 7    # wider window for irreversible-ish boolean changes
FILTER_CONSENSUS   = 5
FILTER_CONFIDENCE  = 0.72

POSITION_WINDOW    = 5
POSITION_CONSENSUS = 4
POSITION_CONFIDENCE = 0.75

# ── Safe corridors for numeric params ────────────────────────────────────────
# (min_val, max_val, max_step_per_adjustment)
SAFE_CORRIDORS: Dict[str, Tuple[float, float, float]] = {
    "entry_threshold":       (1.8, 3.5,  0.20),
    "exit_threshold":        (0.3, 1.0,  0.10),
    "stop_loss_threshold":   (3.0, 5.5,  0.30),
    "min_std_multiple":      (1.0, 2.5,  0.15),
    "slippage_bps":          (1.0, 10.0, 1.00),
    "min_risk_reward":       (0.5, 3.0,  0.25),
}

# ── Filter toggles (bool, 1.0=on / 0.0=off) ─────────────────────────────────
FILTER_TOGGLES: List[str] = ["hurst_enabled", "std_filter_enabled"]

# ── Streak-based circuit breaker (independent of Claude) ─────────────────────
STREAK_REDUCE_AT    = 3    # consecutive losses before reducing size
STREAK_PAUSE_AT     = 6    # consecutive losses before pausing algo
STREAK_REDUCTION    = 0.20 # 20% position size reduction per trigger
STREAK_MIN_SIZE_USD = 200  # never reduce below this

RECOVERY_WINS_AT    = 4    # consecutive wins before stepping size back up
RECOVERY_STEP       = 0.10 # 10% size increase per trigger, capped at baseline

COOLOFF_WINDOW      = 15   # recent log entries checked for reverted changes


class AutoTuner:
    """
    Reads recent learnings from DB and acts on high-confidence consensus
    recommendations. Also enforces streak-based circuit breakers independently
    of Claude's analysis.
    """

    def __init__(self, db: "DatabaseManager", engine=None, socketio=None) -> None:
        self.db = db
        self.engine = engine
        self.socketio = socketio
        self.validator = LearningValidator(db, engine=engine, socketio=socketio)

    # ── Public ───────────────────────────────────────────────────────────────

    def check_and_apply(self, trade_id: int) -> None:
        """
        Entry point after every closed trade analysis.
        Runs streak safety first (independent), then consensus checks.
        Safe to call from any thread.
        """
        try:
            # Layer 2: streak circuit breaker — always runs regardless of auto_tune setting
            self._check_streak_safety(trade_id)
            self._check_streak_recovery(trade_id)

            config = self.db.get_config()

            # Layer 3: judge applied changes. Verdicts always recorded;
            # auto-revert only while the operator has auto-tune on.
            self.validator.engine = self.engine   # engine attaches after app startup
            self.validator.run(
                trade_id,
                revert_allowed=getattr(config, "auto_tune_enabled", False),
            )

            if not getattr(config, "auto_tune_enabled", False):
                return

            # Layer 1: Claude consensus for all recommendation types
            learnings = self.db.get_recent_learnings(limit=max(FILTER_WINDOW, CONSENSUS_WINDOW))
            if not learnings:
                return

            self._process_parameter_changes(learnings[:CONSENSUS_WINDOW], trade_id)
            self._process_filter_toggles(learnings[:FILTER_WINDOW], trade_id)
            self._process_position_changes(learnings[:POSITION_WINDOW], trade_id)
            self._process_observations(learnings[0], trade_id)   # most recent only

        except Exception:
            logger.exception("AutoTuner.check_and_apply error")

    # ── Layer 2: Streak circuit breaker ──────────────────────────────────────

    def _check_streak_safety(self, trigger_trade_id: int) -> None:
        try:
            closed = [t for t in self.db.get_trades(limit=12) if not t.is_open]
            if not closed:
                return

            streak = 0
            for t in closed:
                if t.pnl_usd < 0:
                    streak += 1
                else:
                    break

            if streak < STREAK_REDUCE_AT:
                return

            config = self.db.get_config()

            if streak >= STREAK_PAUSE_AT and config.algo_enabled:
                logger.warning("AutoTuner streak: %d losses — PAUSING ALGO", streak)
                config.algo_enabled = False
                self.db.save_config(config)
                if self.engine and hasattr(self.engine, "toggle_algo"):
                    self.engine.toggle_algo(False)
                self._emit("auto_tune", {
                    "param": "algo_enabled",
                    "old_value": True,
                    "new_value": False,
                    "avg_confidence": 1.0,
                    "rationale": f"Circuit breaker: {streak} consecutive losses — algo paused",
                    "timestamp": datetime.utcnow().isoformat(),
                    "trigger_trade_id": trigger_trade_id,
                    "action_type": "CIRCUIT_BREAKER",
                })
                self.db.save_learning_log(
                    param="algo_enabled",
                    old_value=1.0,
                    new_value=0.0,
                    avg_confidence=1.0,
                    rationale=f"Circuit breaker: {streak} consecutive losses",
                    learning_ids=[],
                    trigger_trade_id=trigger_trade_id,
                )
                return

            if streak >= STREAK_REDUCE_AT:
                current_size = config.position_size_usd
                new_size = round(max(current_size * (1.0 - STREAK_REDUCTION), STREAK_MIN_SIZE_USD), 2)
                if abs(new_size - current_size) < 1.0:
                    return
                logger.warning(
                    "AutoTuner streak: %d losses — reducing size $%.0f → $%.0f",
                    streak, current_size, new_size,
                )
                # Record the operator-chosen size the first time we cut it so
                # the recovery ladder knows what to climb back to.
                if (getattr(config, "position_size_baseline_usd", 0.0) or 0.0) <= 0:
                    config.position_size_baseline_usd = current_size
                config.position_size_usd = new_size
                self.db.save_config(config)
                if self.engine:
                    self.engine.config.position_size_usd = new_size
                    self.engine.config.position_size_baseline_usd = config.position_size_baseline_usd
                self._emit("auto_tune", {
                    "param": "position_size_usd",
                    "old_value": current_size,
                    "new_value": new_size,
                    "avg_confidence": 1.0,
                    "rationale": f"Circuit breaker: {streak} consecutive losses — size reduced",
                    "timestamp": datetime.utcnow().isoformat(),
                    "trigger_trade_id": trigger_trade_id,
                    "action_type": "CIRCUIT_BREAKER",
                })
                self.db.save_learning_log(
                    param="position_size_usd",
                    old_value=current_size,
                    new_value=new_size,
                    avg_confidence=1.0,
                    rationale=f"Circuit breaker: {streak} consecutive losses",
                    learning_ids=[],
                    trigger_trade_id=trigger_trade_id,
                )
        except Exception:
            logger.exception("Streak safety check failed")

    def _check_streak_recovery(self, trigger_trade_id: int) -> None:
        """
        Undo automated size reductions once performance recovers: after
        RECOVERY_WINS_AT consecutive wins, step position_size_usd back up by
        RECOVERY_STEP toward the recorded baseline. Only ever climbs back to
        the size the operator originally chose — never beyond it — so this is
        safe to run regardless of auto_tune_enabled, same as the reducer.
        """
        try:
            config = self.db.get_config()
            baseline = getattr(config, "position_size_baseline_usd", 0.0) or 0.0
            current = config.position_size_usd
            if baseline <= 0 or current >= baseline:
                return

            closed = [t for t in self.db.get_trades(limit=12) if not t.is_open]
            wins = 0
            for t in closed:
                if t.pnl_usd > 0:
                    wins += 1
                else:
                    break
            if wins < RECOVERY_WINS_AT:
                return

            new_size = round(min(baseline, current * (1.0 + RECOVERY_STEP)), 2)
            if new_size <= current:
                return

            config.position_size_usd = new_size
            if new_size >= baseline:
                config.position_size_baseline_usd = 0.0   # ladder complete
            self.db.save_config(config)
            if self.engine:
                self.engine.config.position_size_usd = new_size
                self.engine.config.position_size_baseline_usd = config.position_size_baseline_usd

            logger.info(
                "AutoTuner recovery: %d wins — size $%.0f → $%.0f (baseline $%.0f)",
                wins, current, new_size, baseline,
            )
            self._emit("auto_tune", {
                "param": "position_size_usd",
                "old_value": current,
                "new_value": new_size,
                "avg_confidence": 1.0,
                "rationale": f"Recovery ladder: {wins} consecutive wins — size restored toward ${baseline:,.0f} baseline",
                "timestamp": datetime.utcnow().isoformat(),
                "trigger_trade_id": trigger_trade_id,
                "action_type": "CIRCUIT_RECOVERY",
            })
            self.db.save_learning_log(
                param="position_size_usd",
                old_value=current,
                new_value=new_size,
                avg_confidence=1.0,
                rationale=f"Recovery ladder: {wins} consecutive wins",
                learning_ids=[],
                trigger_trade_id=trigger_trade_id,
            )
        except Exception:
            logger.exception("Streak recovery check failed")

    # ── Layer 1a: Numeric parameter changes ───────────────────────────────────

    def _process_parameter_changes(
        self, learnings: List[Dict[str, Any]], trade_id: int
    ) -> None:
        votes: Dict[str, List[Dict]] = {}
        for lrn in learnings:
            for rec in self._typed_recs(lrn, "PARAMETER_CHANGE"):
                param = rec.get("param", "")
                if param not in SAFE_CORRIDORS:
                    continue
                current  = float(rec.get("current_value", 0))
                suggested = float(rec.get("suggested_value", 0))
                direction = "up" if suggested > current else "down"
                key = f"{param}:{direction}"
                votes.setdefault(key, []).append({
                    "suggested":   suggested,
                    "confidence":  float(rec.get("confidence", 0)),
                    "rationale":   rec.get("rationale", ""),
                    "learning_id": lrn.get("id"),
                })

        for key, vote_list in votes.items():
            if len(vote_list) < CONSENSUS_MIN:
                continue
            avg_conf = sum(v["confidence"] for v in vote_list) / len(vote_list)
            if avg_conf < MIN_CONFIDENCE:
                continue
            param, direction = key.split(":")
            if self._recently_reverted(param, direction):
                logger.info(
                    "AutoTuner: skipping %s:%s — reverted by validation recently (cool-off)",
                    param, direction,
                )
                continue
            suggestions = sorted(v["suggested"] for v in vote_list)
            target  = suggestions[len(suggestions) // 2]
            clamped = self._clamp_numeric(param, target)
            self._apply_numeric(
                param, clamped, avg_conf,
                vote_list[-1]["rationale"],
                [v["learning_id"] for v in vote_list if v["learning_id"]],
                trade_id,
            )

    # ── Layer 1b: Filter toggles ──────────────────────────────────────────────

    def _process_filter_toggles(
        self, learnings: List[Dict[str, Any]], trade_id: int
    ) -> None:
        for param in FILTER_TOGGLES:
            votes_on  = []
            votes_off = []
            for lrn in learnings:
                for rec in self._typed_recs(lrn, "FILTER_TOGGLE"):
                    if rec.get("param") != param:
                        continue
                    suggested = float(rec.get("suggested_value", -1))
                    entry = {
                        "confidence":  float(rec.get("confidence", 0)),
                        "rationale":   rec.get("rationale", ""),
                        "learning_id": lrn.get("id"),
                    }
                    if suggested >= 0.5:
                        votes_on.append(entry)
                    else:
                        votes_off.append(entry)

            for direction, vote_list in [("on", votes_on), ("off", votes_off)]:
                if len(vote_list) < FILTER_CONSENSUS:
                    continue
                avg_conf = sum(v["confidence"] for v in vote_list) / len(vote_list)
                if avg_conf < FILTER_CONFIDENCE:
                    continue
                if self._recently_reverted(param, "up" if direction == "on" else "down"):
                    logger.info(
                        "AutoTuner: skipping %s:%s — reverted by validation recently (cool-off)",
                        param, direction,
                    )
                    continue
                self._apply_filter_toggle(
                    param,
                    new_state=(direction == "on"),
                    avg_conf=avg_conf,
                    rationale=vote_list[-1]["rationale"],
                    learning_ids=[v["learning_id"] for v in vote_list if v["learning_id"]],
                    trigger_trade_id=trade_id,
                )

    # ── Layer 1c: Position size reductions ────────────────────────────────────

    def _process_position_changes(
        self, learnings: List[Dict[str, Any]], trade_id: int
    ) -> None:
        votes = []
        for lrn in learnings:
            for rec in self._typed_recs(lrn, "POSITION_SIZE_CHANGE"):
                if rec.get("param") != "position_size_usd":
                    continue
                suggested = float(rec.get("suggested_value", 0))
                current   = float(rec.get("current_value", suggested + 1))
                if suggested >= current:
                    continue    # Never auto-increase
                votes.append({
                    "suggested":   suggested,
                    "confidence":  float(rec.get("confidence", 0)),
                    "rationale":   rec.get("rationale", ""),
                    "learning_id": lrn.get("id"),
                })

        if len(votes) < POSITION_CONSENSUS:
            return
        avg_conf = sum(v["confidence"] for v in votes) / len(votes)
        if avg_conf < POSITION_CONFIDENCE:
            return

        suggestions = sorted(v["suggested"] for v in votes)
        target = suggestions[len(suggestions) // 2]
        config = self.db.get_config()
        current = config.position_size_usd
        # Max 25% reduction per step, never below minimum
        clamped = round(max(current * 0.75, STREAK_MIN_SIZE_USD, target), 2)
        if clamped >= current:
            return

        self._apply_numeric(
            "position_size_usd", clamped, avg_conf,
            votes[-1]["rationale"],
            [v["learning_id"] for v in votes if v["learning_id"]],
            trade_id,
        )

    # ── Layer 1d: Observations → ai_insights ─────────────────────────────────

    def _process_observations(self, learning: Dict[str, Any], trade_id: int) -> None:
        learning_id = learning.get("id")
        for rec in self._typed_recs(learning, "OBSERVATION"):
            rationale = rec.get("rationale", "").strip()
            if not rationale:
                continue
            # Deduplicate: skip if same param is already pending
            self.db.save_ai_insight(
                learning_id=learning_id,
                trade_id=trade_id,
                insight_type="OBSERVATION",
                param=rec.get("param", "observation"),
                current_value=str(rec.get("current_value", "")),
                suggested_value=str(rec.get("suggested_value", "")),
                confidence=float(rec.get("confidence", 0)),
                rationale=rationale,
            )
            self._emit("ai_insight", {
                "learning_id":   learning_id,
                "trade_id":      trade_id,
                "insight_type":  "OBSERVATION",
                "param":         rec.get("param", "observation"),
                "confidence":    round(float(rec.get("confidence", 0)), 3),
                "rationale":     rationale,
                "timestamp":     datetime.utcnow().isoformat(),
            })

    # ── Application helpers ───────────────────────────────────────────────────

    def _apply_numeric(
        self,
        param: str,
        new_value: float,
        avg_conf: float,
        rationale: str,
        learning_ids: List[int],
        trigger_trade_id: int,
    ) -> None:
        config = self.db.get_config()
        old_value = float(getattr(config, param, new_value))
        if abs(new_value - old_value) < 1e-6:
            return

        setattr(config, param, new_value)
        self.db.save_config(config)
        if self.engine:
            setattr(self.engine.config, param, new_value)

        logger.info(
            "AutoTuner [PARAM]: %s  %.4f → %.4f  conf=%.2f  — %s",
            param, old_value, new_value, avg_conf, rationale,
        )
        self.db.save_learning_log(
            param=param, old_value=old_value, new_value=new_value,
            avg_confidence=avg_conf, rationale=rationale,
            learning_ids=learning_ids, trigger_trade_id=trigger_trade_id,
        )
        self._emit("auto_tune", {
            "param": param, "old_value": old_value, "new_value": new_value,
            "avg_confidence": round(avg_conf, 3),
            "rationale": rationale,
            "timestamp": datetime.utcnow().isoformat(),
            "trigger_trade_id": trigger_trade_id,
            "action_type": "PARAMETER_CHANGE",
        })

    def _apply_filter_toggle(
        self,
        param: str,
        new_state: bool,
        avg_conf: float,
        rationale: str,
        learning_ids: List[int],
        trigger_trade_id: int,
    ) -> None:
        config = self.db.get_config()
        old_state = bool(getattr(config, param, not new_state))
        if old_state == new_state:
            return

        setattr(config, param, new_state)
        self.db.save_config(config)
        if self.engine:
            setattr(self.engine.config, param, new_state)

        old_v = 1.0 if old_state else 0.0
        new_v = 1.0 if new_state else 0.0
        logger.info(
            "AutoTuner [FILTER]: %s  %s → %s  conf=%.2f  — %s",
            param, old_state, new_state, avg_conf, rationale,
        )
        self.db.save_learning_log(
            param=param, old_value=old_v, new_value=new_v,
            avg_confidence=avg_conf, rationale=rationale,
            learning_ids=learning_ids, trigger_trade_id=trigger_trade_id,
        )
        self._emit("auto_tune", {
            "param": param,
            "old_value": old_v, "new_value": new_v,
            "avg_confidence": round(avg_conf, 3),
            "rationale": rationale,
            "timestamp": datetime.utcnow().isoformat(),
            "trigger_trade_id": trigger_trade_id,
            "action_type": "FILTER_TOGGLE",
        })

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _recently_reverted(self, param: str, direction: str) -> bool:
        """
        True if a change to this param in this direction was reverted by the
        validator within the last COOLOFF_WINDOW log entries. Blocks the
        consensus layer from re-applying a proven-bad change while the losing
        recommendations are still inside the learnings window.
        """
        try:
            for entry in self.db.get_learning_log(limit=COOLOFF_WINDOW):
                if entry.get("param") != param or not entry.get("reverted"):
                    continue
                entry_dir = "up" if float(entry["new_value"]) > float(entry["old_value"]) else "down"
                if entry_dir == direction:
                    return True
        except Exception:
            logger.exception("Cool-off check failed for %s", param)
        return False

    def _typed_recs(
        self, learning: Dict[str, Any], rec_type: str
    ) -> List[Dict[str, Any]]:
        """Extract recommendations of a given type from a learning row."""
        recs = json.loads(learning.get("recommendations", "[]"))
        return [
            r for r in recs
            if r.get("type") == rec_type
            # backward compat: if type missing, treat as PARAMETER_CHANGE
            or (rec_type == "PARAMETER_CHANGE" and "type" not in r)
        ]

    def _clamp_numeric(self, param: str, value: float) -> float:
        min_v, max_v, max_step = SAFE_CORRIDORS[param]
        config = self.db.get_config()
        current = float(getattr(config, param, value))
        if value > current:
            value = min(value, current + max_step)
        else:
            value = max(value, current - max_step)
        return round(max(min_v, min(max_v, value)), 4)

    def _emit(self, event: str, data: Dict[str, Any]) -> None:
        if self.socketio:
            try:
                self.socketio.emit(event, data, namespace="/")
            except Exception:
                pass
