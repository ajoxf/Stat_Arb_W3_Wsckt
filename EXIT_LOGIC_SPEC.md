# Exit Logic Specification

**Full detail on all 7 exit conditions — logic, formulas, parameters, UI, and dashboard display. Sufficient to implement in any pairs-trading system regardless of broker or instrument.**

---

## Overview

Every ~0.5 s (one tick) the engine evaluates all exit conditions in strict priority order. **The first condition that fires wins — no later condition is evaluated.** Conditions 1–6 are "override exits" driven by dollar P&L, time, and regime signals. Condition 7 is the normal z-score-based exit from the signal generator.

```
EACH TICK (while in position)
│
├─ 1. DOLLAR_STOP      ← always first (risk before reward)
├─ 2. PROFIT_TARGET
├─ 3. MAX_HOLD         ← silent when losing
├─ 4. TRAILING_STOP
├─ 5. HURST_REGIME     ← optional
├─ 6. SPREAD_VELOCITY  ← optional
└─ 7. Z-SCORE EXIT     ← normal path
```

**Live P&L definition used by all overrides:**

```
entry_spread = entry_futures_price − β × entry_spot_price
cur_spread   = current_futures_mid − β × current_spot_mid

LONG:  spread_change = entry_spread − cur_spread
SHORT: spread_change = cur_spread   − entry_spread

pnl_gross = spread_change × futures_quantity

round_trip_fees = (entry_fee_bps/10000 × spot_qty × entry_spot)
                + (entry_fee_bps/10000 × fut_qty  × entry_futures)
                + (exit_fee_bps/10000  × spot_qty × current_spot)
                + (exit_fee_bps/10000  × fut_qty  × current_futures)

net_pnl = pnl_gross − round_trip_fees
```

Fee bps: LIMIT order → maker rate; MARKET order → taker rate. If a POST_ONLY rejection has already occurred and the next exit is known to be MARKET, the fee estimate switches to taker rate immediately so the live P&L shown is honest.

---

## Exit 1 — Dollar Stop

**Purpose:** Hard floor on loss per trade. Evaluated unconditionally every tick — no gate, no suppression. Risk control always comes before reward.

### Logic

```
stop_usd = resolved stop amount (see below)

if net_pnl <= −abs(stop_usd):
    fire STOP_LOSS
```

### Stop Amount Resolution (priority order)

```
1. Scale-invariant form (preferred):
   if stop_loss_capital_pct > 0:
     capital_at_risk = (margin_leg_a + margin_leg_b) × (1 + m2m_buffer_pct / 100)
     stop_usd = (stop_loss_capital_pct / 100) × capital_at_risk

2. Fixed-dollar fallback:
   else:
     stop_usd = max_loss_usd

3. Disabled:
   if both == 0 → dollar stop is off
```

**Capital at risk breakdown:**

```
margin_leg_a = (entry_spot × spot_qty) / spot_leverage
margin_leg_b = (entry_futures × fut_qty) / futures_leverage
capital_at_risk = (margin_leg_a + margin_leg_b) × (1 + m2m_buffer_pct / 100)
```

The M2M buffer ensures the stop fires before the exchange-level margin call — it is the same buffer used for the pre-entry balance check.

### Execution Behaviour

- On first trigger: attempts **LIMIT (POST_ONLY)** exit at current best price
- If POST_ONLY rejected once (cancelSource=31): forces **MARKET** on next attempt to guarantee close
- After close: 300 s stop-loss cooldown, then `entry_cooldown_seconds` before re-entry allowed

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `stop_loss_capital_pct` | Float | `0.0` | % of locked capital per trade (e.g. `1.0` = 1%). Scale-invariant. **Recommended 0.5–1.0.** |
| `max_loss_usd` | Float | `0.0` | Fixed-dollar stop. Used only when `stop_loss_capital_pct = 0`. |
| `m2m_buffer_pct` | Float | `10.0` | Adds headroom to capital_at_risk denominator |
| `spot_leverage` | Int | `1` | Leg A leverage (divisor for margin_leg_a) |
| `futures_leverage` | Int | `20` | Leg B leverage (divisor for margin_leg_b) |

### Settings UI

Section: **⚡ Fast-Exit Overrides**

