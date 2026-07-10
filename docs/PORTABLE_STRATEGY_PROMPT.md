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

**Porting to a new broker / market / instrument — RE-DERIVE these, never copy ours:**
1. **The pair must be cointegrated** on the new market — verify (ADF/Engle-Granger
   on the spread, stable β) BEFORE trading. The whole edge assumes reversion exists.
2. **β** for the new pair, and a re-anchoring cadence (β drifts; a re-rating moves
   the spread by Δβ·P_A — enough to blow a no-stop position, so β is structural, §2).
3. **Contract/lot granularity and exchange minimums** per leg — these drive the
   lattice sizer (§4) and the min-fill check; wrong values silently break the hedge.
4. **Fees AND slippage from REAL fills** on the new venue (§3.3) — the fee page lies;
   an inflated cost estimate makes the edge filter and cost floor misbehave (§6.2).
5. **The venue's order semantics**: post-only/maker flag, reduce-only, close-whole-
   position endpoint, and — critically — the **WebSocket reconnect + resubscribe**
   behaviour (§8). Every venue's socket dies differently; the break-on-timeout rule
   is universal, the details are not.
6. **Tick cadence and volatility** decide the lookback and whether reversions are
   large enough vs cost (§12's hard truth). A quiet or trending instrument makes the
   same code −EV; confirm the spread actually swings enough to pay the toll first.
Everything else in this spec (the exit ladder, accounting, resilience, EV discipline)
is venue-agnostic — port it verbatim, including the WHY notes.

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
  And do NOT let a finite half-life "rescue" a Hurst gate: a finite HL on a strongly
  trending spread (measured live: H≈0.87, HL≈250 periods, every such entry stopped
  out in the trend) is the rolling mean running away from price, not reversion — the
  rescue clause silently defeated the whole filter. Verdict from live money: keep
  regime detection OUT of the automatic entry gate entirely; run it as a manual /
  monitoring signal (drift, anchor z-score) and let the operator decide. Automated
  regime gates cost more in missed-and-mislabeled trades than they save.

## 3. Entry rules — ALL must pass

1. **Trigger**: |z| ≥ `entry_z` (default **3.1–3.5**). Sign picks direction (z>0 →
   LONG spread if S is above μ in your sign convention — verify against §1).
2. **Entry ceiling**: refuse entries at |z| ≥ `max_entry_z` — ALWAYS active,
   independent of whether a z-stop may close trades (see §6). Entries live in
   the band `entry_z ≤ |z| < max_entry_z`. WHY: a z at 5+ is a momentum spike
   mid-flight, not a better entry — our extreme entries (z 5.49, 5.40, 5.06)
   went 0-for-3, one stopped out in 3 seconds. Keep the band ≥ 1σ wide
   (e.g. 3.5–4.5); a 0.5σ band refuses nearly everything in fast tape. Note:
   the ceiling judges z at signal time — a spike crossing the band between
   ticks is judged wherever it lands.
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
   **CRITICAL (cost the most live money of any single misconfig this program hit):**
   the %-of-capital target is ALREADY net of all fees, so the cost floor is
   REDUNDANT for it — and worse than redundant. At small capital the floor
   (1.5× an inflated cost estimate) pushed the target to $2.67 when a FULL
   reversion was only worth ~$2.08, so the take-profit could never fire; every
   winner leaked out through the trailing stop / gate at a fraction of its value
   (18–45% capture), and one trade that later reverted clean to its TP had
   already been scraped at $0.15 instead of $1.81. Fix: with a %-of-capital or
   σ-fraction target, set `cost_floor_mult = 0`. Keep the floor only for a raw
   fixed-$ target (which is NOT fee-aware). And fix the cost estimate at the
   source (see §3.3 — inflated slippage doubled the modeled cost, which doubled
   the floor). Print the RESOLVED target $ at entry and eyeball it against a full
   reversion — if target ≥ |z|·σ·qty, it will never fire.
3. **Reversion exit, GATED** (z returns inside `exit_z = 0.5`): allowed to close
   ONLY if `net ≥ gate_floor` (`0` = break-even; or `gate_capital_pct`, e.g.
   0.3–0.5%). Never book a losing "profit-take". Fail-open if P&L can't be
   priced. **The gate MUST defer to max-hold**: past 1× the trade's max-hold
   the floor decays to break-even; past 2× the gate releases entirely (the
   reversion edge is spent — take what's there). WHY: without the decay, gate
   (needs net ≥ floor) + max-hold (needs net > 0) + an out-of-reach TP jointly
   DEADLOCKED a fully reverted trade — held at +$1.19 for being 2 cents under
   the floor, then bled 80 minutes to a −$4.46 stop.
4. **MAX HOLD**: after `{4×}` measured half-life (or fixed minutes fallback), exit
   only if net > 0; suppress while z-progress ≥ `{50%}` toward home **only when a
   TP exists** (we shipped the suppression waiting for a TP that was configured off).
5. **z-stop: demote it to entry-ceiling duty (recommended)**. Post-entry, the
   rolling mean/σ drift, so a z-stop's dollar meaning wanders — ours fired at
   z −5.67 while gross was still INSIDE the dollar line, on a path (entry
   −3.84 → +1.2 → −5.67) that was oscillation, not trend. Config toggle
   `z_stop_exit_enabled` (keep OFF once a %-capital stop is armed): in-trade
   risk is then dollars only — *close in profit first, or lose the cap %*.
   FAIL-SAFES (non-negotiable): never suppress the dollar/daily override
   stops; auto-re-enable the z-stop whenever NO dollar stop is armed (a trade
   must always have a stop); the threshold keeps its entry-ceiling job either
   way. When you disable it, LOG every occasion it would have fired — those
   lines + trade outcomes score the design change with data.

**Trailing stop — usually OFF for mean reversion (learned live).** A trailing
stop exits on a pullback from an interim peak, but in mean reversion you WANT
to hold to the target. With a reachable TP it is redundant-or-harmful: live, a
trade peaked at +$1.13, the trailing stop cut it at +$0.15 on a 35% pullback,
and the spread then reverted straight through to its TP (+$1.81) minutes later.
A trailing stop is a trend-riding tool; it fights a reversion book. If the TP is
reachable (see §6.2), let the TP bank the win and the dollar stop cap the loss —
keep trailing OFF. Only consider it to protect an OVERSHOOT *past* the TP, and
even then the TP fires first on the way through.

**Exit execution is classified by URGENCY, not by the word "stop" (fee lesson).**
A loss cut (dollar/daily/z stop) is urgent → straight to market; you cannot rest
a maker order while the market runs. But a PROFIT-protecting exit (TP, trailing,
reversion) is NOT urgent — give it a maker probe first (one attempt, then market
fallback). Live: a trailing-stop exit was miscategorized as a "stop" and forced
to a taker fill; the taker fee ate ~$0.5 of a ~$0.65 gain (85% of gross). Route
every profit-side exit maker-first; reserve straight-to-market for genuine loss
cuts. (Group the reasons explicitly, e.g. `MAKER_PROBE_EXITS = {TAKE_PROFIT,
TRAILING, REVERSION, DOLLAR_STOP}` vs `MARKET_NOW = {STOP_LOSS, DAILY_LOSS}`.)

**Exit-path completeness rule (learned the expensive way):** enumerate every
exit's preconditions and prove at least one exit is reachable in EVERY
(P&L, z, time) state. Watch the corner with z-stop exits disabled: a sideways
loser (net < 0, z never reverting) has no clock — max-hold skips losers and
the gate needs a reversion signal. Either accept that it waits for TP/SL, or
add a hard time-stop (close ANY trade at ~3× max-hold regardless of P&L).
The **break-even hoverer** is the same failure wearing a smile: because MAX_HOLD
only fires when net > 0, a trade that sits at ~break-even is held indefinitely
until net ticks barely positive — live, one hovered **78 minutes** then exited
−$0.16 as fees ate a $0.25 gross. That is capital held hostage for nothing. The
hard time-stop must close on TIME regardless of P&L SIGN, not just rescue
runaway losers — a trade going nowhere is a loss of the thing you're actually
short: deployable capital and the next setup.

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
  exchange before acting. Recovery is what makes an auto-restart safe — the
  position stays on the exchange and is re-adopted with its stop/target intact.

**Connection resilience — the single biggest source of live "hangs" (port these
verbatim):**
- **Every WebSocket receive loop MUST `break`→reconnect on a receive-timeout, never
  `continue`.** A socket can go silently half-dead (TCP up, zero data — routine
  behind a load balancer); re-arming `receive()` on the same dead socket just spins
  forever. Live, the price feed did exactly this: it logged "receive timeout" every
  35 s but never reconnected, ticks stopped, and the process got restarted every
  ~30 min — while the execution socket beside it (which broke→reconnected correctly)
  recovered in one attempt. On break: reconnect AND **re-subscribe** (a reconnected
  socket with no subscriptions delivers nothing) and **discard cached ticks** from
  before the gap so the engine never acts on stale prices.
- **Liveness heartbeat must prove the LOOP is alive, not that DATA is flowing.**
  If you write the heartbeat only from the tick callback, a data outage is
  indistinguishable from a frozen loop, and the watchdog restarts the whole process
  for what a reconnect would fix. Write it from a small timer coroutine (proves the
  async loop is spinning) and handle "no ticks for N s" separately by forcing a WS
  reconnect — a surgical fix, not a process kill.
- **External watchdog** (separate process): restart the app if the process dies OR
  the heartbeat file goes stale (a hang a crash-only supervisor can't catch). Keep
  relaunch fast (seconds) — a slow relaunch is unmanaged-position time. If it also
  auto-restarts (a supervisor loop), the app must EXIT (not self-exec) on a remote
  restart, or you get two live engines placing double orders — gate on an env flag.
- **Remote restart command** (e.g. Telegram `/restart`) for a soft hang when you
  have no shell: it re-launches the process; the open position is re-adopted on
  startup. It only works while the poll thread still runs — a true freeze needs the
  heartbeat watchdog. Both together = self-healing from any stuck state.
- Give EVERY exchange call (WS and REST) a tight timeout and a REST circuit breaker
  so a slow venue fails fast instead of stacking 10 s waits that starve the loop.

## 9. Configuration principles

Prefer **scale-invariant knobs** — they survive resizing and re-vol: σ-fraction
target, %-of-capital target/gate/stop, ×half-life hold, edge multiple. Fixed-$
fields exist only as fallbacks and are ignored when the scale-invariant twin is set.
Log every auto-tuned change (old → new, why) to an audit table; auto-tuning without
corridors + consensus + post-change validation is self-overfitting — a tuner that
loosened entries after a good day walked straight into the next trend.

**Tune the take/hold from measurements, not opinion.** Persist per-trade
lifecycle extremes: peak and trough net P&L WITH minutes-after-entry. Then:
- Take-profit / gate %: read the peak distribution (as % of capital) and set the
  take near the ~60–70th percentile of peaks — "70% of trades peaked above X%
  within Y minutes" is the sentence that sets the number. (Our first data
  point: a loser peaked at 0.49% of capital at minute 6 against a 0.5% floor —
  missed by 2 cents; the distribution decides whether 0.3% or 0.5% is right.)
- Max-hold: median `peak_minutes` of winners — when the best exit typically
  arrives — not a guess.
- Always compute the breakeven win rate `stop_net / (target + stop_net)` for
  the chosen geometry and verify the measured win rate clears it (smaller
  takes hit more often but demand a higher win rate — e.g. 0.3% take vs 1%
  stop needs ~80%; 0.5% needs ~71%). The distribution decides, not intuition.

**Run the whole book on one expectancy sheet, in R (= one stop's worth of
money).** Compute and surface live: win rate `p`, realized reward:risk
`RR = avg_win/avg_loss`, profit factor `Σwin/Σloss`, break-even WR `1/(1+RR)`,
and expectancy `EV/R = p·(1+RR) − 1`. Two hard truths that saved a lot of
flailing: (a) **chase R:R, not win rate** — a system with RR 1.5 is *allowed*
to lose 60% of the time; obsessing over "fewer losers" optimizes the wrong
knob. (b) **On a fixed reversion distribution, RR and WR trade off** — a smaller
target lifts WR but craters RR, a larger one the reverse; you CANNOT dial both
up by tuning target/stop. The only way to move both is a better trade
*population* (more selective entries, a livelier regime). If the sheet says
you're below break-even WR, you are −EV no matter how any single trade "felt."

**Measure whether you're exiting too early — the "what-if-held" shadow.** After
every exit that is NOT a clean target hit (stops, trailing, sub-target
reversions, small early wins), keep marking the position's REAL held P&L (per
§5 accounting, on the frozen fills — immune to z/β drift) for a window
(~60 min) and record whether/when it would have reverted to break-even and to
the target, plus the peak it reached. High revert-rate ⇒ your exits are
premature (cutting winners, stops too tight); low ⇒ the exits were right and
the move was real. This turned "it would have reverted, just wait" from an
argument into logged data. **Persist each watch the moment it's armed and
resume it on restart** — an in-memory-only watch is silently lost every restart,
so during a tuning session (frequent restarts) it never finalizes and looks
broken.

## 10. Observability (all of it earned its place)

- Position card: live net P&L, **BE/EX/TP/SL levels** AND the net dollar value
  each level equals, Δspread, per-leg price change % since entry (colored by
  whether the move helps that leg's side), age vs max-hold.
- Post-trade analysis must be **crisp and rule-based, numbers first** — LLM
  prose walls go unread. Every close reports: a deterministic outcome tag
  (TARGET HIT / REVERSION BANKED / TIME EXIT / STOPPED IN TREND — never
  reverted / STOPPED AFTER FULL REVERSION — z came home but price never paid),
  **which stop fired** ("z-stop — dollar stop −$X NOT reached (gross $Y)" vs
  "DOLLAR stop — capital cap"), timed extremes ("Peak/Trough +$1.19 (6m) /
  −$4.46 (88m)"), capture vs available move, hold vs max-hold, gate holds
  (count × duration × floor), z path with range.
- Remote tracking: entry notification carries the full exit geometry; an
  on-demand command (e.g. Telegram /positions) renders the live snapshot —
  pull-based real-time tracking beats periodic push spam.
- Per-trade review: entry/exit z, available Δ (|z|·σ), realized Δ, **capture
  ratio**, cost ratio, fee split (est vs real), exit reason, streaks, PF, EV/trade.
- Deterministic config-coherence audit ("is a target set? is a stop armed?") must
  check ALL the forms that configure a thing, not a subset. Live, the audit
  screamed "no profit target set" for 30+ reviews because it checked only the
  σ-fraction and fixed-$ fields and ignored the %-of-capital field that WAS set —
  sending every review (human and LLM) chasing a non-issue while the real problem
  (the cost floor, §6.2) sat one line away. A false "missing" alarm is worse than
  none: it launders attention away from the actual bug.
- Journal each trade's persisted exit geometry (BE/EX/TP/SL as spread levels) and
  the realized Δspread + signed captured-Δ, so history reads in the one variable
  that pays — not four raw leg prices you have to do arithmetic on.
- Surface the what-if-held shadow (§9): "N tracking" the moment a trade closes,
  then "reverted X/Y within the window" once watches complete.
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
13. Exit-path completeness: a fully reverted, sub-floor trade must still have an
    exit — gate floor decays to break-even past 1× max-hold, releases at 2×
    (regression for the +$1.19-held-over-2¢ → −$4.46 deadlock).
14. z-stop suppression matrix: off+dollar-armed suppresses; default on; override
    stops (DOLLAR/DAILY) never suppressed; no-dollar-stop fail-safe keeps the
    z-stop; plain reversion EXITs untouched.
15. Lifecycle extremes persist: peak/trough net + minutes-after-entry round-trip
    through the DB and render in the close report ("+$1.19 (6m) / −$4.46 (88m)").
16. The full close/accounting path runs end-to-end in paper mode with lifecycle
    stats attached — any dangling reference in it fails the suite.
17. **WS receive loop breaks on the FIRST receive-timeout and schedules a
    reconnect** (assert exactly one receive call, not a spin) — regression for
    the silently-dead-socket hang that restarted the process every ~30 min.
18. Exit-fee routing: profit-side exits (TAKE_PROFIT/TRAILING/REVERSION) take a
    maker probe before market; genuine loss cuts (STOP_LOSS/DAILY_LOSS) go
    straight to market — regression for the taker fee that ate 85% of a gain.
19. Cost floor is skipped for a %-of-capital / σ-fraction target (already fee-net)
    and applied only to a raw fixed-$ target; resolved target < |z|·σ·qty.
20. What-if-held shadow: arms on every non-target exit (incl. small wins),
    skips a clean target hit; the pending watch persists on arm, resumes on
    restart, and drops (not lingers) if its window elapsed during downtime.

## 12. Non-goals / hard warnings

- **No per-leg exchange-native stop orders** at leverage — one leg stopping alone
  converts a hedge into a naked position on normal volatility.
- No calendar on/off switches — the edge filter (σ vs cost) handles dead days by
  arithmetic, whichever day they fall on.
- Don't gate on Hurst at tick timescales; use half-life + drift/regime instead.
- Scaling size does NOT change survival odds — every %-of-capital level scales with
  qty, so the z-distance to the stop is size-invariant. Fix expectancy before size.
- The z-exit is not a profit engine; the TP and the gate are. **z earns entries;
  dollars govern everything after.** Post-entry, the rolling z is a drifting
  statistic — never let it be the thing that pays or stops you.
- Changing risk settings while a trade is OPEN applies to that trade immediately
  (our tightened stop fired 56s after the save). Fine if intended — know it.
- When you disable any protective mechanism, keep logging every occasion it
  WOULD have fired, alongside the trade's outcome — every design change becomes
  a scoreable natural experiment instead of an argument.
- Every dollar level needs a cost-floor sanity check at the operating size: a
  cost floor multiple of 1.5× silently pinned our "0.5% of capital" target ~50%
  higher at small capital. Print the RESOLVED levels at entry, not the configs.
- **A trailing stop is a trend tool; do not run it on a reversion book with a
  reachable TP** — it cuts winners mid-reversion before the TP fires (§6).
- **The hardest truth, and no config fixes it: if most reversions are smaller
  than the round-trip cost, the strategy cannot make money on that pair/period.**
  Measure it — count the fraction of recent trades whose gross move was below
  cost (ours ran 16/20). When it's high, you are not mis-tuned, you are on a dead
  or trending market: the spread must SWING with enough amplitude AND actually
  REVERT to pay the toll. The edge filter (amplitude vs cost) and the regime read
  (does it revert or trend) are the two gates that enforce "only trade swings big
  and clean enough to pay." When they leave the bot flat for hours, that is the
  system working, not broken — trading a toll-sized wiggle just donates the toll.
  Fix selection or change instrument; do not tune the stop/target and hope.
- Watch the exchange's real fill types: a maker-intended book that fills taker
  (post-only rejects → market fallback) pays multiples more; if fees are a large
  fraction of a small gross move (ours hit 82–103% of gross on the worst trades),
  the fee model and the exit routing (§6) matter as much as the signal.
