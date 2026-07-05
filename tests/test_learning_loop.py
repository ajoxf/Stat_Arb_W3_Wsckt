"""
Unit tests for the self-learning loop:

- Risk/reward entry gate (signals._check_risk_reward)
- AutoTuner streak circuit breaker: baseline recording + recovery ladder
- LearningValidator: verdicts, auto-revert, stale detection, selection-bias
  exclusions, and the re-application cool-off
"""

import pytest
from datetime import datetime, timedelta

from core.auto_tuner import AutoTuner, STREAK_REDUCTION, RECOVERY_STEP
from core.learning_validator import LearningValidator
from core.signals import SignalGenerator
from database.manager import DatabaseManager
from models import Trade, TradingConfig


NOW = datetime.utcnow()


@pytest.fixture
def db(tmp_path):
    return DatabaseManager(db_path=str(tmp_path / "test.db"))


def make_trade(pnl: float, exit_dt: datetime, asset: str = "BTC") -> Trade:
    return Trade(
        asset=asset,
        position_type="LONG",
        entry_time=exit_dt - timedelta(minutes=30),
        exit_time=exit_dt,
        entry_zscore=2.1,
        exit_zscore=0.4,
        pnl_usd=pnl,
        exit_reason="EXIT" if pnl > 0 else "STOP_LOSS",
        is_open=False,
        is_paper=False,
    )


def save_trades(db, pnls, start: datetime):
    """
    Persist closed trades one minute apart, oldest first. save_trade only
    writes P&L/exit fields on the UPDATE path (mirroring open→close in the
    engine), so insert first, then save again with the id set.
    """
    for i, pnl in enumerate(pnls):
        trade = make_trade(pnl, start + timedelta(minutes=i))
        trade.id = db.save_trade(trade)
        db.save_trade(trade)


# ── Risk/reward entry gate ────────────────────────────────────────────────────

class TestRiskRewardFilter:
    def _generator(self, **overrides) -> SignalGenerator:
        config = TradingConfig(
            entry_threshold=2.0, exit_threshold=0.5, stop_loss_threshold=4.0,
            hedge_ratio=1.0, **overrides,
        )
        sg = SignalGenerator(config)
        sg.current_std = 10.0
        sg.spot_prices.append(100.0)
        return sg

    def test_disabled_always_passes(self):
        sg = self._generator(risk_reward_filter_enabled=False)
        ok, ratio = sg._check_risk_reward(2.5)
        assert ok is True and ratio is None

    def test_ratio_matches_cost_adjusted_formula(self):
        sg = self._generator(risk_reward_filter_enabled=True, min_risk_reward=0.5)
        costs = (sg._compute_round_trip_cost()["round_trip_bps"] / 10000) * 100.0
        ok, ratio = sg._check_risk_reward(3.0)
        expected = ((3.0 - 0.5) * 10.0 - costs) / ((4.0 - 3.0) * 10.0 + costs)
        assert ratio == pytest.approx(expected, abs=1e-3)
        assert ok is (ratio >= 0.5)

    def test_blocks_below_floor_and_passes_above(self):
        sg = self._generator(risk_reward_filter_enabled=True, min_risk_reward=1.0)
        # Just past the entry threshold: reward 1.5σ vs risk 2.0σ → ratio < 1
        ok_low, ratio_low = sg._check_risk_reward(2.0)
        assert ok_low is False and ratio_low < 1.0
        # Deep entry: reward 3.0σ vs risk 0.5σ → ratio well above 1
        ok_high, ratio_high = sg._check_risk_reward(3.5)
        assert ok_high is True and ratio_high > 1.0

    def test_fails_closed_on_degenerate_state(self):
        sg = self._generator(risk_reward_filter_enabled=True)
        sg.current_std = 0.0
        assert sg._check_risk_reward(2.5) == (False, 0.0)
        sg.current_std = 10.0
        # At the stop level there is no risk budget left
        assert sg._check_risk_reward(4.0) == (False, 0.0)

    def test_costs_can_erase_the_reward(self):
        sg = self._generator(risk_reward_filter_enabled=True)
        sg.current_std = 0.001   # reward in spread units ≈ 0 vs real costs
        ok, ratio = sg._check_risk_reward(2.5)
        assert ok is False and ratio == 0.0


