---
name: pre-live-check
description: Go-live gate for the trading bot. Use before flipping paper_trading to false, before deploying changes to order placement / exits / P&L accounting, or when the operator asks "is this safe to run live?".
---

# Skill: Pre-Live Checklist

Run every step. Do not skip steps because "the change is small" — the commit
history of this repo is a museum of small changes that auto-closed positions
in a loop. Report each item as PASS / FAIL / N-A with one line of evidence,
then give a single GO / NO-GO verdict. Any FAIL on items 1–7 is NO-GO.

## 1. Unit tests
`pytest tests/ -v` — all green. If the change being shipped fixed a bug,
verify a regression test for that bug exists in this diff.

## 2. Invariant review
Read `CLAUDE.md` "Non-negotiable invariants". For each invariant touched by
the diff, quote the code that preserves it (units/ctVal conversion, posSide,
minSz pre-flight, reduceOnly, bounded retries, clOrdId idempotency,
pending-intent persistence).

## 3. Config sanity
Diff the running config against defaults: leverage caps, notional per leg,
`paper_trading`, `algo_enabled`, fee tier matches the operator's current OKX
statement, daily-loss kill switch enabled.

## 4. Environment gate
`.env` keys match target environment (demo key + live mode = refuse). Confirm
startup performs the 50101 check and clock-drift gate.

## 5. Order test suite (adapter or order-path changes only)
Run the demo-mode order suite (`/order-test-suite`) for the affected order
types. 100% pass required.

## 6. Kill switches
Confirm each risk switch is wired and alert-tested: daily loss limit, basis
blowout, stale data, order-error rate, drawdown halt, Telegram `/pause`.

## 7. Reconciliation
`python scripts/reconcile_okx.py` against the latest history CSV — zero
unexplained diffs between exchange history and bot DB.

## 8. Observability
Telegram bot responds to `/ping`, `/positions`, `/balance`; dashboard shows
live ticks; AI monitor running with a valid `ANTHROPIC_API_KEY` (or
explicitly acknowledged as disabled).

## 9. Rollback plan
State in one sentence how to halt (Telegram `/pause`, dashboard kill, or
process stop) and confirm open positions can be closed manually if the bot
must be stopped mid-position.