```
┌─────────────────────────────────────────────────────────────┐
│  Dollar Stop (% capital)         [  1.0  ]  range 0–20      │
│  ← Scale-invariant: stop = X% of margin locked for trade    │
│                                                             │
│  ── Fixed-$ Fallback ──                                     │
│  Dollar Stop (USD)               [  0    ]  step 0.50        │
│  ← Exit when net P&L ≤ −this amount                         │
└─────────────────────────────────────────────────────────────┘
```

- If `stop_loss_capital_pct > 0`, the fixed-USD field is ignored
- Both set to 0 = dollar stop disabled (pure z-score risk management only)

### Dashboard Display

In the **Signal & Position** card position detail panel:

```
│ Stop Loss    │  −$5.22         │
```

In the **P&L Progress Gauge:**
- Left anchor of the gradient bar is `−$stop_usd` (red end)
- If `net_pnl ≤ −stop_usd` the dot is at the far-left red zone

---

## Exit 2 — Profit Target

**Purpose:** Lock in profit once the spread has reverted sufficiently. Checked only after the dollar stop passes.

### Logic

```
target_usd = resolved target amount (see below)

if net_pnl >= target_usd:
    fire EXIT
```

### Target Amount Resolution (priority order)

```
1. Scale-invariant form (preferred):
   if profit_target_sigma_frac > 0:
     target_usd = σ_frac × |entry_zscore| × entry_spread_std × futures_quantity

2. Fixed-dollar fallback:
   else:
     target_usd = profit_target_usd

3. Cost floor (applied on top of whichever form):
   if profit_target_min_cost_mult > 0:
     round_trip_cost = fees_usd + slippage_usd   (all 4 legs)
     target_usd = max(target_usd, cost_mult × round_trip_cost)

4. Disabled:
   if both == 0 → profit target is off
```

**Scale-invariant formula expanded:**

```
target_usd = σ_frac × |Z_entry| × σ_spread × Q_futures

Where:
  σ_frac        = profit_target_sigma_frac (e.g. 0.65)
  |Z_entry|     = absolute z-score at trade open (e.g. 3.2)
  σ_spread      = rolling spread std frozen at entry (e.g. $85.23)
  Q_futures     = futures quantity in BTC (e.g. 0.0337)

Example: 0.65 × 3.2 × 85.23 × 0.0337 = $5.96
```

This scales the target proportionally to how extreme the entry was and how volatile the pair is — large z-score at a volatile time → larger target automatically.

**Cost floor example:**

```
round_trip_cost = $1.40  (fees on 4 legs)
cost_mult = 1.0
floor = $1.40

If σ-frac formula gives $0.80 → target raised to $1.40
If σ-frac formula gives $5.96 → target stays $5.96
```

The floor ensures that a statistically valid but small-dollar target never fires at a net loss after execution costs.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `profit_target_sigma_frac` | Float | `0.0` | Fraction of expected reversion to capture (e.g. `0.65` = 65%). Scale-invariant. **Recommended 0.60–0.70.** |
| `profit_target_usd` | Float | `0.0` | Fixed-dollar target. Used only when `sigma_frac = 0`. |
| `profit_target_min_cost_mult` | Float | `0.0` | Target must be ≥ this × round-trip cost. Set `1.0` to guarantee positive net after fees. |

### Settings UI

Section: **⚡ Fast-Exit Overrides**

```
┌─────────────────────────────────────────────────────────────┐
│  Profit Target (σ-fraction)      [ 0.65  ]  range 0–2       │
│  ← target = σ_frac × |Z_entry| × σ × qty                    │
│  ← 0.6–0.7 recommended (capture ~65% of the mean reversion) │
│                                                             │
│  Profit Cost Floor (× fees)      [ 1.0   ]  range 0–10      │
│  ← target must clear total fees × this multiplier           │
│                                                             │
│  ── Fixed-$ Fallback ──                                     │
│  Profit Target (USD)             [  0    ]  step 0.50        │
│  ← Exit once net P&L ≥ this amount                          │
└─────────────────────────────────────────────────────────────┘
```

### Dashboard Display

In the **Signal & Position** card:

```
│ Profit Target │  +$4.95         │
```

In the **P&L Progress Gauge:**
- Right anchor of the gradient bar is `+$target_usd` (green end)
- Dot moves toward the right as P&L improves