# ── Streak circuit breaker + recovery ladder ─────────────────────────────────

class TestStreakBreakerAndRecovery:
    def test_loss_streak_reduces_size_and_records_baseline(self, db):
        save_trades(db, [5.0, -1.0, -1.0, -1.0], NOW - timedelta(hours=1))
        tuner = AutoTuner(db)
        tuner.check_and_apply(trade_id=1)

        config = db.get_config()
        assert config.position_size_usd == pytest.approx(1000.0 * (1 - STREAK_REDUCTION))
        assert config.position_size_baseline_usd == pytest.approx(1000.0)

    def test_win_streak_climbs_back_to_baseline_and_clears_it(self, db):
        config = db.get_config()
        config.position_size_usd = 950.0
        config.position_size_baseline_usd = 1000.0
        db.save_config(config)
        save_trades(db, [2.0, 2.0, 2.0, 2.0], NOW - timedelta(hours=1))

        tuner = AutoTuner(db)
        tuner.check_and_apply(trade_id=1)

        config = db.get_config()
        # 950 × 1.10 = 1045 → capped at the 1000 baseline, ladder complete
        assert config.position_size_usd == pytest.approx(1000.0)
        assert config.position_size_baseline_usd == 0.0

    def test_recovery_steps_partially_when_far_from_baseline(self, db):
        config = db.get_config()
        config.position_size_usd = 500.0
        config.position_size_baseline_usd = 1000.0
        db.save_config(config)
        save_trades(db, [2.0, 2.0, 2.0, 2.0], NOW - timedelta(hours=1))

        AutoTuner(db).check_and_apply(trade_id=1)

        config = db.get_config()
        assert config.position_size_usd == pytest.approx(500.0 * (1 + RECOVERY_STEP))
        assert config.position_size_baseline_usd == pytest.approx(1000.0)

    def test_no_recovery_without_baseline(self, db):
        save_trades(db, [2.0, 2.0, 2.0, 2.0], NOW - timedelta(hours=1))
        AutoTuner(db).check_and_apply(trade_id=1)
        assert db.get_config().position_size_usd == pytest.approx(1000.0)

    def test_six_losses_pauses_algo(self, db):
        config = db.get_config()
        config.algo_enabled = True
        db.save_config(config)
        save_trades(db, [-1.0] * 6, NOW - timedelta(hours=1))

        AutoTuner(db).check_and_apply(trade_id=1)
        assert db.get_config().algo_enabled is False


# ── Validation & auto-revert ──────────────────────────────────────────────────

def log_change(db, param="entry_threshold", old=2.0, new=2.2, rationale="test change"):
    db.save_learning_log(
        param=param, old_value=old, new_value=new,
        avg_confidence=0.8, rationale=rationale,
        learning_ids=[], trigger_trade_id=1,
    )
    return db.get_learning_log(limit=1)[0]["id"]


