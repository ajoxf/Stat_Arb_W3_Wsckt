# Portable Strategy Prompt — Market-Neutral Pair Mean-Reversion Bot

Copy everything below the line into a new implementation project (AI assistant or
developer). Fill the `{PLACEHOLDERS}`. This spec encodes not just the rules but the
live-money lessons behind them — the "why" notes are load-bearing; do not drop them.

---

You are implementing an automated **market-neutral pair mean-reversion trading
system**. Build it exactly to this specification. Where the spec says WHY, treat it
as a constraint discovered with real money — do not "simplify" it away.

## 0. Parameters (fill these in)

- PLATFORM / LANGUAGE: `{e.g. Python asyncio service, MT5 EA, Node}`
- BROKER / EXCHANGE + API: `{e.g. OKX, Binance, IBKR, MT5}`
- LEG A (the "anchor" instrument): `{e.g. ETH-USDT perpetual}`
- LEG B (the hedge instrument): `{e.g. BTC-USDT perpetual}`
- CONTRACT/LOT GRANULARITY per leg: `{e.g. 0.1 ETH/contract, 0.01 BTC/contract}`
- LEVERAGE per leg: `{e.g. 20x}` · CAPITAL BUDGET per trade: `{e.g. $1,800 on Leg A}`
- FEES per leg (maker/taker, bps) — **measured from real fills, not the fee page**
- TICK CADENCE: `{e.g. 0.5–1s}` · ROLLING LOOKBACK: `{e.g. 7,200 ticks}`

## 1. The strategy in one paragraph

Trade the **spread** `S = P_B − β·P_A` between two cointegrated instruments, where β
(hedge ratio) makes the pair dollar-neutral. When the spread is statistically
stretched, enter both legs in opposite directions; profit when the gap reverts.
**P&L = Δspread × size. Everything else — z-scores, filters, half-life — is a
prediction about that one number.** Δspread fails three ways, and the system needs a
defense for each: the move is too small vs costs (dead day → edge filter), the move
goes one way and stays (trend → regime guards), or your executed legs don't match β
(bad hedge → lattice sizing + true-fill accounting).

## 2. Signals

- Maintain rolling mean μ and std σ of S over the lookback; recompute stats every
  `{stats_interval, e.g. 300s}`; z = (S − μ)/σ.
- β is **structural**: changing it recomputes the whole spread series. Block β
  changes while a position is open or the algo is on.
- LONG spread = long Leg A / short Leg B (profits when S falls). SHORT is the mirror.
- Half-life of mean reversion via OU regression (ΔS = θ(μ−S)+ε; HL = ln2/θ) — used
  for max-hold and as a mean-reversion sanity check. Skip Hurst as a gate: on slow
  reverting spreads it reads ~0.9 at tick timescales and blocks everything (measured).

## 3. Entry rules — ALL must pass