In the **Filters** card info box:
- Shows live `round_trip_cost` in bps and USD
- Shows `std_ratio` = expected move / cost (must exceed `min_std_multiple`)

---

## Exit 3 — Max Hold

**Purpose:** Prevent capital from being tied up indefinitely in a slow-to-revert trade. Only fires when the trade is profitable — inert when losing (the dollar stop handles losses).

### Logic

```
held = time or periods since entry

if held >= max_hold AND net_pnl > 0:
    check Z-progress gate
    if gate suppressed → do NOT fire
    else → fire EXIT
```

### Hold Measurement (two forms, only one active)

```
Form A — Half-life multiple (preferred, scale-invariant):
  if max_hold_halflife_mult > 0:
    max_hold_periods = max_hold_halflife_mult × current_half_life
    periods_held = total_engine_ticks − entry_tick_count
    condition: periods_held >= max_hold_periods

Form B — Fixed minutes (fallback):
  if max_hold_minutes > 0:
    held_minutes = (now − entry_time).total_seconds() / 60
    condition: held_minutes >= max_hold_minutes
```

Half-life is the Ornstein-Uhlenbeck mean-reversion half-life of the spread (see main spec). A multiplier of 2× means: "if the spread hasn't reverted in twice its typical reversion time, something is wrong."

### Z-Progress Gate (suppression)

When the trade is actively reverting toward the exit threshold, max hold is suppressed to avoid cutting a profitable trade short. The gate measures what fraction of the journey from entry to exit has been completed.

```
entry_abs  = |entry_zscore|         (e.g. 3.20)
cur_abs    = |current_zscore|       (e.g. 1.80)
exit_abs   = |exit_threshold|       (e.g. 0.50)
journey    = entry_abs − exit_abs   (e.g. 2.70)

z_progress = (entry_abs − cur_abs) / journey
           = (3.20 − 1.80) / 2.70
           = 0.52  (52% of the way home)

if z_progress >= max_hold_z_progress_min (e.g. 0.50):
    MAX_HOLD SUPPRESSED — let it run to target
```

**Silent when losing:** The `net_pnl > 0` gate means max hold is completely inert during losing trades. This is intentional — the dollar stop handles loss containment; max hold only exits when you're already winning.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_hold_halflife_mult` | Float | `0.0` | Max hold = this × measured half-life in ticks. Scale-invariant. **Recommended 1.5–2.0.** |
| `max_hold_minutes` | Float | `0.0` | Fixed-minutes fallback. Used only when `halflife_mult = 0`. |
| `max_hold_z_progress_min` | Float | `0.5` | Suppress max hold when Z has reverted this fraction toward exit threshold. `0` = never suppress. `1.0` = always suppress (disables gate). |

### Settings UI

Section: **⚡ Fast-Exit Overrides**

```
┌─────────────────────────────────────────────────────────────┐
│  Max Hold (× half-life)          [ 5.0   ]  range 0–20      │
│  ← hold limit = mult × measured OU half-life                │
│  ← 1.5–2.0 recommended                                     │
│                                                             │
│  Max Hold Z-Progress Gate        [ 0.5   ]  range 0–1       │
│  ← suppress exit while Z is ≥ X fraction toward threshold   │
│  ← 0.5 = suppress when trade is >50% of the way home        │
│                                                             │
│  ── Fixed-$ Fallback ──                                     │
│  Max Hold (minutes)              [  0    ]  step 1           │
│  ← exit after N min if net P&L > 0                          │
└─────────────────────────────────────────────────────────────┘
```

### Dashboard Display

In the **Signal & Position** card position detail panel:

```
│ Max Hold      │  10m 0s         │  ← limit (shown only when configured)
│ Remaining     │   4m 23s        │  ← countdown; warning colour when < 10% left
```

When the hold limit is nearly reached, the remaining field turns amber then red. When max hold has expired (but is suppressed by z-progress), the field shows `EXPIRED` in grey — the gate is holding it open.

In the **Filters** card, the half-life value is shown so the user can see what the multiplier is scaling against:
```
Half-Life: 42 periods  → max hold at 5× = 210 periods ≈ 1h 45m
```

---

## Exit 4 — Trailing Stop

**Purpose:** Capture profits when P&L has peaked near (but below) the profit target and then starts retracing. Prevents the common scenario where P&L reaches +$4.50, target is +$4.95, and then the trade drifts back to a stop-loss.