class TestLearningValidator:
    def test_failed_change_is_reverted(self, db):
        # Healthy before the change, poor after → FAILED → revert
        save_trades(db, [2.0, 2.0, -1.0, 2.0, 2.0, 2.0], NOW - timedelta(hours=2))
        log_id = log_change(db)
        config = db.get_config()
        config.entry_threshold = 2.2   # the change is in force
        db.save_config(config)
        save_trades(db, [-1.0, -1.0, 2.0, -1.0, -1.0, -1.0], NOW + timedelta(minutes=5))

        LearningValidator(db).run(trigger_trade_id=1, revert_allowed=True)

        assert db.get_config().entry_threshold == pytest.approx(2.0)
        entry = next(e for e in db.get_learning_log(limit=10) if e["id"] == log_id)
        assert entry["reverted"] == 1
        joined = db.get_learning_log_with_validation(limit=10)
        judged = next(e for e in joined if e["id"] == log_id)
        assert judged["verdict"] == "FAILED" and judged["v_action"] == "reverted"
        # The revert itself was logged for the audit trail
        assert any(
            (e.get("rationale") or "").startswith("Auto-revert:")
            for e in db.get_learning_log(limit=10)
        )

    def test_stale_change_is_not_stomped(self, db):
        save_trades(db, [2.0, 2.0, -1.0, 2.0, 2.0, 2.0], NOW - timedelta(hours=2))
        log_id = log_change(db)
        config = db.get_config()
        config.entry_threshold = 2.5   # operator moved it again since
        db.save_config(config)
        save_trades(db, [-1.0, -1.0, 2.0, -1.0, -1.0, -1.0], NOW + timedelta(minutes=5))

        LearningValidator(db).run(trigger_trade_id=1, revert_allowed=True)

        assert db.get_config().entry_threshold == pytest.approx(2.5)
        joined = db.get_learning_log_with_validation(limit=10)
        judged = next(e for e in joined if e["id"] == log_id)
        assert judged["verdict"] == "FAILED" and judged["v_action"] == "stale"

    def test_no_revert_when_auto_tune_disabled(self, db):
        save_trades(db, [2.0, 2.0, -1.0, 2.0, 2.0, 2.0], NOW - timedelta(hours=2))
        log_change(db)
        config = db.get_config()
        config.entry_threshold = 2.2
        db.save_config(config)
        save_trades(db, [-1.0, -1.0, 2.0, -1.0, -1.0, -1.0], NOW + timedelta(minutes=5))

        LearningValidator(db).run(trigger_trade_id=1, revert_allowed=False)

        assert db.get_config().entry_threshold == pytest.approx(2.2)

    def test_improvement_is_validated(self, db):
        save_trades(db, [-1.0, -1.0, 2.0, -1.0, -1.0, 2.0], NOW - timedelta(hours=2))
        log_id = log_change(db)
        save_trades(db, [2.0, 2.0, 2.0, -1.0, 2.0, 2.0], NOW + timedelta(minutes=5))

        LearningValidator(db).run(trigger_trade_id=1, revert_allowed=True)

        joined = db.get_learning_log_with_validation(limit=10)
        judged = next(e for e in joined if e["id"] == log_id)
        assert judged["verdict"] == "VALIDATED"

    def test_insufficient_post_change_trades_defers_verdict(self, db):
        save_trades(db, [2.0, 2.0, -1.0, 2.0, 2.0, 2.0], NOW - timedelta(hours=2))
        log_id = log_change(db)
        save_trades(db, [-1.0, -1.0], NOW + timedelta(minutes=5))

        LearningValidator(db).run(trigger_trade_id=1, revert_allowed=True)

        assert log_id not in db.get_validated_log_ids()

    def test_breaker_and_size_entries_are_never_judged(self, db):
        save_trades(db, [2.0, 2.0, -1.0, 2.0, 2.0, 2.0], NOW - timedelta(hours=2))
        breaker_id = log_change(
            db, param="position_size_usd", old=1000, new=800,
            rationale="Circuit breaker: 3 consecutive losses",
        )
        save_trades(db, [-1.0, -1.0, 2.0, -1.0, -1.0, -1.0], NOW + timedelta(minutes=5))

        LearningValidator(db).run(trigger_trade_id=1, revert_allowed=True)

        assert breaker_id not in db.get_validated_log_ids()
        assert db.get_config().position_size_usd == pytest.approx(1000.0)

    def test_verdict_is_terminal(self, db):
        save_trades(db, [-1.0, -1.0, 2.0, -1.0, -1.0, 2.0], NOW - timedelta(hours=2))
        log_id = log_change(db)
        save_trades(db, [2.0, 2.0, 2.0, -1.0, 2.0, 2.0], NOW + timedelta(minutes=5))

        validator = LearningValidator(db)
        validator.run(trigger_trade_id=1, revert_allowed=True)
        validator.run(trigger_trade_id=2, revert_allowed=True)

        joined = db.get_learning_log_with_validation(limit=20)
        assert sum(1 for e in joined if e["id"] == log_id and e["verdict"]) == 1


# ── Re-application cool-off ───────────────────────────────────────────────────

class TestCoolOff:
    def test_reverted_direction_is_blocked(self, db):
        log_id = log_change(db, param="entry_threshold", old=2.0, new=2.2)
        db.mark_learning_log_reverted(log_id)
        tuner = AutoTuner(db)
        assert tuner._recently_reverted("entry_threshold", "up") is True
        assert tuner._recently_reverted("entry_threshold", "down") is False
        assert tuner._recently_reverted("exit_threshold", "up") is False