1. **Trigger**: |z| ≥ `entry_z` (default **3.1**). Sign picks direction (z>0 → LONG
   spread if S is above μ in your sign convention — verify against §1's definition).
2. **Entry ceiling**: refuse entries at |z| ≥ `stop_z` (default **4.0–4.5**). WHY: we
   entered at z=5.49 with the stop at 5.5 and were stopped out in 3 seconds.
3. **Edge filter (the dead-day gate)**: expected capture must clear costs:
   `capture = target_fraction × |z| × σ × qty` (same number the exit targets) and
   require `capture ≥ min_edge_multiple × round_trip_cost` (default **1.5×**).
   `round_trip_cost` = all four legs' fees + slippage — **calibrated to real fills**.
   WHY: with maker-both-legs execution, real slippage ≈ 0 and real fees were 4.7×
   smaller than the config estimate; the inflated model made every trade look
   "structurally unprofitable" and the floor unreachable. Audit monthly: if modeled
   cost ≥ 2× realized cost, raise an alarm.
4. **Regime guard (the trending-day gate)**: at minimum a trend-direction filter —
   slope of S over the recent window: rising S → SHORT-only, falling → LONG-only.
   Better: a daily drift monitor — freeze a **morning anchor** after a warm-up,
   then measure efficiency ratio (|net move| ÷ path length), zero-crossings of the
   anchor, and variance ratio on a trailing window; if persistently TRENDING, halt
   new entries for the day and auto-rearm next morning. WHY: one labeled-TRENDING
   overnight session produced 5 straight losses; every entry card displayed the
   label while nothing enforced it.
5. **Cooldowns/gates**: entry cooldown `{60s}` after any close; stop cooldown
   `{300s}` after a stop; **z-reset gate** — after a stop, block same-direction
   re-entry until z returns inside the exit band (stops re-entering a trend).
6. **Atomic pre-checks**: BOTH legs' exchange minimums and required balance verified
   before placing EITHER order. WHY: a leg that fails minimums after the other
   filled creates an instant naked position.
7. **Circuit breakers**: daily-loss halt (`{daily_max_loss}`); loss-streak reducer
   (−20% size at 3 consecutive losses, pause algo at 6).

## 4. Position sizing — lattice co-sizing (critical at small size)

Ideal sizes: `qty_A = budget / P_A`, `qty_B = qty_A / β`. Real venues round to whole
contracts/lots **per leg**, which silently destroys the hedge: ideal 10.08/2.71
contracts floored independently = 10/2 = executed ratio 50 when β = 37 → a 34% naked
directional overhang the model can't see (this alternately gifted and mugged us).

**Rule**: search the whole-contract combinations within ±1 contract of the ideals
(each leg ≥ 1), reject any combo whose total notional exceeds ideal × (1 + `{12%}`),
and pick the one minimizing `|executed_ratio − β| / β`. (Example: 11/3 = ratio 36.7,
1.5% error, instead of 10/2 = 34% error.) Pass the CHOSEN sizes to the executor —
the executor must never re-derive one leg from the other via β (we did; it floored a
contract away and the fill-ratio guard rejected its own filled entry). Beware float
dust in unit conversion: `0.03/0.01 = 2.999…96` — epsilon-floor (`int(x + 1e-9)`).

## 5. True-position accounting (non-negotiable)

- After entry, **record the actually-filled base quantities per leg** as the
  position of record; recompute notional/margin from fills. Requested ≠ filled.
- **P&L is computed per leg from actual fills**:
  `LONG: qty_A(exit_A−entry_A) + qty_B(entry_B−exit_B)`; SHORT mirrored. This equals
  the spread formula only when executed ratio = β — and matches the broker to the
  cent always. All stops, targets, and capital math run on this number.
- Use **actual fees** from fills when the API provides them; estimate otherwise.
- Keep fills in the venue's native unit distinct from base units everywhere
  (contracts vs coins). WHY: our min-fill check compared contracts to base ("992%
  filled") and could only catch zero-fill entries.
- Money that moves outside a recorded trade (orphan cleanups) goes to an
  **untracked-close ledger**, is charged to the daily-loss tracker, and is surfaced
  in the UI. Silent cleanup costs otherwise exist only in the broker statement.

## 6. Exit ladder — levels in DOLLARS off entry fills, frozen at entry

Compute once at entry and display: **BE · EX · TP · SL** as absolute spread/price
levels. `net(S) = d×(S−S_entry)×qty − fees` (d = −1 LONG / +1 SHORT).

WHY dollars and not z: the rolling mean chases the spread during the hold, so
in-trade z "reverts" without the price paying you (a documented trade turned
+$0.22 theoretical into −$1.08 real). z is for ENTRIES; exits act on money.

Priority order each tick (risk first):

1. **DOLLAR STOP** (ungated, fires on GROSS move so "stop" means spread distance,
   not fees): `gross ≤ −stop_usd` where
   `stop_usd = min(target_usd ÷ RR, stop_capital_pct × capital_at_risk)` — the
   TIGHTER wins. Defaults: `stop_capital_pct = 1.5%`, `RR = 0.3` (RR-derived stop is
   then wide; the %-cap is the binding catastrophe ceiling).
   `capital_at_risk = Σ leg_notional/leverage × (1 + m2m_buffer%)`.
   Execution: stops go **straight to market** (one optional maker probe for the
   frequent DOLLAR_STOP, ~2s, then market). Post-only exits get rejected precisely
   when the market runs — never rest a stop. Never route stops via RFQ.
2. **TAKE PROFIT** (ungated, **P&L alone — no z condition**). Precedence of forms:
   σ-fraction (`0.5 × |z_entry| × σ × qty`) > **%-of-capital** ("bank the win":
   `net ≥ tp_capital_pct × capital_at_risk`, e.g. 0.5% ⇒ BE + 0.5% cap) > fixed $.
   **Cost floor**: `target = max(target, cost_floor_mult × round_trip_cost)`,
   `cost_floor_mult ≈ 1.0–1.5`. Check floor ≤ plausible full reversion
   (`|z|×σ×qty`) — if the floor exceeds it, the trade can never win; block entry.
3. **Reversion exit, GATED** (z returns inside `exit_z = 0.5`): allowed to close
   ONLY if `net ≥ gate_floor` (`0` = break-even; or `gate_capital_pct`, e.g. 0.5%).
   Never book a losing "profit-take". Fail-open if P&L can't be priced.
4. **MAX HOLD**: after `{4×}` measured half-life (or fixed minutes fallback), exit
   only if net > 0; suppress while z-progress ≥ `{50%}` toward home **only when a
   TP exists** (we shipped the suppression waiting for a TP that was configured off).
5. **z-stop backstop** at `stop_z` (rarely first — the dollar stop usually wins).

## 7. Execution engine

- **Maker-first**: post-only limits on both legs simultaneously; poll fills; on
  timeout re-poll once before declaring partial (late fills upgrade PARTIAL→FILLED).
- **Post-only rejection** (price crossed book): cancel BOTH legs, then **always
  re-read the failed leg's fill state** — a "cancelled" order can carry a partial
  fill; if any filled quantity leaked, **flatten it immediately** via the venue's
  whole-position close endpoint and fail the entry flat. WHY: a leaked partial is a
  naked leveraged position.
- **One-leg-failed protocol** (leg risk is THE pair-trading risk): the moment one
  leg fails, settle the other (cancel → re-read → flatten leaks). Reject the entry
  unless fills ≥ `min_fill_ratio {95%}` **measured in base units**.
- **Exits are reduce-only** and reconciled against the live exchange position
  before placing (scale down to what actually exists; skip legs already flat).
- Retry cadence: entries ~10s after a post-only reject; exits ~10s (2s while a
  DOLLAR_STOP is mid-close). Every failure path applies a cooldown — no hot loops.

## 8. Reconciliation & self-healing (this is where accounts die)

- **Position-sync check** every `{20s}`: engine state vs exchange positions.
  - Engine FLAT + exchange has bot-instrument positions → after 3 consecutive
    mismatches, **auto-close orphans** via the venue's close-whole-position
    endpoint (instrument + side only — never hand-compute close sizes; a
    units confusion once made an orphan permanently un-closeable). Book the cost
    to the untracked ledger. Never touch non-bot instruments.
  - Engine IN-TRADE + exchange flat → after 3 checks, force-clear engine state.
- **Crash-safe closes**: mark the trade closed and persist ONLY after exit orders
  succeed; if the close crashes mid-accounting the engine must not order-spam
  (reduce-only + reconcile make retries harmless — we survived exactly this).
- On restart: recover any open trade from the DB, then reconcile against the
  exchange before acting. A watchdog process relaunches the app on hang.

## 9. Configuration principles

Prefer **scale-invariant knobs** — they survive resizing and re-vol: σ-fraction
target, %-of-capital target/gate/stop, ×half-life hold, edge multiple. Fixed-$
fields exist only as fallbacks and are ignored when the scale-invariant twin is set.
Log every auto-tuned change (old → new, why) to an audit table; auto-tuning without
corridors + consensus + post-change validation is self-overfitting — a tuner that
loosened entries after a good day walked straight into the next trend.

## 10. Observability (all of it earned its place)

- Position card: live net P&L, **BE/EX/TP/SL levels**, Δspread, age vs max-hold.
- Per-trade review: entry/exit z, available Δ (|z|·σ), realized Δ, **capture
  ratio**, cost ratio, fee split (est vs real), exit reason, streaks, PF, EV/trade.
- Trade-vs-broker reconciliation to the cent (per-leg accounting makes this exact).
- Untracked-close badge; daily loss vs limit; regime/drift state; hedge-ratio
  z-score vs a frozen morning anchor (sustained |z|>2 that doesn't return =
  structural trend warning, monitoring only).
- Logs must narrate decisions: entry geometry (BE/EX/TP/SL at open), lattice sizing
  chosen vs naive floor, gate holds, stop reasons, reconcile actions.

## 11. Acceptance tests (port these; they all caught real bugs)

1. Per-leg P&L reproduces broker-reported P&L on recorded fills to the cent.
2. Lattice: ideal 10.08/2.71 @ β 37.2 picks 11/3 within +12% notional; huge-size
   floors stay; sub-minimum legs refuse (never silently upsize beyond tolerance).
3. Epsilon-floor: 0.03/0.01 → 3 units, 2.69 → 2.
4. Executor targets THE lattice sizes (a β-rederivation must fail the suite).
5. Fill-ratio check is unit-correct (contracts vs base).
6. Gate: holds sub-floor reversion exits, releases at floor, never gates stops or
   TP/MAX_HOLD; disabled and fail-open paths.
7. Exit levels solve net(BE)=0, net(TP)=target, gross(SL)=−stop exactly; EX = BE
   when gate floor is 0.
8. Target precedence σ-frac > %-cap > $; cost floor raises tiny targets.
9. Stop = min(RR-stop, %-cap) — verify each side can bind.
10. **Full close path runs end-to-end in paper mode** (would have caught our
    NameError crash loop that retried a close forever).
11. Orphan auto-close: emits ledger entry + charges daily loss; failed close books
    nothing; never touches non-bot instruments.
12. Leaked-partial flatten: cancel-then-reread flattens hidden fills; clean cancels
    place nothing; unconfirmed cancels stay OPEN for retry.

## 12. Non-goals / hard warnings

- **No per-leg exchange-native stop orders** at leverage — one leg stopping alone
  converts a hedge into a naked position on normal volatility.
- No calendar on/off switches — the edge filter (σ vs cost) handles dead days by
  arithmetic, whichever day they fall on.
- Don't gate on Hurst at tick timescales; use half-life + drift/regime instead.
- Scaling size does NOT change survival odds — every %-of-capital level scales with
  qty, so the z-distance to the stop is size-invariant. Fix expectancy before size.
- The z-exit is not a profit engine; the TP and the gate are. z earns entries.