### Logic

```
Step 1 — Peak tracking (every tick, unconditional):
  if net_pnl > peak_pnl:
    peak_pnl = net_pnl    ← high-water mark, never goes down

Step 2 — Floor gate (is trailing stop armed?):
  if trailing_stop_floor_pct > 0 AND target_usd > 0:
    armed = (peak_pnl >= (trailing_stop_floor_pct / 100) × target_usd)
  else:
    armed = True    ← active from first profitable tick

Step 3 — Trigger check (only when armed):
  trail_trigger = peak_pnl × (1 − trailing_stop_pct / 100)
  if net_pnl < trail_trigger:
    fire EXIT (reason: TRAILING_STOP)
```

**Worked example:**
```
profit_target_usd     = $5.00
trailing_stop_floor_pct = 70%      → arms when peak_pnl >= $3.50
trailing_stop_pct     = 20%

Timeline:
  Tick 1: net_pnl = +$1.20  → peak=$1.20, floor not met ($1.20 < $3.50), NOT armed
  Tick 2: net_pnl = +$3.80  → peak=$3.80, floor met ($3.80 >= $3.50), ARMED
           trail_trigger = $3.80 × 0.80 = $3.04
  Tick 3: net_pnl = +$4.50  → peak=$4.50, trigger = $4.50 × 0.80 = $3.60
  Tick 4: net_pnl = +$4.20  → still above $3.60, no fire
  Tick 5: net_pnl = +$3.50  → still above $3.60... wait
  Tick 6: net_pnl = +$3.58  → still above $3.60... wait
  Tick 7: net_pnl = +$3.59  → STILL above $3.60... very close
  Tick 8: net_pnl = +$3.55  → $3.55 < $3.60 → FIRE EXIT at ~$3.55
```

The peak is the highest net_pnl ever seen since the trade opened. It is reset to 0.0 when a position is opened or closed.

### Floor gate rationale

Without the floor, a tiny early-trade upward blip (e.g. +$0.10 peak) would arm the trailing stop at a near-zero trigger level. The floor delays arming until the trade has meaningful profit, ensuring the trailing stop only fires when there is actually something worth protecting.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `trailing_stop_pct` | Float | `0.0` | % pullback from peak that fires exit. `0` = disabled. e.g. `20` = exit when P&L drops 20% from peak. |
| `trailing_stop_floor_pct` | Float | `0.0` | Only arm when `peak_pnl >= X% of profit_target`. `0` = arm from first tick in profit. e.g. `70` = arm at 70% of target. |

### Settings UI

Section: **Trailing Stop** (separate card section after velocity exit)

```
┌─────────────────────────────────────────────────────────────┐
│  Pullback %                      [ 20    ]  range 0–100     │
│  ← % drop from peak P&L to trigger exit. 0 = disabled.     │
│  ← Typical: 15–25%                                         │
│                                                             │
│  Floor %                         [ 70    ]  range 0–100     │
│  ← Arm only when P&L has reached X% of profit target.      │
│  ← 0 = arm from any profit. Typical: 60–75%.               │
└─────────────────────────────────────────────────────────────┘
```

Both fields set to 0 = trailing stop disabled.

### Dashboard Display

The trailing stop does not have a dedicated display row in the current dashboard, but the P&L gauge implicitly shows its effect — the moving dot shows how far current P&L is from the peak, and the user can see when the trigger level is approaching.

In the engine log (visible in server output):
```
INFO  Fast-exit override fired: TRAILING_STOP
      P&L $3.55 pulled back 20% from peak $4.50 (trigger=$3.60)
```

The exit reason stamped on the closed trade is `TRAILING_STOP`, visible in the trade journal Exit Reason column.

**Recommended configuration for early profit capture:**
```
profit_target_usd         = 2.50   ← lower the target
trailing_stop_floor_pct   = 70     ← arm at $1.75 (70% of $2.50)
trailing_stop_pct         = 20     ← fire when 20% below peak
```
Result: arms at +$1.75, locks in ~+$2.00 if peak was $2.50, never watches a +$4.50 run back to a stop-loss.

---

## Exit 5 — Hurst Regime Change

**Purpose:** Exit early when the spread transitions from mean-reverting to trending. The core statistical assumption of the trade is violated — better to cut before the dollar stop is hit.

### Logic

