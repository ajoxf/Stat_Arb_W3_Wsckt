---
name: order-test-suite
description: Build, port, or run the 40-scenario live order test suite (LIMIT/MARKET × spot/futures/delta-neutral + partial-fill recovery) against a crypto exchange. Use after any adapter-layer change, when onboarding a new pair or instrument type, after exchange API updates, or before going live on a new account.
---

# Skill: Live Order Test Suite

The complete implementation reference lives in
`docs/order_test_suite_skill.md` (scenario taxonomy, state machine, async
runner, OKX quirks, quantity/limit-price calculation, partial-fill recovery,
WebSocket UI, error taxonomy, porting checklists). **Read that file in full
before building or modifying the suite.**

## When invoked

1. Determine the goal: run the existing suite, add scenarios, or port it to a
   new exchange/project.
2. Read `docs/order_test_suite_skill.md` — especially §9 (adapter quirks),
   §20 (error taxonomy), and §25 (from-scratch checklist).
3. Confirm environment before sending any order: demo vs live keys
   (`OKX_DEMO_MODE`), and refuse to run against live unless the operator
   explicitly confirms.
4. Run scenarios individually before batch runs; respect the suite's
   rate-limit spacing.
5. Report results as a pass/fail table with per-scenario error codes and a
   P&L/fee breakdown; persist the CSV export.

## Hard rules

- Real orders are placed — tiny quantities near `minSz`, demo mode by default.
- Never leave stub positions: every scenario must reach its close/cleanup
  step even on failure.
- Any new adapter quirk discovered goes into `docs/order_test_suite_skill.md`
  §9 and, if it's an invariant, one line in `CLAUDE.md`.
