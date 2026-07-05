# CLAUDE.md — project memory for Claude Code

Auto-loaded at the start of every Claude Code session in this repo. Keep it short,
factual, and current — this file is the replacement for re-pasting long briefs.

## What this is

Delta-neutral **spot vs perpetual-futures statistical arbitrage bot** on OKX
(Flask + SocketIO dashboard, Python 3.11, asyncio engine). **Real money flows
through this code.** Treat every change to order placement, position accounting,
or P&L math as production-critical.

## Architecture map

- `app.py` — Flask + SocketIO entry point
- `core/trading_engine.py` — orchestrator; owns the single asyncio event loop
- `core/signals.py` — z-score / Hurst / STD-filter signal generation
- `core/order_executor.py` — coordinated 2-leg order placement
- `core/ai_monitor.py` — periodic Claude-powered health review (Telegram alerts)
- `core/post_trade_analyzer.py` + `core/auto_tuner.py` — learning loop
- `adapters/okx_adapter.py` — OKX V5 REST + WebSocket
- `database/` — sqlite: positions, trades, signals, learnings
- `backtest/` — history fetch, replay, analysis
- `docs/00…12_*.md` — numbered subsystem docs; read the relevant one before
  touching that subsystem
- `funding_arb_build_prompt.md` §4 — **OKX quirks reference** (units, posSide,
  ctVal, error codes); §6 — hard guardrails; §10 — anti-patterns. Authoritative.

## Non-negotiable invariants (violations have lost money before)

1. `sz` for cross-margin SPOT MARKET BUY is **USDT notional**, not base qty.
2. SWAP quantities are **contracts**; always convert via `ctVal`. Never assume
   1 contract = 1 base asset.
3. Trust `posSide`, never the sign of `pos`.
4. Every close is pre-flighted against `minSz`; dust (< $1) is never "closed".
5. `reduceOnly: true` on every closing SWAP order. Error `51169` means engine
   and exchange disagree — reconcile first, never blind-retry.
6. Bounded retries only (`[2,4,8,16]s`); never retry rate limits indefinitely.
7. Idempotent placement via `clOrdId`; on retry-after-timeout, query by
   `clOrdId` before re-placing.
8. Persist intent (`status='pending'`) before any state-changing operation;
   on startup reconcile against the exchange, never against the DB alone.
9. Credentials live in `.env` only. Never in the DB, never in a dead UI form.
10. No `except: pass`. Every suppressed exception logs with `logger.exception`.
11. Exit/stop decisions use the **rolling** current z-score; stop-loss uses the
    **frozen entry reference** (see commits 4b177c9, "frozen entry reference").

## Common OKX error codes (we hit all of these)

`50101` demo/live key mismatch · `50013` transient busy (backoff) ·
`50102` clock drift (resync, refuse orders if >15s) · `51169` reduce-only
would not reduce (reconcile) · `51020` below minSz · `50110` IP not whitelisted.

## Commands

```bash
pip install -r requirements.txt
python app.py                      # dashboard at :5000
pytest tests/ -v                   # unit tests (mocked adapter)
python scripts/reconcile_okx.py    # diff OKX history CSV vs bot DB
bash test_orders.sh                # live order suite helpers
```

Project skills (see `.claude/skills/`): `post-trade-analyzer`,
`order-test-suite`, `pre-live-check`.

## Working style (operator preferences)

- Two-sentence end-of-turn summaries.
- Comment WHY, not WHAT; identifiers self-document.
- No defensive coding for impossible cases.
- No planning documents unless asked.
- Commit and push every working step; never leave the tree dirty.
- Before touching order placement / P&L / exits: state the invariant(s) above
  that apply, then run `pytest tests/` and, for adapter changes, the demo-mode
  order test suite before declaring done.