```
Each tick:
  if current_hurst > hurst_exit_threshold:
    hurst_exit_count += 1
    if hurst_exit_count >= hurst_exit_n_ticks:
      fire EXIT (reason: HURST_REGIME)
  else:
    hurst_exit_count = 0    ← reset on any tick below threshold
```

N consecutive ticks above the threshold are required to avoid false positives from a single noisy Hurst calculation. The counter resets as soon as Hurst drops back below the threshold.

### Hurst Interpretation

```
H < 0.5 → mean-reverting (anti-persistent) → FAVORABLE — entry allowed
H = 0.5 → random walk
H > 0.5 → trending (persistent) → UNFAVORABLE — entry blocked by Hurst filter
H > 0.55 (exit threshold) → regime has flipped mid-trade → EXIT
```

The exit threshold is typically set slightly higher than the entry filter threshold to create hysteresis — a brief touch of H=0.50 during entry evaluation doesn't flip the exit condition on an already-open trade.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hurst_exit_enabled` | Bool | `False` | Enable regime-change exit. Off by default — enable when trading trending pairs. |
| `hurst_exit_threshold` | Float | `0.55` | H level that signals regime flip. Typically 0.05–0.10 above entry `hurst_threshold`. |
| `hurst_exit_n_ticks` | Int | `3` | Consecutive ticks above threshold before firing. `1` = react immediately; `5` = more conservative. |

### Settings UI

Section: **Post-Entry Exit Overrides — Hurst Regime Exit**

```
┌─────────────────────────────────────────────────────────────┐
│  [✓] Enable Hurst Regime Exit                               │
│                                                             │
│  H Threshold                     [ 0.55  ]  range 0.3–0.9   │
│  ← Exit when Hurst rises above this (spread turned trending) │
│                                                             │
│  Consecutive Ticks               [  3    ]  range 1–20      │
│  ← Require N ticks above threshold before firing            │
└─────────────────────────────────────────────────────────────┘
```

### Dashboard Display

The Hurst value is shown live in the **Statistics & Regime** card:
```
Hurst: 0.58    ← red text when ≥ hurst_threshold
```

And in the **Filters** card badges:
```
Hurst:  NO    ← when entry filter would block (H ≥ entry threshold)
```

Exit reason on closed trade: `HURST_REGIME`

---

## Exit 6 — Spread Velocity

**Purpose:** Exit early when the spread is drifting adversely at an accelerating rate — catching a momentum move before it hits the dollar stop.

### Logic

```
Each tick:
  append current_spread to velocity_window (rolling buffer)

  if len(velocity_window) >= velocity_exit_window_ticks:
    spread_then = velocity_window[−velocity_exit_window_ticks]
    spread_now  = current_spread
    window_min  = velocity_exit_window_ticks × tick_interval_sec / 60

    raw_velocity = (spread_now − spread_then) / window_min   ← pts per minute

    adverse_velocity = raw_velocity      if SHORT position
                     = −raw_velocity     if LONG position

    if adverse_velocity > velocity_exit_pts_per_min:
      velocity_exit_count += 1
      if velocity_exit_count >= velocity_exit_n_ticks:
        fire EXIT (reason: SPREAD_VELOCITY)
    else:
      velocity_exit_count = 0    ← reset on any tick below threshold
```

**Adverse direction:**
- LONG trade (bought spread): spread rising is adverse (spread moving against you)
- SHORT trade (sold spread): spread falling is adverse

**Tick interval:** 0.5 s per tick (engine runs at ~2 Hz). A 20-tick window = 10 seconds.

