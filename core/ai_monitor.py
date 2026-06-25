"""
AI-powered SRE monitor for the trading bot.

Periodically samples a snapshot of engine state + recent log tail and asks
Claude to classify bot health as ok / warn / critical. Warn and critical
verdicts are pushed to Telegram via the existing notifier. Designed to
catch silent failures that don't trip explicit alerts (rate-limit storms,
clock drift, stuck positions, repeated order failures).

Self-disables if ANTHROPIC_API_KEY is missing or the `anthropic` package
isn't installed. Repeats are suppressed: the same severity+summary won't
re-alert within ALERT_REPEAT_SUPPRESS_SEC.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.trading_engine import TradingEngine

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024
_LOG_TAIL_LINES = 80
_DEFAULT_INTERVAL_SEC = 15 * 60
ALERT_REPEAT_SUPPRESS_SEC = 2 * 60 * 60  # don't re-alert same condition for 2h

_MONITOR_TOOL = {
    "name": "report_bot_health",
    "description": (
        "Report the trading bot's current operational health. Severity rules: "
        "'ok' = normal operation, no human action needed. "
        "'warn' = degraded but recoverable (rate limits, stale data, minor drift). "
        "'critical' = stuck position, repeated order failures, clock drift causing "
        "rejections, exchange auth issues, or anything that needs human intervention now."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": ["ok", "warn", "critical"]},
            "summary": {
                "type": "string",
                "description": "Two short sentences describing bot health and any issue spotted.",
            },
            "action_recommended": {
                "type": "string",
                "description": (
                    "Optional: a concrete action the operator should take "
                    "(e.g. 'resync Windows clock', 'restart bot'). "
                    "Empty string if no action needed."
                ),
            },
        },
        "required": ["severity", "summary"],
    },
}


class AIMonitor:
    def __init__(self, engine: "TradingEngine", interval_sec: int = _DEFAULT_INTERVAL_SEC) -> None:
        self.engine = engine
        self.interval_sec = interval_sec
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
        self._last_run: Optional[datetime] = None
        self._last_severity: str = "ok"
        self._last_summary: str = ""
        self._last_action: str = ""
        self._last_alert_key: Optional[tuple] = None
        self._last_alert_time: Optional[datetime] = None

        if not self._api_key:
            logger.info("AI monitor: ANTHROPIC_API_KEY not set — monitor disabled")

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self._api_key:
            return
        if self._task and not self._task.done():
            return
        self._stop = False
        try:
            self._task = asyncio.create_task(self._loop(), name="ai-monitor")
            logger.info("AI monitor started (interval=%ds)", self.interval_sec)
        except RuntimeError:
            logger.warning("AI monitor: no running event loop — start skipped")

    def stop(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        # Delay the first check so the bot can settle after startup.
        try:
            await asyncio.sleep(self.interval_sec)
        except asyncio.CancelledError:
            return
        while not self._stop:
            try:
                await asyncio.to_thread(self._run_check)
            except Exception:
                logger.exception("AI monitor check failed (continuing)")
            try:
                await asyncio.sleep(self.interval_sec)
            except asyncio.CancelledError:
                break

    # ── core check (runs in worker thread) ───────────────────────────────────
    def _run_check(self) -> None:
        snapshot = self._gather_snapshot()

        try:
            import anthropic
        except ImportError:
            logger.error("AI monitor: anthropic package not installed; disabling")
            self._stop = True
            return

        client = anthropic.Anthropic(api_key=self._api_key)
        prompt = self._build_prompt(snapshot)
        try:
            msg = client.messages.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                tools=[_MONITOR_TOOL],
                tool_choice={"type": "tool", "name": "report_bot_health"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            err_str = str(exc)
            if "credit balance" in err_str.lower() or "insufficient" in err_str.lower():
                logger.warning(
                    "AI monitor: Anthropic API credits exhausted — disabling monitor. "
                    "Add credits at console.anthropic.com to re-enable (requires restart)."
                )
                self._stop = True
                return
            logger.warning("AI monitor: API call failed: %s", exc)
            return

        verdict = self._extract_verdict(msg)
        if not verdict:
            logger.warning("AI monitor: no tool_use in response")
            return

        severity = verdict.get("severity", "ok")
        summary = verdict.get("summary", "").strip()
        action = (verdict.get("action_recommended") or "").strip()

        self._last_run = datetime.now(timezone.utc)
        self._last_severity = severity
        self._last_summary = summary
        self._last_action = action
        logger.info("[AI MONITOR] %s — %s", severity.upper(), summary)

        if severity in ("warn", "critical"):
            self._maybe_alert(severity, summary, action)

    def _maybe_alert(self, severity: str, summary: str, action: str) -> None:
        """Send to Telegram, deduping repeats of the same condition."""
        key = (severity, summary)
        now = datetime.now(timezone.utc)
        if (
            key == self._last_alert_key
            and self._last_alert_time
            and (now - self._last_alert_time).total_seconds() < ALERT_REPEAT_SUPPRESS_SEC
        ):
            return
        try:
            from core.telegram_bot import get_notifier
            notifier = get_notifier()
            body = f"AI monitor [{severity.upper()}]: {summary}"
            if action:
                body += f"\nSuggested action: {action}"
            notifier.notify_error(body)
            self._last_alert_key = key
            self._last_alert_time = now
        except Exception:
            logger.exception("AI monitor: failed to push Telegram alert")

    # ── context gathering ───────────────────────────────────────────────────
    def _gather_snapshot(self) -> Dict[str, Any]:
        eng = self.engine
        snap: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "asset": getattr(eng.config, "asset", "?"),
            "paper_trading": getattr(eng.state, "paper_trading", None),
            "algo_enabled": getattr(eng.state, "algo_enabled", None),
            "current_position": getattr(eng.state, "current_position", None),
            "position_mismatch": getattr(eng, "_position_mismatch", None),
            "orphan_mismatch_count": getattr(eng, "_orphan_mismatch_count", 0),
        }
        sg = getattr(eng, "signal_generator", None)
        if sg is not None:
            try:
                snap["zscore"] = round(sg.current_zscore, 3)
                snap["spread"] = round(sg.current_spread, 4)
                snap["spread_std"] = round(sg.current_std, 4)
                snap["spread_mean"] = round(sg.current_mean, 4)
                snap["hurst"] = round(sg.current_hurst, 3)
                snap["half_life"] = sg.current_half_life
                snap["hurst_filter_enabled"] = getattr(sg.config, "hurst_enabled", True)
                snap["hurst_threshold"] = getattr(sg.config, "hurst_threshold", 0.5)
                snap["hurst_blocking_entries"] = (
                    getattr(sg.config, "hurst_enabled", True)
                    and sg.current_hurst >= getattr(sg.config, "hurst_threshold", 0.5)
                )
            except Exception:
                pass
        snap["recent_log_tail"] = self._tail_log(_LOG_TAIL_LINES)
        return snap

    @staticmethod
    def _tail_log(n: int) -> List[str]:
        log_dir = Path("logs")
        if not log_dir.exists():
            return ["(logs/ directory not found — file logging not configured)"]
        # TimedRotatingFileHandler writes "trading.log" (current) + "trading.logYYYYMMDD" (rotated)
        candidates = sorted(log_dir.glob("trading.log*"), reverse=True)
        if not candidates:
            # Fallback: legacy trading_*.log naming
            candidates = sorted(log_dir.glob("trading_*.log"), reverse=True)
        if not candidates:
            return ["(no trading log files found in logs/)"]
        try:
            text = candidates[0].read_text(errors="replace")
            return text.splitlines()[-n:]
        except Exception:
            return []

    @staticmethod
    def _build_prompt(snap: Dict[str, Any]) -> str:
        log_lines = snap.pop("recent_log_tail", [])
        log_block = "\n".join(log_lines) if log_lines else "(no recent log lines)"
        import json
        state_block = json.dumps(snap, default=str, indent=2)
        return (
            "You are an SRE monitoring a crypto spot+futures statistical-arbitrage "
            "trading bot. Review the snapshot below and call report_bot_health.\n\n"
            "STRICT RULES — violations result in incorrect verdicts:\n"
            "1. If hurst_filter_enabled is false, the Hurst exponent is INFORMATIONAL ONLY. "
            "Do NOT mention Hurst in the summary and do NOT use it to influence severity. "
            "A disabled filter is a deliberate operator choice, not a risk.\n"
            "2. If algo_enabled is false, no trades can be triggered. Do NOT warn about "
            "z-score proximity to entry thresholds when the algo is disabled.\n"
            "3. paper_trading true = simulated mode; treat open positions as expected, not stuck.\n\n"
            "Look for: stuck positions (live mode only), position_mismatch flagged True, "
            "repeated OKX error codes (50102 clock drift, 50013 rate limit storms, "
            "51169 reduce-only failures), engine stuck in a single state for an "
            "unreasonable period, or anything else suggesting the bot needs attention.\n\n"
            "Keep summary to TWO sentences. Suggest action only when severity is "
            "warn or critical.\n\n"
            f"STATE:\n{state_block}\n\n"
            f"RECENT LOG TAIL ({len(log_lines)} lines):\n{log_block}\n"
        )

    @staticmethod
    def _extract_verdict(msg) -> Optional[Dict[str, Any]]:
        for block in getattr(msg, "content", []) or []:
            if getattr(block, "type", None) == "tool_use":
                return dict(getattr(block, "input", {}) or {})
        return None

    # ── status accessor (for /api/ai-monitor) ────────────────────────────────
    def get_status(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self._api_key),
            "running": bool(self._task and not self._task.done()),
            "interval_sec": self.interval_sec,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "last_severity": self._last_severity,
            "last_summary": self._last_summary,
            "last_action": self._last_action,
        }
