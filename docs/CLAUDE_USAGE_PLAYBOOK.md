# Claude Usage Audit & Playbook

Audit of how Claude has been used on this project (evidence: full git history
2026-05-18 → 2026-06-18, 102 commits / 80 Claude-authored, prompt files, docs,
skills), with categorized recommendations for future usage. Written 2026-07-05.

---

## 1. What the history shows

**Volume & cadence.** 102 commits in one month, concentrated in marathon
bursts: 12 (May 18), 27 (Jun 12), 30 (Jun 17), 17 (Jun 18) commits in single
days. Claude authored ~80% of commits. This is heavy, productive usage — but
the burst pattern means long sessions where context degrades and fixes stack
on fixes.

**Firefighting ratio.** 14+ commits begin with `Fix/CRITICAL/ROOT CAUSE`, and
the messages narrate production incidents on a live-money system:
"CRITICAL: defensive override stops destructive auto-close loop",
"Fix infinite exit retry loop caused by OKX error 51169",
"Telegram: stop main Settings save from wiping bot config",
"Fix all 29 audit issues: critical → high → medium → low".
The same UI bug was fixed twice in consecutive commits
("Fix blocked signal card flickering…" then "Fix last-blocked-signal
flickering every tick") — a signature of shipping fixes without verifying them.

**Branch topology.** 22 of 102 commits are merges, mostly the same long-lived
session branch (`claude/crypto-arbitrage-system-RN3Jr`) merged repeatedly into
`claude/multi-pair-backtest`, across **two repos** (Stat_Arb_W3 →
Stat_Arb_W3_Wsckt). No PRs, no CI (`.github/` absent), no review gate.

**Test surface.** One unit-test file (14 tests, 397 lines) for ~18,300 lines
of Python. The excellent 40-scenario *live* order suite exists, but it's slow,
costs real API calls, and can't run per-commit. Most of the bugs in the commit
log (exit function returning True on failure, P&L ordering, fee labels) are
unit-testable with a mocked adapter.

**Knowledge management.** The strongest artifact in the repo is
`funding_arb_build_prompt.md` — a 417-line build brief with phased gates,
exchange quirks, anti-patterns learned from real losses, and a working-style
contract. This is elite prompt engineering. But it's a *manual paste* pattern:
no `CLAUDE.md` existed, so every fresh session rediscovered the codebase, and
lessons lived in a file Claude only saw if you remembered to paste it.

**Skills that never fired.** `.claude/skills/post-trade-analyzer.md` was a
bare markdown file — Claude Code discovers skills at
`.claude/skills/<name>/SKILL.md` with YAML frontmatter, so this 745-line skill
was invisible to the tool it was written for. Same for
`docs/order_test_suite_skill.md`. Both are now installed correctly.

---

## 2. Strengths — keep doing these

1. **The build-brief pattern.** Mission → strategy math → API reference →
   quirks → phased build with *gates* → guardrails → anti-patterns → working
   style. Reuse this template for every new project; it's the single biggest
   quality lever in your history.
2. **Encoding losses as anti-patterns.** §10 of the build prompt ("the prior
   project lost 30 minutes hammering the API on uncloseable dust", "double-
   placed during retries") turns incidents into permanent instructions. Now
   institutionalized in `CLAUDE.md` so it loads automatically.
3. **Numbered subsystem docs** (`docs/00…12`). Point Claude at the one
   relevant doc instead of letting it re-derive the architecture.
4. **Claude as a runtime component**, not just a dev tool: `ai_monitor.py`,
   `post_trade_analyzer.py`, `auto_tuner.py`. Few users think this way.
5. **Phased go-live gates** (backtest → paper → single-pair live → multi-pair)
   with quantitative pass criteria.

## 3. Weaknesses — and the fix for each

| # | Weakness | Evidence | Fix |
|---|----------|----------|-----|
| 1 | No persistent project memory | No CLAUDE.md; 417-line brief pasted manually | `CLAUDE.md` (now committed). Update it whenever a bug teaches an invariant — one line each. |
| 2 | Fixes shipped unverified | Same flicker bug fixed twice; repeated CRITICAL loops | Ask Claude to **verify before declaring done** (`/verify` skill in Claude Code); require it to show the failing → passing evidence. |
| 3 | Test coverage far below the stakes | 14 unit tests vs live-money order/P&L code | Rule: any bug that reaches a `Fix:` commit gets a regression unit test in the same commit. Ask Claude to write the test *first*, from the bug report. |
| 4 | No CI, no PR gate | `.github/` absent; direct merges | Add GitHub Actions running `pytest`; work in short-lived branches → PR; in Claude Code web, ask Claude to **watch the PR** (it can auto-fix CI failures and respond to review comments). |
| 5 | Marathon sessions | 27–30 commits/day bursts | One session ≈ one scoped task. Start big features in **plan mode** (Shift+Tab twice in the CLI) so the approach is agreed before code is written. |
| 6 | Skills in undiscoverable format | Bare `.md` in `.claude/skills/` | Fixed: skills now live at `.claude/skills/<name>/SKILL.md` with frontmatter; invoke as `/post-trade-analyzer`, `/order-test-suite`, `/pre-live-check`. |
| 7 | Branch/merge sprawl across 2 repos | 22 merge commits of one session branch | Short-lived `claude/<task>` branches off main, deleted after merge. |
| 8 | AI monitor pins an old model, no cost hygiene | `_MODEL = "claude-sonnet-4-6"` | For a periodic log-scanning health check, use the current small model (Haiku 4.5: `claude-haiku-4-5-20251001`) — ~10× cheaper — or current Sonnet (`claude-sonnet-5`) if the review needs depth. Re-check model IDs against docs.claude.com when bumping. |
| 9 | Repo migration lost history | This repo starts at 2026-05-18; predecessor history unreachable | Prefer branches within one repo; if migrating, `git clone` with history. |

## 4. Opportunities — things you're not using yet

1. **Plan mode for anything touching order flow.** Money-path changes should
   never start with Claude editing files; start with a reviewed plan.
2. **PR babysitting.** From Claude Code web/GitHub integration: open a PR,
   then tell Claude to watch it — it subscribes to CI + review events and
   pushes fixes autonomously.
3. **Scheduled routines.** Claude Code (web) supports scheduled triggers.
   Natural fits: nightly `scripts/reconcile_okx.py` + anomaly summary to you;
   weekly batch run of `/post-trade-analyzer` over the week's closed trades;
   monthly "audit my config drift vs backtest assumptions".
4. **Hooks.** A `PostToolUse`/`Stop` hook that runs `pytest tests/ -q` so a
   session cannot end green with broken tests. A `SessionStart` hook to
   `pip install -r requirements.txt` in fresh web containers.
5. **`/fewer-permission-prompts`** (built-in skill) to allowlist the read-only
   commands you approve constantly.
6. **Headless Claude in ops** — you already do this (ai_monitor); extend the
   pattern: pipe the reconcile diff through `claude -p "flag anything that
   looks like a fill-accounting bug"` in a cron job.
7. **Recover the other 11 months.** On your local machine: `claude` transcripts
   live under `~/.claude/projects/`; `/usage` shows consumption; claude.ai →
   Settings → Privacy → export data gives chat history. Add the predecessor
   repo (`Stat_Arb_W3`) to a session (`add repo ajoxf/Stat_Arb_W3`) to extend
   this audit over its history.

## 5. Areas of interest (what your history says you care about)

- **Execution correctness** on OKX (units, posSide, reduce-only, partial fills)
  — the dominant theme, ~40% of commits.
- **P&L truthfulness** (net vs gross, fees, funding, reconciliation).
- **Operator observability** (Telegram commands, dashboard cards, AI monitor).
- **Self-improving systems** (post-trade learnings, auto-tuner, safe corridors).
- **Portability playbooks** (order-suite porting guide, build brief for the
  successor repo) — you naturally write for reuse; skills are the right
  container for this instinct.

## 6. Command & skill library — save these

### Built-in Claude Code commands to make habitual
| Command | When |
|---|---|
| Plan mode (Shift+Tab ×2) | Before any order-flow / P&L change |
| `/code-review` | Before merging any branch |
| `/security-review` | Before anything touching keys, .env, auth |
| `/verify` | After any fix — prove it end-to-end |
| `/init` | First session in any new repo (generates CLAUDE.md) |
| `/fewer-permission-prompts` | Once per repo to cut prompt fatigue |
| `/usage`, `/context`, `/compact` | Monitor long sessions; compact at natural breakpoints instead of letting context degrade |

### Project skills (in `.claude/skills/`)
| Skill | Purpose |
|---|---|
| `/post-trade-analyzer` | 8-phase trade autopsy → structured learnings → safe-corridor recommendations |
| `/order-test-suite` | Build/port/run the 40-scenario live order suite (full reference in `docs/order_test_suite_skill.md`) |
| `/pre-live-check` | Gate before flipping paper→live or deploying order-path changes |

### Reusable prompt templates (keep in repo, paste when needed)
- `funding_arb_build_prompt.md` — the new-project build-brief template.
- "Regression-test-first": *"Here is the bug report. Write a failing unit test
  that reproduces it, show me the failure, then fix it, then show the pass."*
- "Invariant check": *"Before editing, list which CLAUDE.md invariants this
  change touches and how you'll avoid violating them."*

## 7. Memory categorization (what belongs where)

| Layer | Contents | Location |
|---|---|---|
| **Project invariants** | Money-path rules, OKX quirks, error codes, hard-won bug lessons | `CLAUDE.md` (auto-loaded) |
| **Deep references** | Subsystem docs, API tables, porting guides | `docs/` (pointed to from CLAUDE.md) |
| **Procedures** | Repeatable multi-step workflows | `.claude/skills/<name>/SKILL.md` |
| **Personal prefs** | Terse summaries, comment style, commit habits | `CLAUDE.md` "Working style" + `~/.claude/CLAUDE.md` for cross-project prefs |
| **Automation** | Test-on-stop, session bootstrap | `.claude/settings.json` hooks |
| **New-project bootstraps** | Build-brief template | `funding_arb_build_prompt.md` pattern |

**Maintenance rule:** every `Fix:` commit that reveals a durable lesson adds
one line to CLAUDE.md's invariants. Every workflow you explain to Claude twice
becomes a skill. Everything else stays out — CLAUDE.md must stay short enough
to be read in full, every session.