**Example:**
```
velocity_exit_window_ticks = 20    (10 s window)
velocity_exit_pts_per_min  = 2.0
velocity_exit_n_ticks      = 5

Spread 10 s ago: −120.00
Spread now:      −131.50   (adverse move of 11.50 pts in 10 s)
window_min = 20 × 0.5 / 60 = 0.1667 min
raw_velocity = (−131.50 − (−120.00)) / 0.1667 = −69.0 pts/min
adverse_velocity (LONG) = −(−69.0) = 69.0 pts/min  >> 2.0 threshold
→ velocity_exit_count increments; after 5 consecutive ticks → EXIT
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `velocity_exit_enabled` | Bool | `False` | Enable adverse-drift exit. Off by default. |
| `velocity_exit_pts_per_min` | Float | `2.0` | Spread-point drift per minute to trigger. Tune to the pair's typical volatility. |
| `velocity_exit_n_ticks` | Int | `5` | Consecutive ticks above threshold before firing. |
| `velocity_exit_window_ticks` | Int | `20` | Rolling window length in ticks for velocity calculation. `20` ticks ≈ 10 s at 0.5 s/tick. |

### Settings UI

Section: **Post-Entry Exit Overrides — Spread Velocity Exit**

```
┌─────────────────────────────────────────────────────────────┐
│  [✓] Enable Spread Velocity Exit                            │
│                                                             │
│  pts/min threshold               [ 2.0   ]                  │
│  ← Exit when adverse drift exceeds this rate                │
│                                                             │
│  Consecutive ticks to confirm    [  5    ]  range 1–20      │
│  ← Require N consecutive ticks above threshold              │
│                                                             │
│  Rolling window (ticks, ~0.5 s each)  [ 20 ]  range 5–100  │
│  ← Larger window = smoother but slower to react             │
└─────────────────────────────────────────────────────────────┘
```

### Dashboard Display

No dedicated card. Exit reason on closed trade: `SPREAD_VELOCITY`

The **Spread History** chart on the dashboard shows the spread over time, so a velocity exit will visually correlate with a sharp slope on that chart.

---

## Exit 7 — Z-Score Exit (Normal Path)

**Purpose:** The primary, expected exit — the spread has reverted to near its historical mean and the statistical edge has been captured.

### Logic

Three sub-modes controlled by `exit_signal_mode`:

#### Mode A: `zscore` (default)

```
LONG exit:   current_zscore <= +exit_threshold
SHORT exit:  current_zscore >= −exit_threshold
```

The z-score is the same rolling z-score used for entry, computed every tick. When it crosses back through the exit threshold, the trade is closed.

#### Mode B: `spread`

```
entry_mean = spread_mean frozen at entry time

LONG exit:   current_spread <= entry_mean
SHORT exit:  current_spread >= entry_mean
```

Exits when the live spread crosses the mean that was in effect when the trade opened. Decouples the exit from rolling-mean drift — useful when the mean shifts significantly mid-trade.

#### Mode C: `hybrid`

```
Either Mode A OR Mode B fires → EXIT
```

Whichever condition is met first triggers the exit.

#### Emergency Z-Stop (always active, independent of mode)

```
LONG:  current_zscore >= +stop_loss_threshold → STOP_LOSS
SHORT: current_zscore <= −stop_loss_threshold → STOP_LOSS
```

This fires when the spread has moved further against the position in z-score terms, regardless of the dollar stop. It acts as a second layer of risk control.

### Mean Locking at Entry

When a trade opens, the signal generator **freezes** the current `spread_mean` and `spread_std` onto the trade record. This ensures:
- The `spread` mode has a stable reference that doesn't drift with the rolling window
- The profit target σ-frac formula uses the volatility that existed at entry
- Post-trade analysis can see exactly what the conditions were when the trade was taken

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `exit_threshold` | Float | `0.5` | Z-score magnitude for normal z-score exit |
| `stop_loss_threshold` | Float | `4.0` | Z-score magnitude for emergency z-stop |
| `exit_signal_mode` | String | `zscore` | `zscore` / `spread` / `hybrid` |

### Settings UI

Section: **Z-Score Thresholds**

```
┌─────────────────────────────────────────────────────────────┐
│  Exit Threshold                  [ 0.5   ]  range 0–3       │
│  ← Close LONG when z ≤ +X, close SHORT when z ≥ −X          │
│                                                             │
│  Stop Loss Threshold             [ 4.0   ]  range 2–10      │
│  ← Emergency z-stop when spread widens beyond this level    │
└─────────────────────────────────────────────────────────────┘
```

Section: **⚡ Fast-Exit Overrides**

```
┌─────────────────────────────────────────────────────────────┐
│  Exit Signal Mode                [  Z-Score (original)  ▼ ] │
│  Options:                                                   │
│    Z-Score (original) — exit when rolling Z ≤ ±threshold    │
│    Absolute Spread    — exit when spread crosses entry mean  │
│    Hybrid             — either condition fires exit          │
└─────────────────────────────────────────────────────────────┘
```

### Dashboard Display

**Z-Score card** (large, top of Signal & Position panel):
```
Z-Score: 0.48    ← green/blue when near zero, flips colour at thresholds
```

**Z-Score History Chart:**
- Blue line: z-score over last 100 ticks
- Green dashed lines: ±entry_threshold
- Red dashed lines: ±exit_threshold

When z-score crosses the exit threshold, the trade closes and the recent trades table updates.

---

## Exit Execution — All Conditions

When any of the 7 conditions fires, the same execution path runs:

```
1. Throttle check:
   if (now − last_exit_attempt) < retry_interval → skip this tick

2. Order type selection:
   if exit_postonly_reject_count >= 1 → force MARKET (guarantee close)
   else → POST_ONLY LIMIT (maker fees)

3. Place both legs:
   - Spot/Leg A: SELL (LONG) or BUY (SHORT)
   - Futures/Leg B: BUY (LONG) or SELL (SHORT)
   - Per-leg orderbook snap immediately before placement

4. POST_ONLY rejection (cancelSource=31):
   → cancel the other leg to prevent orphan
   → increment exit_postonly_reject_count
   → wait retry_interval (10 s)
   → on next attempt: force MARKET

5. On fill:
   → capture fill prices (VWAP if multiple fills)
   → fetch actual fees from exchange fill response
   → compute realized P&L
   → stamp exit_reason on trade record
   → apply cooldowns

6. Cooldowns:
   STOP_LOSS exit → 300 s stop-loss cooldown
   Any exit       → entry_cooldown_seconds (default 60 s)
```

### Exit Reason Labels

Each exit stamps a reason code on the trade, visible in the Trade Journal:

| Reason Code | Triggered by |
|-------------|--------------|
| `EXIT_SIGNAL` | Z-score exit (condition 7) |
| `STOP_LOSS` | Dollar stop, emergency z-stop |
| `PROFIT_TARGET` | Profit target (condition 2) |
| `MAX_HOLD` | Max hold timer (condition 3) |
| `TRAILING_STOP` | Trailing stop (condition 4) |
| `HURST_REGIME` | Hurst regime change (condition 5) |
| `SPREAD_VELOCITY` | Spread velocity (condition 6) |
| `MANUAL` | User clicked Close button |

---

## Interaction Between Conditions

Understanding how the conditions interact prevents misconfiguration:

| Scenario | What happens |
|----------|-------------|
| P&L hits profit target AND trailing stop floor in same tick | Profit target fires first (#2 < #4) — correct |
| P&L = +$4.50, target = +$4.95, trade then retraces | Without trailing stop: waits for z-score exit or dollar stop. With trailing stop: exits at ~$3.60 |
| Trade losing, max hold timer expires | Max hold silent (`net_pnl < 0`). Dollar stop still active |
| Trade winning 70%, z-score halfway home, max hold expires | Z-progress gate suppresses max hold — lets it run to target |
| Hurst spikes above threshold for 2 ticks then drops | Hurst exit counter resets on tick 3 — no fire |
| POST_ONLY rejection during stop-loss exit | Retries once as LIMIT, then MARKET on second attempt |
| Dollar stop AND z-score emergency fire same tick | Dollar stop is checked first (#1), z-stop is condition 7 — dollar stop wins |

### Recommended Starting Configuration

For a medium-frequency pairs trade (entry z ≈ 2–3, half-life 30–60 min):

```
Dollar Stop:     stop_loss_capital_pct = 1.0   (1% of margin)
Profit Target:   profit_target_sigma_frac = 0.65, profit_target_min_cost_mult = 1.0
Max Hold:        max_hold_halflife_mult = 2.0,  max_hold_z_progress_min = 0.5
Trailing Stop:   trailing_stop_pct = 20,  trailing_stop_floor_pct = 70
Hurst Exit:      disabled initially; enable after observing H behaviour on the pair
Velocity Exit:   disabled initially; enable if spread tends to trend post-entry
Z-Score Exit:    exit_threshold = 0.5,  exit_signal_mode = zscore
```

This configuration:
- Stops out at ~1% capital loss
- Takes profit when ~65% of the statistical move is captured (scaled to entry conditions)
- Exits a profitable-but-stuck trade after 2× its typical reversion time
- Protects accumulated profit if P&L peaks near target then retraces
- Uses simple z-score as primary exit
