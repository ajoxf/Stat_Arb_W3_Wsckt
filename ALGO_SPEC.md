# Statistical Arbitrage — System Specification

**Pairs trading engine for perpetual swap / spot spreads. All parameters are configurable; logic is exchange-agnostic.**

---

## Table of Contents

1. [Core Concept](#1-core-concept)
2. [Spread & Z-Score](#2-spread--z-score)
3. [Statistical Indicators](#3-statistical-indicators)
4. [Entry Filters](#4-entry-filters)
5. [Position Sizing & Margin](#5-position-sizing--margin)
6. [Entry Execution](#6-entry-execution)
7. [Exit Hierarchy](#7-exit-hierarchy)
8. [Exit Execution](#8-exit-execution)
9. [P&L Calculation](#9-pl-calculation)
10. [Full Decision Flow](#10-full-decision-flow)
11. [Configuration Reference](#11-configuration-reference)
12. [In-Position Dashboard Card](#12-in-position-dashboard-card)

---

## 1. Core Concept

A **cash-and-carry spread** between two correlated instruments — typically a perpetual swap (futures leg) and a spot instrument on the same or related underlying. When the spread deviates far enough from its historical mean (measured in standard deviations), the engine enters expecting mean reversion. The two legs are always traded simultaneously and sized to be dollar-neutral after applying the hedge ratio.

```
Leg A  (spot-equivalent):   Buy/Sell spot or spot-like instrument
Leg B  (futures):           Sell/Buy perpetual swap or futures contract
Spread = Futures_Price − β × Spot_Price
```

**LONG spread** = buy Leg A + sell Leg B → profits when spread falls  
**SHORT spread** = sell Leg A + buy Leg B → profits when spread rises

---

## 2. Spread & Z-Score

### Spread Formula

```
spread(t) = futures_price(t) − β × spot_price(t)
```

- **β (hedge_ratio)**: Default `1.0` for same-underlying pairs. For cross-pair: `β = futures_mid / spot_mid` (user-configurable).

### Rolling Statistics

```
μ  = mean(spread[-lookback_period:])
σ  = std(spread[-lookback_period:], ddof=1)
```

- Updated every `stats_update_interval` seconds (default 300 s).
- Z-score is recalculated on **every tick** using the most-recently-computed μ and σ.
- Minimum data required before trading: full `lookback_period` points (system is in **COLLECTING** regime until then).

### Z-Score

```
z(t) = (spread(t) − μ) / σ
```

### Entry Thresholds

```
LONG  signal:  z ≥ +entry_threshold    (spread is too high → expect fall)
SHORT signal:  z ≤ −entry_threshold    (spread is too low → expect rise)
```

### Exit Thresholds (Z-Score Mode)

```
LONG exit:   z ≤ +exit_threshold
SHORT exit:  z ≥ −exit_threshold
```

### Emergency Z-Stop

```
LONG emergency:  z ≥ +stop_loss_threshold  (spread moved further against us)
SHORT emergency: z ≤ −stop_loss_threshold
```

### Exit Signal Modes (`exit_signal_mode`)

| Mode | Logic |
|------|-------|
| `zscore` | Exit when z crosses back through `exit_threshold` (default) |
| `spread` | Exit when live spread crosses the `entry_mean` frozen at trade open |
| `hybrid` | Either condition above triggers exit |

The `spread` and `hybrid` modes decouple exit from rolling-mean drift — useful when the mean shifts significantly after entry.

---

## 3. Statistical Indicators

### Hurst Exponent (H)

Measures persistence of the spread time series.

```
Computed via R/S analysis over sub-series of lengths k ∈ [10, 50]
H = slope of log(R/S) vs log(k)
Clipped to [0, 1]
```

| Range | Regime | Meaning |
|-------|--------|---------|
| H < 0.4 | MEAN_REVERTING | Spread oscillates — favorable for entry |
| 0.4 ≤ H ≤ 0.6 | NEUTRAL | Mixed |
| H > 0.6 | TRENDING | Spread is drifting — unfavorable |

Requires ≥ 20 data points; updated every `stats_update_interval` seconds.

### Half-Life (Ornstein-Uhlenbeck Process)

Measures how quickly the spread reverts to mean.

```
OU model:  Δspread(t) = θ × (μ − spread(t)) + noise
Fit via OLS:
  x = μ − spread(t)
  y = Δspread(t)
  θ = Σ(x·y) / Σ(x²)
  
half_life = ln(2) / θ     (when θ > 0)
```

- Expressed in the same units as `lookback_period` (ticks/periods).
- Recommended lookback: `2.5 × half_life` (midpoint of the 2–5× valid range).
- Used by max-hold and STD filter to scale time-based exits to the specific pair's reversion speed.

---

## 4. Entry Filters

All filters must pass before an entry signal is acted on.

### Filter 1 — Hurst Filter

```
PASS if:  hurst_enabled == False
       OR current_hurst < hurst_threshold
```

- Blocks entry when the spread is in a trending regime.
- **hurst_enabled**: default `True` | **hurst_threshold**: default `0.5`

### Filter 2 — STD Filter (Edge-to-Cost Gate)

Ensures the expected profit from the trade exceeds the round-trip transaction cost by a configured multiple.

**Step 1 — Expected capturable move (in spread units)**

```
If profit_target_sigma_frac > 0:
  capture = σ_frac × |z_entry| × σ          (scale-invariant)
Else:
  capture = max(|z_entry| − exit_threshold, 0) × σ   (based on distance to exit)
```

**Step 2 — Round-trip cost (in spread units)**

```
round_trip_bps = entry_fees + exit_fees + slippage

Per leg, fee schedule:
  Spot or spot-like:     LIMIT → spot_maker_fee_bps,  MARKET → spot_taker_fee_bps
  Futures / derivative:  LIMIT → futures_maker_fee_bps, MARKET → futures_taker_fee_bps

Total for 4 legs:
  cost_bps = spot_entry_bps + fut_entry_bps + spot_exit_bps + fut_exit_bps + 4 × slippage_bps

cost_price = (cost_bps / 10000) × β × spot_price     (converts to spread units)
```

**Step 3 — Gate**

```
edge_ratio = capture / cost_price
Required:   edge_ratio ≥ max(min_std_multiple, profit_target_min_cost_mult)
```

- **std_filter_enabled**: default `True`
- **min_std_multiple**: default `0.9` (must recover ~90% of cost; just above break-even)
- **profit_target_min_cost_mult**: default `0.0` (disabled); set ≥ 1.0 for "profit target must exceed total fees"

### Filter 3 — Z-Score Floor

```
BLOCKED if:  z ≥ stop_loss_threshold (LONG)
          or z ≤ −stop_loss_threshold (SHORT)
```

Prevents entering into already-extreme spreads that could widen further.

### Filter 4 — Trend Direction Filter (Optional)

```
slope = linear_slope(spread[-20% of lookback:])
LONG blocked  if slope > 0  (spread is rising, adverse for LONG)
SHORT blocked if slope < 0  (spread is falling, adverse for SHORT)
```

- **trend_direction_filter**: default `False`

---

## 5. Position Sizing & Margin

### Quantities

```
spot_qty    = position_size_usd / spot_price
futures_qty = spot_qty / β
```

Quantities are rounded down to exchange contract minimums before placement.

### Notional

```
leg_a_notional = entry_spot_price × spot_qty
leg_b_notional = entry_futures_price × futures_qty
total_notional = leg_a_notional + leg_b_notional
```

### Margin & Capital Locked

```
leg_a_leverage = spot_leverage    (if spot is a derivative, else 1)
leg_b_leverage = futures_leverage

margin_a = leg_a_notional / leg_a_leverage
margin_b = leg_b_notional / leg_b_leverage

capital_locked = (margin_a + margin_b) × (1 + m2m_buffer_pct / 100)
```

The M2M buffer (`m2m_buffer_pct`, default `10%`) reserves extra capital for mark-to-market fluctuations while the trade is open.

### Balance Check (Pre-Entry)

```
required_balance = capital_locked
available_balance ≥ required_balance    (or entry is blocked)
```

---

## 6. Entry Execution

### Pre-Entry Guards (All Must Pass)

1. Position state is NONE (no open trade)
2. No order execution currently in flight
3. Stop-loss cooldown has expired (300 s after a stop-loss close)
4. Entry cooldown has expired (`entry_cooldown_seconds`, default 60 s)
5. Daily loss limit not breached (`daily_max_loss_usd`)
6. No existing position on exchange (if `verify_exchange_position = True`)
7. No pending open orders on exchange (prevents duplicates)
8. Both spot and futures price ticks are available
9. `position_size_usd ≤ max_position_size_usd`
10. Available balance ≥ capital_locked (checked via exchange API, cached 3 s)
11. Both leg quantities exceed exchange minimums

### TWAP / Slicing

```
For slice i in [1 .. entry_slices]:
  Place both legs simultaneously
  Wait entry_slice_interval_sec between slices
  VWAP-blend fill prices across all slices
```

Entry is rejected if total fill ratio < `min_fill_ratio` (default 95%).

### Order Types

- Default: `POST_ONLY` limit orders (maker fees, no price crossing)
- If `cancelSource=31` (POST_ONLY rejected): retry with fresh per-leg price snap, 10 s cooldown
- `limit_order_price_offset_bps` (default 1.0 bps): offset from best bid/ask for passive fill
- Timeout: `limit_order_timeout_sec` (default 30 s) before fallback

### Per-Leg Price Snap (Anti-Stale Pricing)

Each leg gets its own fresh orderbook snap immediately before its `place_order` call. This prevents the second leg from using a price that is ~600 ms stale (one full leg RTT). Staleness at placement is reduced to ~50 ms.

### Orphan Recovery

If one leg fills and the other is cancelled (e.g. POST_ONLY rejection), the engine attempts maker-rate recovery (LIMIT order) on the unfilled leg before falling back to MARKET. This avoids paying taker fees + slippage on every leg-risk event.

---

## 7. Exit Hierarchy

Evaluated on every tick (~0.5 s). **First match wins — checked in this exact order.**

### Override Exits (Dollar / Time Based)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  #1  DOLLAR_STOP        (always first — risk before reward)                 │
│      net_pnl ≤ −stop_usd                                                    │
│      → signal = STOP_LOSS                                                   │
│                                                                             │
│  #2  PROFIT_TARGET                                                          │
│      net_pnl ≥ target_usd                                                   │
│      → signal = EXIT                                                        │
│                                                                             │
│  #3  MAX_HOLD           (only when net_pnl > 0)                             │
│      time/periods held ≥ threshold                                          │
│      suppressed if Z-progress ≥ z_progress_min (trade making progress)     │
│      → signal = EXIT                                                        │
│                                                                             │
│  #4  TRAILING_STOP                                                          │
│      peak_pnl tracked; armed when peak ≥ floor_pct% of target              │
│      fires when net_pnl < peak_pnl × (1 − trail_pct%)                      │
│      → signal = EXIT                                                        │
│                                                                             │
│  #5  HURST_REGIME       (if hurst_exit_enabled)                             │
│      H > hurst_exit_threshold for N consecutive ticks                      │
│      → signal = EXIT                                                        │
│                                                                             │
│  #6  SPREAD_VELOCITY    (if velocity_exit_enabled)                          │
│      adverse spread drift > threshold for N consecutive ticks              │
│      → signal = EXIT                                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Z-Score Exit (Normal Path)

```
#7  Z-SCORE / SPREAD EXIT
    exit_signal_mode controls which condition fires (see Section 2)
    → signal = EXIT
    
    Emergency Z-stop:
    → signal = STOP_LOSS
```

---

### Detailed Override Logic

#### #1 — Dollar Stop

```
If stop_loss_capital_pct > 0:
  stop_usd = (stop_loss_capital_pct / 100) × capital_locked
Else:
  stop_usd = max_loss_usd

Fires when: net_pnl ≤ −abs(stop_usd)
```

#### #2 — Profit Target

```
If profit_target_sigma_frac > 0:
  target_usd = profit_target_sigma_frac × |entry_zscore| × entry_spread_std × futures_qty
Else:
  target_usd = profit_target_usd

Cost floor (if profit_target_min_cost_mult > 0):
  target_usd = max(target_usd, cost_mult × round_trip_cost_usd)

Fires when: net_pnl ≥ target_usd
```

The σ-fraction form scales the target with the actual entry z-score and volatility — a large z-score at a volatile time sets a larger dollar target automatically.

#### #3 — Max Hold

```
Half-life form (if max_hold_halflife_mult > 0):
  max_hold_periods = max_hold_halflife_mult × half_life
  periods_held = total_ticks − entry_tick_count
  Fires when: periods_held ≥ max_hold_periods AND net_pnl > 0

Fixed-minutes form (if max_hold_minutes > 0):
  held_minutes = (now − entry_time).total_seconds() / 60
  Fires when: held_minutes ≥ max_hold_minutes AND net_pnl > 0
```

**Z-Progress Gate (suppresses max hold when trade is "working"):**

```
entry_abs  = |entry_zscore|
cur_abs    = |current_zscore|
exit_abs   = |exit_threshold|
journey    = entry_abs − exit_abs
z_progress = (entry_abs − cur_abs) / journey

If z_progress ≥ max_hold_z_progress_min → MAX_HOLD suppressed
```

Max hold only fires when the trade is profitable **and** not actively reverting toward target. Completely inert when `net_pnl < 0`.

#### #4 — Trailing Stop

```
Peak tracking (every tick):
  if net_pnl > peak_pnl: peak_pnl = net_pnl

Floor gate:
  If trailing_stop_floor_pct > 0 AND target_usd > 0:
    armed = (peak_pnl ≥ (trailing_stop_floor_pct / 100) × target_usd)
  Else:
    armed = True  (active from first profitable tick)

Trigger:
  trail_trigger = peak_pnl × (1 − trailing_stop_pct / 100)
  Fires when: armed AND net_pnl < trail_trigger
```

**Example**: `profit_target_usd=$5.00`, `trailing_stop_floor_pct=70`, `trailing_stop_pct=20`
- Arms when `peak_pnl ≥ $3.50` (70% of $5.00)
- If peak hits $4.50, trigger = $4.50 × 0.80 = **$3.60**
- Position exits at $3.60 rather than waiting for $5.00 or reverting to a stop-loss

#### #5 — Hurst Regime-Change Exit

```
Each tick:
  If hurst > hurst_exit_threshold:
    consecutive_count += 1
    If consecutive_count ≥ hurst_exit_n_ticks → EXIT
  Else:
    consecutive_count = 0
```

Exits early when the spread flips from mean-reverting to trending, before the dollar stop is hit.

#### #6 — Spread Velocity Exit

```
window_minutes = velocity_exit_window_ticks × 0.5s / 60
velocity = (spread_now − spread[−velocity_exit_window_ticks]) / window_minutes

adverse_velocity = velocity  if SHORT position
                 = −velocity if LONG position

Each tick:
  If adverse_velocity > velocity_exit_pts_per_min:
    consecutive_count += 1
    If consecutive_count ≥ velocity_exit_n_ticks → EXIT
  Else:
    consecutive_count = 0
```

Catches accelerating adverse drift before it hits the dollar stop.

---

## 8. Exit Execution

```
1. Throttle check: skip if last attempt < retry_interval ago
2. POST_ONLY rejection count:
   If count ≥ 1 → force MARKET (guarantees close)
   Else         → POST_ONLY LIMIT (maker fees)
3. Place both legs simultaneously
4. On POST_ONLY rejection (cancelSource=31):
   → Increment rejection count
   → Retry next tick (≈10 s cooldown)
   → On next attempt: MARKET order
5. On fill: capture actual fill prices and actual fees from exchange API
6. Compute realized P&L (see Section 9)
7. Apply cooldowns:
   STOP_LOSS close → 300 s stop-loss cooldown
   Any close       → entry_cooldown_seconds (default 60 s)
8. Update daily_loss_usd tracker
```

---

## 9. P&L Calculation

### Gross P&L

```
entry_spread = entry_futures − β × entry_spot
exit_spread  = exit_futures  − β × exit_spot

LONG:   spread_change = entry_spread − exit_spread
SHORT:  spread_change = exit_spread  − entry_spread

pnl_gross = spread_change × futures_quantity
```

### Fees

Actual fees are fetched from the exchange fill response for each leg. If unavailable, estimated from configured bps rates:

```
Actual (preferred):
  fees = |spot_entry_fee_from_exchange| + |futures_entry_fee_from_exchange|
       + |spot_exit_fee_from_exchange|  + |futures_exit_fee_from_exchange|

Estimated (fallback):
  fees = (a_entry/10000) × spot_qty    × entry_spot
       + (b_entry/10000) × futures_qty × entry_futures
       + (a_exit /10000) × spot_qty    × exit_spot
       + (b_exit /10000) × futures_qty × exit_futures

Where a/b = maker_bps (LIMIT) or taker_bps (MARKET) per leg
```

### Net P&L

```
pnl_net     = pnl_gross − fees
pnl_percent = (pnl_net / notional_usd) × 100
```

### Return on Locked Capital

```
pnl_pct_on_capital = (pnl_net / capital_locked) × 100
```

This metric reflects the actual return on margin deployed, which is 3–5× higher than the notional-based percentage at typical leverage.

### Live (Unrealized) P&L

Uses current mid prices as exit price proxies; same fee formula as realized close. If the exit mode has been forced to MARKET (after a POST_ONLY rejection), taker fees are applied in the live estimate.

---

## 10. Full Decision Flow

```
╔═══════════════════════════════════════════════════════════════════════╗
║                         TICK LOOP (~0.5 s)                           ║
╚═══════════════════════════════════════════════════════════════════════╝
                              │
                  ┌───────────▼───────────┐
                  │  Fetch spot & futures  │
                  │  prices (bid/ask/mid)  │
                  └───────────┬───────────┘
                              │
                  ┌───────────▼───────────┐
                  │ Compute spread, z-score│
                  │ Update stats if interval│
                  │ passed (every 300 s)   │
                  └───────────┬───────────┘
                              │
                 ┌────────────▼─────────────┐
                 │   IN POSITION?            │
                 └────┬─────────────┬────────┘
                      │YES          │NO
                      ▼             ▼
         ┌────────────────────┐  ┌─────────────────────────┐
         │ CHECK EXIT OVERRIDES│  │ CHECK ENTRY FILTERS      │
         │ (in order #1→#6)   │  │ 1. Hurst < threshold?    │
         │                    │  │ 2. Edge ratio ≥ minimum?  │
         │ If none fire:      │  │ 3. Z not at stop level?   │
         │ CHECK Z-SCORE EXIT │  │ 4. Trend direction OK?    │
         │   (#7)             │  └────────────┬────────────┘
         └─────────┬──────────┘               │ALL PASS
                   │                          ▼
         ┌─────────▼──────────┐  ┌─────────────────────────┐
         │  EXIT SIGNAL?       │  │ Z ≥ entry_threshold?     │
         │  YES → Execute exit │  │ YES → entry_signal        │
         │  NO  → Continue     │  └────────────┬────────────┘
         └────────────────────┘               │
                                   ┌──────────▼──────────────┐
                                   │ ENGINE GUARDS (11 checks)│
                                   │ Balance, cooldowns, etc. │
                                   └──────────┬──────────────┘
                                              │ALL PASS
                                   ┌──────────▼──────────────┐
                                   │  EXECUTE ENTRY           │
                                   │  TWAP slices, VWAP blend │
                                   │  Record fills, fees      │
                                   │  Set position = LONG/SHORT│
                                   └─────────────────────────┘
```

---

## 11. Configuration Reference

### Z-Score & Thresholds

| Parameter | Default | Description |
|-----------|---------|-------------|
| `entry_threshold` | `2.0` | Z-score magnitude to trigger entry |
| `exit_threshold` | `0.5` | Z-score magnitude to trigger z-score exit |
| `stop_loss_threshold` | `4.0` | Z-score magnitude to block entry / emergency exit |
| `exit_signal_mode` | `zscore` | `zscore` \| `spread` \| `hybrid` |
| `lookback_period` | `100` | Rolling window size for μ and σ |
| `stats_update_interval` | `300` | Seconds between μ/σ recalculation |
| `hedge_ratio` | `1.0` | β for spread formula |

### Position Sizing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `position_size_usd` | `1000.0` | Nominal position size per trade |
| `max_position_size_usd` | `10000.0` | Hard cap on position size |
| `spot_leverage` | `1` | Leverage applied to spot/Leg A |
| `futures_leverage` | `1` | Leverage applied to futures/Leg B |
| `m2m_buffer_pct` | `10.0` | Extra capital held for mark-to-market swings |
| `daily_max_loss_usd` | `0.0` | Halt trading when daily loss exceeds this |

### Fees & Slippage

| Parameter | Default | Description |
|-----------|---------|-------------|
| `spot_maker_fee_bps` | `8.0` | Spot leg, LIMIT fill fee |
| `spot_taker_fee_bps` | `10.0` | Spot leg, MARKET fill fee |
| `futures_maker_fee_bps` | `2.0` | Futures leg, LIMIT fill fee |
| `futures_taker_fee_bps` | `5.0` | Futures leg, MARKET fill fee |
| `slippage_bps` | `1.5` | Per-leg slippage allowance |

> **OKX VIP4 actual rates**: futures maker = 0.8 bps, taker = 2.7 bps. Update these to match your exchange tier.

### Entry Filters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `hurst_enabled` | `True` | Enable Hurst regime filter |
| `hurst_threshold` | `0.5` | Maximum H for entry to be allowed |
| `std_filter_enabled` | `True` | Enable edge-to-cost gate |
| `min_std_multiple` | `0.9` | Minimum edge-ratio to allow entry |
| `profit_target_min_cost_mult` | `0.0` | Cost floor multiplier applied to target |
| `trend_direction_filter` | `False` | Block entry if slope opposes direction |

### Order Execution

| Parameter | Default | Description |
|-----------|---------|-------------|
| `entry_execution_mode` | `LIMIT` | `LIMIT` or `MARKET` |
| `exit_execution_mode` | `LIMIT` | `LIMIT` or `MARKET` |
| `limit_order_timeout_sec` | `30` | Cancel limit after this many seconds |
| `limit_order_price_offset_bps` | `1.0` | Offset from best bid/ask |
| `entry_slices` | `1` | TWAP slice count |
| `entry_slice_interval_sec` | `5.0` | Delay between TWAP slices |
| `min_fill_ratio` | `0.95` | Minimum fill to accept entry |
| `entry_cooldown_seconds` | `60` | Wait after any close before re-entering |

### Profit Target (#2)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `profit_target_sigma_frac` | `0.0` | Scale target to `σ_frac × \|Z_entry\| × σ` (recommended: 0.6–0.7) |
| `profit_target_usd` | `0.0` | Fixed-dollar target (fallback when σ-frac = 0) |
| `profit_target_min_cost_mult` | `0.0` | Target must be ≥ this × round-trip cost |

### Dollar Stop (#1)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `stop_loss_capital_pct` | `0.0` | Stop at X% of locked capital (recommended: 0.5–1.0) |
| `max_loss_usd` | `0.0` | Fixed-dollar stop (fallback) |

### Max Hold (#3)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_hold_halflife_mult` | `0.0` | Max hold = mult × half_life periods (recommended: 1.5–2.0) |
| `max_hold_minutes` | `0.0` | Fixed-minutes fallback |
| `max_hold_z_progress_min` | `0.5` | Suppress max hold when Z is this far home (0–1) |

### Trailing Stop (#4)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `trailing_stop_pct` | `0.0` | Pullback % from peak to trigger exit |
| `trailing_stop_floor_pct` | `0.0` | Only arm when P&L has reached X% of target |

### Hurst Exit (#5)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `hurst_exit_enabled` | `False` | Enable regime-change exit |
| `hurst_exit_threshold` | `0.55` | H level that signals trending |
| `hurst_exit_n_ticks` | `3` | Consecutive ticks above threshold to fire |

### Spread Velocity Exit (#6)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `velocity_exit_enabled` | `False` | Enable adverse-drift exit |
| `velocity_exit_pts_per_min` | `2.0` | Adverse drift threshold (spread-pts/min) |
| `velocity_exit_n_ticks` | `5` | Consecutive ticks above threshold to fire |
| `velocity_exit_window_ticks` | `20` | Rolling window for velocity (20 ticks ≈ 10 s) |

---

## 12. In-Position Dashboard Card

All fields shown on the live in-position panel while a trade is open:

### Trade Identity

| Field | Description |
|-------|-------------|
| `id` | Trade ID (sequential integer) |
| `asset` | Asset pair (e.g. `ETH/BTC`) |
| `position_type` | `LONG` or `SHORT` |
| `is_paper` | `true` if paper trading |

### Entry Details

| Field | Description |
|-------|-------------|
| `entry_time` | UTC timestamp when position was opened |
| `entry_spot_price` | VWAP fill price for spot/Leg A |
| `entry_futures_price` | VWAP fill price for futures/Leg B |
| `entry_spread` | Spread at fill = `entry_futures − β × entry_spot` |
| `entry_zscore` | Z-score at the moment of entry |
| `entry_spread_mean` | Rolling μ frozen at entry (used as spread-exit target) |
| `entry_spread_std` | Rolling σ frozen at entry (used for target scaling) |
| `entry_placed_at` | When the order was sent to the exchange |
| `entry_filled_at` | When both legs confirmed filled |
| `entry_latency_ms` | Milliseconds from order placement to fill confirmation |
| `spot_order_id` | Exchange order ID for spot/Leg A |
| `futures_order_id` | Exchange order ID for futures/Leg B |

### Live State

| Field | Description |
|-------|-------------|
| `unrealized_pnl` | Live net P&L in USD (mid-price based, fees deducted) |
| `held_minutes` | Minutes elapsed since entry |
| `quantity` | Futures quantity (BTC or contract equivalent) |
| `notional_usd` | Total notional value (both legs) |
| `margin_usd` | Total margin locked (both legs, excluding M2M buffer) |

### Exit Targets (Resolved in Live Dollars)

| Field | Description |
|-------|-------------|
| `exit_target_usd` | Profit target in USD (0 if disabled) |
| `exit_stop_usd` | Dollar stop in USD (0 if disabled) |
| `round_trip_cost` | Cost floor applied to profit target (0 if inactive) |
| `max_hold_periods` | Max periods before max-hold exit (0 if disabled) |
| `periods_held` | Periods elapsed vs `max_hold_periods` |
| `max_hold_minutes` | Max minutes form (0 if using half-life form) |

### Signal State (Live)

| Field | Description |
|-------|-------------|
| `zscore` | Current z-score |
| `spread` | Current spread value |
| `spread_mean` | Current rolling mean |
| `spread_std` | Current rolling σ |
| `hurst` | Current Hurst exponent |
| `half_life` | Current mean-reversion half-life (periods) |
| `regime` | `MEAN_REVERTING` \| `TRENDING` \| `NEUTRAL` \| `COLLECTING` |
| `hurst_ok` | Whether Hurst filter would pass |
| `std_filter_ok` | Whether STD/edge filter would pass |
| `std_ratio` | Current edge ratio (expected move / cost) |
| `round_trip_cost_bps` | Total round-trip cost in basis points |

### Engine Status

| Field | Description |
|-------|-------------|
| `algo_enabled` | Whether the engine is allowed to trade |
| `paper_trading` | Live or paper mode |
| `spot_connected` | Spot feed connected |
| `futures_connected` | Futures feed connected |
| `sl_cooldown_remaining` | Seconds remaining on stop-loss cooldown |
| `executing_trade` | Order placement in flight |
| `daily_loss_usd` | Cumulative realized loss today |
| `daily_loss_limit` | Max daily loss before auto-halt |
| `position_mismatch` | Exchange position vs engine state mismatch (if any) |
| `entry_execution_mode` | `LIMIT` or `MARKET` |
| `exit_execution_mode` | `LIMIT` or `MARKET` |

---

## Key Formulas Summary

| Formula | Purpose |
|---------|---------|
| `spread = F − β·S` | Core arbitrage metric |
| `z = (spread − μ) / σ` | Entry/exit signal |
| `H = slope(log(R/S) vs log(n))` | Regime detection |
| `HL = ln(2) / θ` | Mean-reversion speed |
| `edge_ratio = capture / cost` | STD filter gate |
| `capture = σ_frac × \|Z\| × σ` | Expected profit (scale-invariant) |
| `cost = (bps/10000) × β × spot` | Round-trip cost in spread units |
| `pnl_gross = Δspread × qty` | Before fees |
| `fees = Σ (bps/10000 × notional_per_leg)` | Per-leg fee estimate |
| `capital_locked = margin × (1 + buf%)` | Capital denominator |
| `pnl_pct_on_capital = pnl / capital_locked × 100` | True return on deployed capital |
| `trail_trigger = peak_pnl × (1 − trail_pct%)` | Trailing stop trigger |
| `z_progress = (entry_abs − cur_abs) / (entry_abs − exit_abs)` | How far trade has reverted |

---

# UI Specification

**Stack:** Flask (Python) + Socket.IO (real-time) + Bootstrap 5 + Chart.js. Four pages. All real-time updates via Socket.IO events; no polling except account balance (3 s cache).**

---

## UI Architecture Overview

```
Browser
  ├── Base Layout (base.html)           — navigation, mode pill, algo toggle, modals
  ├── Dashboard (/)                     — live trading view, charts, position card
  ├── Settings (/settings)              — all parameters, one-page form
  ├── Exchanges (/setup)                — API key management, exchange selection
  └── Analysis (/analysis)             — trade journal, SD touch history, stats

Server
  ├── Flask HTTP routes                  — config CRUD, engine control, data exports
  └── Socket.IO namespace "/"           — tick, signal, trade, error, status events
```

Real-time data flows via Socket.IO. The engine emits events every tick (~0.5 s); the browser renders them without page reload. HTTP routes handle one-off actions (save config, toggle algo, close position, download CSV).

---

## Base Layout (all pages)

### Top Navigation Bar

| Element | Description | Behaviour |
|---------|-------------|-----------|
| Logo | `logo-nexus.svg` left of brand name | Links to `/dashboard` |
| Nav: Dashboard | Link | Highlights active page |
| Nav: Settings | Link | — |
| Nav: Exchanges | Link | — |
| Nav: Analysis | Link | — |
| Mode Pill | Badge top-right | PAPER = yellow, DEMO = blue, LIVE = red (pulsing animation) |
| Connection Dot | Coloured circle | Green = Socket.IO connected, red = disconnected |
| Algo Toggle | Checkbox + label "Algo" | Opens confirmation modal before enabling; calls `POST /api/engine/toggle-algo` |

### Algo Enable Confirmation Modal

Shown before the algo switch is set to ON. Content changes by mode:

| Mode | Header Colour | Icon | Body Content |
|------|--------------|------|--------------|
| LIVE | Red | Warning octagon | Pre-flight checklist: position size, leverage, API trade permission |
| DEMO | Blue | Robot | "Safe demo environment" message |
| PAPER | Grey | Document | "Simulation — no real orders" message |

---

## Page 1 — Dashboard (`/`)

### Account Bar (full width)

One card spanning all columns. Updated by `tick` event.

| Column | Field | Format |
|--------|-------|--------|
| 1 | Exchange name | Text (OKX / Binance / Bybit) |
| 1 | Execution backend badge | REST or WS Streaming |
| 2 | UID | User ID string |
| 3 | Equity | `$0.00` — green if positive |
| 4 | Available | `$0.00` |
| 5 | Unrealized P&L | `$0.00` — green positive, red negative |
| 5 | Last updated | Timestamp |
| 6 | Margin health | `SAFE` / `WARNING` / `DANGER` / `N/A` with colour badge |

### Warning Banners (conditional)

| Banner | Trigger | Background | Actions |
|--------|---------|------------|---------|
| **Live Trading** | `!paper_trading && !demo` | Dark red | None — informational |
| **Orphaned Leg A** | Stale balance detected on spot leg | Yellow | "Sell to USDT" → `POST /api/close-orphaned-spot` · "Dismiss" |
| **Dust Positions** | Any sub-$1 position exists | Light blue | "Sweep" → `POST /api/sweep-dust` |
| **Position Mismatch** | Engine state ≠ exchange state | Red | "Recover" or "Clear" → `POST /api/engine/sync-position` |

### Trading Status Banner (full width)

Shows data-collection progress until the lookback window is full.

| Element | Content | Update |
|---------|---------|--------|
| Icon | Hourglass (collecting) → Check (ready) | `signal` event |
| Status text | "Collecting Data…" / "Ready to Trade" / "In Position" | `signal` event |
| Detail text | "Waiting for lookback period" / filter reason | `signal` event |
| Progress bar | Striped, animated while collecting | `data_points / lookback` |
| Count | `X / Y (Z%)` | `signal` event |

---

### Left Column (`col-lg-5`)

#### Card: Leg A Prices

- Header badge: symbol (e.g. `ETH-USDT-SWAP`)
- Large headline price — colour-coded: green = up tick, red = down tick
- Bid (green) and Ask (red) sub-rows

#### Card: Leg B Prices

Same layout as Leg A.

#### Card: Position Sizing

Two sub-columns (Leg A / Leg B):

| Row | Field |
|-----|-------|
| Notional | `$X,XXX.XX` or `—` |
| Leverage | `1x` / `20x` / `Cash` or `—` |
| Margin | `$X,XXX.XX` or `—` |

Footer explains sizing: "Leg A = anchor (`position_size_usd / spot_price`), Leg B scales by `β × mid`."

#### Card: Live Hedge Ratio (β)

One row:
- **Live β** value in blue (computed from live prices)
- **Configured** value
- **Drift badge** — `+0.45%` colour-coded: green ≤ 0.5%, amber ≤ 2%, red > 2%

#### Card: Signal & Position

This is the primary in-position card. Updated on every `signal` and `trade` event.

**Always-visible header row (3 columns):**

| Column | Field | Detail |
|--------|-------|--------|
| Left | Z-Score | Large number; blue if `|z| < exit_threshold`, green if above +entry, red if below −entry |
| Centre | Spread | Medium number; green if positive, red if negative |
| Right | Position badge | `FLAT` (grey) / `LONG` (green) / `SHORT` (red) |

- **Close button** (top-right, outline): visible only when in position → `POST /api/engine/close-position`
- **Position sync dropdown**: "Recover from DB" / "Clear Position" → `POST /api/engine/sync-position`
- **SL Cooldown badge**: shown while stop-loss cooldown active — `SL COOLDOWN Xs`

**Position detail panel** (hidden when flat, shown when in position):

| Row | Left cell | Right cell |
|-----|-----------|------------|
| 1 | Entry Z-Score | Net P&L (`$X.XX`) — green/red |
| 2 | Leg A Entry Price | Leg B Entry Price |
| 3 | Entry Spread | Δ Spread (current − entry) |
| 4 | Position Age (`h:mm:ss`, live ticker) | Position Size (`$X,XXX`) |
| 5 *(if max-hold set)* | Max Hold limit | Remaining time — warning colour if < 10 min |
| 6 *(if targets set)* | Profit Target (`+$X.XX`) | Stop Loss (`−$X.XX`) |

**P&L Progress Gauge** (below detail rows):
- Horizontal gradient bar spanning stop-loss (left, red) → zero (centre) → profit-target (right, green)
- Moving indicator dot showing current net P&L position on the bar
- Breakeven marker at centre
- Labels at each end: `−$stop` and `+$target`

#### Card: Z-Score History Chart

- Type: Line (Chart.js)
- Max 100 points; rolling FIFO
- Datasets:

| Dataset | Style |
|---------|-------|
| Z-Score | Blue line, 1.5 px |
| ±Entry threshold | Green dashed lines |
| ±Exit threshold | Red thin dashed lines |

- Y-axis: −5 to +5
- X-axis: hidden (time implied by recency)

#### Card: Spread History Chart

- Type: Line (Chart.js)
- Max 100 points; rolling FIFO
- Single dataset: purple line, auto-scaled Y-axis

---

### Middle Column (`col-lg-4`)

#### Card: Statistics & Regime

**Row 1 — four metric boxes:**

| Box | Field | Colour coding |
|-----|-------|---------------|
| Mean | Rolling μ | Blue |
| Std Dev | Rolling σ | Red |
| Hurst | H value | Green if < 0.5, red if ≥ 0.5 |
| Regime | Text label | Yellow = COLLECTING, green = MEAN_REVERTING, red = TRENDING |

**Row 2:**
- Half-life (periods): green < 50, yellow < 200, red ≥ 200
- Suggested Lookback: `≈ 2.5 × half_life` pts

**Row 3:**
- Data progress bar (green, 0–100%) with count `X / Y (Z%)`

#### Card: Filters

**Row 1 — four status badges:**

| Badge | States |
|-------|--------|
| Hurst | `—` / `OK` (green) / `NO` (red) |
| Edge | `—` / `OK` (green) / `NO` (red) |
| Regime | `—` / `WAIT` / `MR` (green) / `TR` (red) |
| Ready | `—` / `YES` (green) / `NO` (red) |

**Info box (light grey):**
- Net P&L / Cost ratio: `X.XXx / req: Y.XXx`
- Fee type: "Maker (N bps/side)" or "Taker (N bps/side)"
- Round-trip cost: `X bps (Y fees + Z slip)`
- Explanation sentence (dynamic, changes with state)

**Reset button** (top-right): clears filter history without restarting engine

#### Card: Last Signal Blocked

Three rows: Signal type · Z-Score · Time (`HH:MM:SS (Xm ago)`)

**Reason box (light grey):**

| State | Message | Colour |
|-------|---------|--------|
| Collecting | "Collecting data — X / Y ticks (Z%). Engine evaluates filters once lookback is full." | Yellow |
| In position | "In position — block tracking paused" | Blue |
| Idle | "Waiting for z to cross ±X (entry threshold)" | Grey |

#### Card: Active Orders

- Header badge: execution modes (e.g. `LIMIT / LIMIT`)
- When no orders: "No active orders" (centred, grey)
- When orders are live — two sub-columns:

| Sub-column | Fields |
|------------|--------|
| Leg A | Side badge · Price · Status badge |
| Leg B | Side badge · Price · Status badge |

Updated by `signal` event; order IDs from engine status.

#### Card: AI Insights

- Header badge: `X pending` (grey / yellow if > 0)
- Scrollable list (max 150 px):

| Element | Detail |
|---------|--------|
| Type badge | `FILTER` (blue) / `POSITION` (yellow) / `OBSERVE` (grey) |
| Timestamp | `MM-DD HH:MM`, opacity 40% |
| Rationale | Full text |
| Confidence | `conf X%` |
| Apply button | Shown for `FILTER_TOGGLE` type → `POST /api/ai-insights/<id>/apply` |
| Dismiss button | `✕` → `DELETE /api/ai-insights/<id>/dismiss` |

#### Card: Auto-Tune Log

- Header badges: Health score `X/100` (colour-coded) · Auto-Tune ON/OFF
- Scrollable list (max 140 px):

| Element | Detail |
|---------|--------|
| Bullet | Red = circuit breaker, orange = parameter change |
| Timestamp + trade ID | Opacity 45% |
| Change | `⚙ Auto-tuned: param  old → new` |
| Sub-line | `conf X%  ·  rationale`, opacity 55% |

#### Card: Recent Trades

Header buttons: **Download CSV** (green outline) · **Clear** (red outline) · **All** (grey outline, links to `/analysis`)

Table (scrollable, max 200 px):

| Column | Format |
|--------|--------|
| Time | `HH:MM:SS` |
| Type | `LONG` (green badge) / `SHORT` (red badge) |
| Z | `±X.XX` |
| Leg A fill | `$X.XX` |
| Leg B fill | `$X.XX` |
| P&L | `+$X.XX` green / `−$X.XX` red |

---

### Right Column (`col-lg-3`)

#### Card: Margin Details

**Row 1 — two metric boxes:**

| Box | Field |
|-----|-------|
| Equity | `$X.XX` |
| Available | `$X.XX` (with "why?" debug link → `GET /api/balance-debug`) |

**Key-value rows:**

| Label | Value |
|-------|-------|
| IMR | `$X.XX` |
| MMR | `$X.XX` |
| Margin Ratio | `0%` |
| Mark Price | `—` |
| Liq. Price | `—` (red) |

Footer note: "These populate once a trade is open."

#### Card: Position Margin *(shown only when in position)*

Small-font table:

| Leg | Value | Lev | Margin |
|-----|-------|-----|--------|
| Leg A | `$X.XX` | `1x` | `$X.XX` |
| Leg B | `$X.XX` | `3x` | `$X.XX` |
| **Total** | — | — | **`$X.XX`** |

Warning row if configured leverage ≠ exchange leverage: `⚠ Leg A: 1x (exchange) vs 2x (configured)`

#### Card: Config (live echo of active settings)

| Label | Value |
|-------|-------|
| Mode | PAPER / DEMO / LIVE badge |
| Asset | Asset badge |
| Size | `$X,XXX` total notional |
| Entry | `±X.X` |
| Exit | `±X.X` |
| Stop | `±X.X` |
| Leg A Lev | `Xx` |
| Leg B Lev | `Xx` |

#### Card: VIP Level

Centred badge — populated from account info.

#### Card: Reset Controls

Three stacked full-width buttons:

| Button | Colour | Action |
|--------|--------|--------|
| Reset Trades/SD | Secondary outline | `POST /api/reset-trades` |
| Reset Spread | Warning outline | Clears spread history only |
| Reset All | Danger outline | `POST /api/reset-all` |

---

### Bottom Section (full width)

#### Card: Exchange Order Log

Header buttons: **Download CSV** (green) · **Refresh** (grey) → `GET /api/exchange-orders`

Table (scrollable, max 280 px):

| Column |
|--------|
| Time |
| Symbol |
| Type (SPOT / SWAP) |
| Side (BUY / SELL) |
| Pos Side (long / short) |
| Order Type (limit / market / post_only) |
| Qty |
| Fill Qty |
| Fill Price |
| Leverage |
| Fee |
| P&L |
| Status |
| Order ID |

Default state: "Click Refresh to load exchange orders"

---

## Page 2 — Settings (`/settings`)

One long form. **Save** calls `POST /api/config`. All sections below are rendered as labelled card sections.

### Section: Pair Selection

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| Quick Pick | `<select>` | `—` | Fills both leg fields when asset chosen |
| Leg A symbol | Text + `<datalist>` | e.g. `ETH-USDT-SWAP` | Autocomplete from `/api/instruments` |
| Leg B symbol | Text + `<datalist>` | e.g. `BTC-USDT-SWAP` | Autocomplete from `/api/instruments` |
| Hedge Ratio (β) | Number `step=0.0001` | `1.0000` | Shows live suggestion from `/api/leg-prices`; "apply" link fills field |
| Pair label | Read-only | Auto-derived | Shows `BTC` / `ETH` etc. |
| Instrument status | Read-only | "Loading…" | `✓ Loaded (XXX spot + YYY futures)` after fetch |

### Section: Z-Score Thresholds

| Field | Type | Default | Range |
|-------|------|---------|-------|
| Entry Threshold | Number | `2.0` | 0.5 – 5 |
| Exit Threshold | Number | `1.0` | 0 – 3 |
| Stop Loss Threshold | Number | `4.0` | 2 – 10 |

### Section: ⚡ Fast-Exit Overrides

**Scale-invariant (preferred) sub-group:**

| Field | Type | Default | Range | Step |
|-------|------|---------|-------|------|
| Profit Target (σ-fraction) | Number | `0.65` | 0 – 2 | 0.05 |
| Max Hold (× half-life) | Number | `5.0` | 0 – 20 | 0.5 |
| Max Hold Z-Progress Gate | Number | `0.5` | 0 – 1 | 0.05 |
| Dollar Stop (% capital) | Number | `1.0` | 0 – 20 | 0.1 |
| Profit Cost Floor (× fees) | Number | `1.0` | 0 – 10 | 0.25 |

**Fixed-$ fallback sub-group** (labelled "Fixed-$ Fallback"):

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| Profit Target (USD) | Number | `0` | Exit when net P&L ≥ this |
| Max Hold (minutes) | Number | `0` | Exit after N min if profitable |
| Dollar Stop (USD) | Number | `0` | Exit when net P&L ≤ −this |

**Exit Signal Mode:**

| Option | Value | Description |
|--------|-------|-------------|
| Z-Score (original) | `zscore` | Exit when rolling Z returns to ±exit threshold |
| Absolute Spread | `spread` | Exit when live spread crosses entry-time mean |
| Hybrid | `hybrid` | Either condition fires exit |

### Section: Rolling Window

| Field | Type | Default | Range |
|-------|------|---------|-------|
| Lookback Period | Number | `100` | 20 – 100,000 |
| Stats Update Interval | `<select>` | `0` | Every tick / 1 min / 5 min / 10 min / 15 min / 30 min / 1 hr |

### Section: Signal Filters

| Field | Type | Default |
|-------|------|---------|
| Enable Hurst Filter | Checkbox | ✓ |
| Hurst Threshold | Number `0.05 step` | `0.5` |
| Enable Edge Filter | Checkbox | ✓ |
| Min Net P&L / Cost Ratio | Number `0.1 step` | `1.5` |
| Trend Direction Filter | Checkbox | ☐ |

### Section: Post-Entry Exit Overrides

**Hurst Regime Exit:**

| Field | Default |
|-------|---------|
| Enable | ☐ |
| H Threshold | `0.55` |
| Consecutive Ticks | `3` |

**Spread Velocity Exit:**

| Field | Default |
|-------|---------|
| Enable | ☐ |
| pts/min threshold | `2.0` |
| Consecutive ticks to confirm | `5` |
| Rolling window (ticks) | `20` |

### Section: Trailing Stop

| Field | Type | Default | Range | Step | Notes |
|-------|------|---------|-------|------|-------|
| Pullback % | Number | `0` | 0 – 100 | 5 | 0 = disabled |
| Floor % | Number | `0` | 0 – 100 | 5 | Arm when P&L ≥ X% of target |

### Section: Self-Learning (AI Auto-Tune)

| Element | Detail |
|---------|--------|
| Claude API Key | Password input + eye toggle + "Save Key" button + "Test Connection" button |
| API Key status badge | "Checking…" / "Configured ✓" / "Not set" |
| Enable Auto-Tune | Checkbox |
| View Learning Log | Button → `GET /api/learning-log` JSON |
| View Learnings | Button → `GET /api/learnings` JSON |

### Section: Position Sizing

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| Position Size (USD) | Number `step=100` | `1000` | Shows live "Capital required: $—" preview with tooltip |
| Max Position Size (USD) | Number `step=100` | `5000` | |
| M2M Buffer (%) | Number `step=5` | `10` | 0 – 100 |
| Leg A Leverage | `<select>` | `1x` | 1x / 2x / 3x / 5x / 10x / 15x / 20x / 25x / 30x / 40x / 50x |
| Leg B Leverage | `<select>` | `3x` | Same options |

### Section: Fee Estimates

| Field | Label | Default |
|-------|-------|---------|
| `spot_maker_fee_bps` | Leg A Maker (bps) | `8` |
| `spot_taker_fee_bps` | Leg A Taker (bps) | `10` |
| `futures_maker_fee_bps` | Leg B Maker (bps) | `2` |
| `futures_taker_fee_bps` | Leg B Taker (bps) | `5` |
| `slippage_bps` | Slippage per Leg (bps) | `0.5` |

Info alert dynamically shows live round-trip cost as settings change.

### Section: Trading Mode

| Field | Type | Default |
|-------|------|---------|
| Paper Trading Mode | Checkbox | ✓ |

When paper = false: red alert with 7-item pre-flight checklist is shown inline.

### Section: Order Execution

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| Entry Mode | `<select>` | `LIMIT` | Market / Limit (Maker) |
| Exit Mode | `<select>` | `LIMIT` | Market / Limit (Maker) |
| Limit Timeout (sec) | Number | `30` | 5 – 300; shown only when LIMIT selected |
| Price Offset (bps) | Number | `1` | 0 – 10; shown only when LIMIT selected |

### Section: Order Slicing (TWAP)

| Field | Type | Default | Range |
|-------|------|---------|-------|
| Entry Slices | Number | `1` | 1 – 10 |
| Slice Interval (sec) | Number `step=0.5` | `2.0` | 0 – 60 |
| Min Fill Ratio | Number `step=0.01` | `0.95` | 0.5 – 1.0 |

### Section: Safety Settings

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| Entry Cooldown (seconds) | Number | `0` | 0 – 3600 |
| Verify Exchange Position Before Entry | Checkbox | ✓ | |
| Orphan Leg Recovery Timeout (seconds) | Number | `60` | 10 – 300 |
| Daily Loss Limit (USD) | Number `step=10` | `0` | 0 = disabled |

### Section: Telegram Notifications

| Field | Type | Default |
|-------|------|---------|
| Enable Telegram | Checkbox | ☐ |
| Bot Token | Password | `""` |
| Chat ID | Text | `""` |
| Notify on Trade Entry/Exit | Checkbox | ✓ |
| Notify on Trading Signals | Checkbox | ☐ |
| Notify on System Errors | Checkbox | ✓ |

Bot commands info alert: `/status`, `/positions`, `/trades`.

### Form Buttons (bottom)

| Button | Style | Action |
|--------|-------|--------|
| Reset | Secondary outline | Reloads page |
| Save Configuration | Green primary | `POST /api/config` |

---

## Page 3 — Exchanges (`/setup`)

### Left Column: Add Exchange

Card: "Add Exchange"

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| Name | Text | ✓ | e.g. "My OKX Account" |
| Exchange | `<select>` | ✓ | OKX / Binance / Bybit |
| API Key | Text | ✓ | |
| Secret Key | Password | ✓ | |
| Passphrase | Password | OKX only | Field hidden for non-OKX exchanges |
| Testnet / Demo Mode | Checkbox | — | Checked by default |

Submit: `POST /api/exchanges` — green primary button.

### Right Column: Connected Exchanges

Card: "Connected Exchanges"

Table columns: Name · Type badge · Mode badge (Testnet yellow / Live green) · Status badge (CONNECTED green / ERROR red) · Actions

Actions per row:
- **Test Connection** (plug icon) → `POST /api/exchanges/<id>/test`
- **Delete** (trash icon, red) → `DELETE /api/exchanges/<id>`

### Bottom: Active Exchange Selection

Card: "Active Exchange Selection"

| Field | Type | Notes |
|-------|------|-------|
| Leg A Exchange | `<select>` | Lists all configured exchanges |
| Leg B Exchange | `<select>` | Lists all configured exchanges |

Save: `POST /api/set-active-exchanges` — green primary, right-aligned.

---

## Page 4 — Analysis (`/analysis`)

### Summary Stats Row (6 cards)

| Card | Value | Colour |
|------|-------|--------|
| Total Trades | Integer | — |
| Winning Trades | Integer | Green |
| Losing Trades | Integer | Red |
| Win Rate | `X%` | — |
| Total P&L | `$X.XX` | Green ≥ 0, red < 0 |
| Avg P&L | `$X.XX` | Green ≥ 0, red < 0 |

### Card: SD Touch Distribution

- Chart type: Bar (Chart.js)
- X-axis: SD Levels (−3, −2, −1, +1, +2, +3)
- Y-axis: Count
- Green bars: positive levels; red bars: negative levels
- Asset filter dropdown (top-right)

### Card: Recent SD Touches

Scrollable table (max 400 px):

| Column | Format |
|--------|--------|
| Time | ISO truncated to seconds |
| SD Level | Badge — green if positive, red if negative |
| Direction | ↑ green / ↓ red |
| Z-Score | 4 dp |
| Spread | 6 dp |

Default: "No SD touch events"

### Card: Trade Journal

Scrollable table (up to 500 rows):

| Column | Format |
|--------|--------|
| Entry Time | `YYYY-MM-DD HH:MM:SS` |
| Exit Time | `YYYY-MM-DD HH:MM:SS` |
| Asset | Text |
| Type | `LONG` green / `SHORT` red badge |
| Entry Z | `±X.XX` |
| Exit Z | `±X.XX` |
| Entry Spot | `$X.XX` |
| Entry Futures | `$X.XX` |
| Exit Spot | `$X.XX` |
| Exit Futures | `$X.XX` |
| Notional | `$X,XXX` |
| P&L | `+$X.XX` green / `−$X.XX` red |
| P&L % | Return on notional |
| Cap % | Return on locked capital |
| Exit Reason | Badge (EXIT_SIGNAL / STOP_LOSS / MANUAL / MAX_HOLD / TRAILING_STOP / DOLLAR_STOP / etc.) |

---

## Real-Time Socket.IO Events

All events on namespace `/`. Browser connects on page load; reconnects automatically.

### Event: `tick`

Emitted every market tick (~0.5 s). Updates prices, β, margin preview.

```json
{
  "spot":    { "bid": 1586.10, "ask": 1586.50, "last": 1586.30, "mid": 1586.30 },
  "futures": { "bid": 59800.00, "ask": 59801.00, "last": 59800.50, "mid": 59800.50 },
  "timestamp": "2026-06-24T16:57:33Z"
}
```

### Event: `signal`

Emitted every tick. Updates z-score chart, spread chart, statistics, filters, status banner.

```json
{
  "signal_type":        "NONE|LONG|SHORT|EXIT|STOP_LOSS",
  "zscore":             2.4939,
  "spread":             -347.07,
  "spread_mean":        0.00,
  "spread_std":         85.23,
  "hurst":              0.47,
  "half_life":          42,
  "regime":             "MEAN_REVERTING",
  "current_position":   "LONG",
  "data_points":        100,
  "lookback":           100,
  "data_ready":         true,
  "hurst_ok":           true,
  "std_filter_ok":      true,
  "std_ratio":          2.51,
  "std_ratio_required": 1.50,
  "round_trip_cost_bps":5.6,
  "round_trip_fees_bps":4.6,
  "round_trip_slippage_bps": 1.0,
  "hedge_ratio":        37.43,
  "beta_x_spot":        59346.0,
  "entry_threshold":    2.0,
  "last_blocked_signal": null,
  "cost_breakdown": {
    "entry_spot_bps": 0.8, "entry_fut_bps": 0.8,
    "exit_spot_bps":  2.7, "exit_fut_bps":  2.7,
    "entry_mode": "LIMIT", "exit_mode": "LIMIT"
  }
}
```

### Event: `trade`

Emitted on open and close. Updates position panel, recent trades table.

```json
{
  "id":                  17,
  "asset":               "BTC",
  "position_type":       "LONG",
  "entry_time":          "2026-06-24T16:57:36Z",
  "exit_time":           "2026-06-24T17:20:48Z",
  "entry_spot_price":    1585.85,
  "entry_futures_price": 59779.00,
  "exit_spot_price":     1572.27,
  "exit_futures_price":  59444.45,
  "entry_spread":        425.81,
  "entry_zscore":        4.35,
  "exit_zscore":         2.56,
  "quantity":            0.033681,
  "notional_usd":        2011.43,
  "margin_usd":          260.82,
  "pnl_usd":             -9.18,
  "pnl_percent":         -0.23,
  "pnl_pct_on_capital":  -3.52,
  "fees_usd":            1.40,
  "is_open":             false,
  "is_paper":            false,
  "exit_reason":         "STOP_LOSS",
  "unrealized_pnl":      null,
  "exit_target_usd":     4.95,
  "exit_stop_usd":       5.22,
  "max_hold_minutes":    10.0
}
```

### Event: `error`

```json
{ "message": "Error description" }
```

Shows toast notification in browser.

### Event: `status`

Emitted on engine state change. Updates algo toggle, connection status.

```json
{ "algo_enabled": true, "is_running": true }
```

### Event: `trade_analysis`

Emitted after AI analyses a closed trade.

```json
{
  "timestamp":    "2026-06-24T17:20:49Z",
  "trade_id":     17,
  "analysis": {
    "health_score":      72,
    "confidence_score":  7.2,
    "summary":           "Entry at extreme Z was valid; exit was forced by stop-loss after spread continued trending."
  }
}
```

Updates health score badge on auto-tune card.

### Event: `auto_tune`

Emitted when AI changes a parameter.

```json
{
  "timestamp":       "2026-06-24T17:20:49Z",
  "action_type":     "CHANGE",
  "param":           "profit_target_usd",
  "old_value":       4.95,
  "new_value":       2.50,
  "avg_confidence":  0.81,
  "rationale":       "Trades consistently failing to reach full target; capture partial move instead."
}
```

### Event: `ai_insight`

Emitted when AI surfaces a non-parameter suggestion.

```json
{
  "timestamp":    "2026-06-24T17:20:49Z",
  "insight_type": "FILTER_TOGGLE",
  "param":        "hurst_enabled",
  "confidence":   0.78,
  "rationale":    "Hurst is blocking valid entries in the current trending micro-regime."
}
```

---

## HTTP API Reference

Base URL: same origin. All responses JSON except CSV downloads.

### Configuration

| Route | Method | Body / Params | Response |
|-------|--------|---------------|----------|
| `/api/config` | GET | — | Full config object |
| `/api/config` | POST | Config object | `{success, config}` |

### Instruments & Pricing

| Route | Method | Params | Response |
|-------|--------|--------|----------|
| `/api/instruments` | GET | `?refresh=1` | `{spot: [...], futures: [...]}` |
| `/api/leg-prices` | GET | `?leg_a=...&leg_b=...` | `{leg_a_price, leg_b_price, suggested_beta}` |

### Account

| Route | Method | Response |
|-------|--------|----------|
| `/api/account-info` | GET | Balance, margin, leverage, positions, VIP level |
| `/api/balance-debug` | GET | `{trading, funding, cross_account_valuation, diagnosis}` |

### Engine Control

| Route | Method | Body | Response |
|-------|--------|------|----------|
| `/api/engine/status` | GET | — | Full engine status (see In-Position Card fields) |
| `/api/engine/toggle-algo` | POST | `{enabled: bool}` | `{success, algo_enabled}` |
| `/api/engine/reset` | POST | — | `{success}` |
| `/api/engine/set-demo-mode` | POST | `{demo: bool}` | `{success, demo}` |
| `/api/engine/sync-position` | POST | `{action: "recover"\|"clear"}` | `{success, action, previous_position, current_position}` |
| `/api/engine/close-position` | POST | — | `{success, trade, message}` |

### Exchange Positions & Orders

| Route | Method | Params / Body | Response |
|-------|--------|---------------|----------|
| `/api/exchange-positions` | GET | — | `{positions, dust_positions, mismatch, mismatch_reason}` |
| `/api/exchange-orders` | GET | `?limit=50` | `{orders: [...]}` |
| `/api/exchange-orders/csv` | GET | `?limit=100` | CSV file download |
| `/api/close-exchange-position` | POST | `{symbol}` | `{success, order_id}` |
| `/api/close-orphaned-spot` | POST | `{currency}` | `{success, amount_sold, order_id}` |
| `/api/sweep-dust` | POST | — | `{success, swept, locked, failed, results}` |

### Trades & Analysis

| Route | Method | Params / Body | Response |
|-------|--------|---------------|----------|
| `/api/trades` | GET | `?limit=100` | Array of trade objects |
| `/api/trades/csv` | GET | — | CSV file download |
| `/api/trade-journal` | GET | — | `{trades, statistics}` |
| `/api/trades/clear` | POST | `{asset?}` | `{success, deleted}` |
| `/api/trades/<id>` | DELETE | — | `{success}` |

### SD Touches & Spread History

| Route | Method | Notes |
|-------|--------|-------|
| `/api/sd-touches` | GET | `?asset=...&limit=500` |
| `/api/sd-touches/clear` | POST | |
| `/api/spread-history` | GET | `?n=100` — returns `{spreads, zscores}` |
| `/api/spread-history/clear` | POST | |

### Exchange Management

| Route | Method | Body | Response |
|-------|--------|------|----------|
| `/api/exchanges` | GET | — | Array of exchange records |
| `/api/exchanges` | POST | `{name, type, api_key, secret, passphrase?, testnet}` | `{success, exchange}` |
| `/api/exchanges/<id>` | DELETE | — | `{success}` |
| `/api/exchanges/<id>/test` | POST | — | `{success, account, error?}` |
| `/api/set-active-exchanges` | POST | `{spot_id?, futures_id?}` | `{success}` |

### AI / Claude Integration

| Route | Method | Notes |
|-------|--------|-------|
| `/api/anthropic/status` | GET | `{configured, preview}` |
| `/api/anthropic/key` | POST | Body: `{key}` — saves to `.env` |
| `/api/anthropic/test` | POST | `{success, model}` |
| `/api/learnings` | GET | `?limit=20` |
| `/api/learning-log` | GET | `?limit=50` |
| `/api/ai-insights` | GET | `?status=pending&limit=30` |
| `/api/ai-insights/<id>/apply` | POST | Applies `FILTER_TOGGLE` insight to live config |
| `/api/ai-insights/<id>/dismiss` | DELETE | Marks insight dismissed |
| `/api/ai-monitor/status` | GET | Current AI monitor health verdict |

### Telegram

| Route | Method | Body | Response |
|-------|--------|------|----------|
| `/api/telegram/test` | POST | — | `{success, message}` |
| `/api/telegram/config` | POST | `{enabled, token, chat_id, notify_trades, notify_signals, notify_errors}` | `{success}` |

### Diagnostics & Resets

| Route | Method | Notes |
|-------|--------|-------|
| `/api/key-env-probe` | POST | Tests API key against both OKX Live and Demo environments |
| `/api/reset-trades` | POST | Clears trades only |
| `/api/reset-all` | POST | Full reset: trades + SD touches + spread history |

---

## Key JSON Object Shapes

### Config Object (all fields)

```json
{
  "asset": "BTC",
  "spot_symbol": "ETH-USDT-SWAP",
  "futures_symbol": "BTC-USDT-SWAP",
  "hedge_ratio": 37.43,
  "entry_threshold": 2.0,
  "exit_threshold": 0.5,
  "stop_loss_threshold": 4.0,
  "position_size_usd": 2000,
  "max_position_size_usd": 10000,
  "spot_leverage": 20,
  "futures_leverage": 20,
  "m2m_buffer_pct": 10,
  "lookback_period": 100,
  "stats_update_interval": 0,
  "hurst_enabled": true,
  "hurst_threshold": 0.5,
  "std_filter_enabled": true,
  "min_std_multiple": 1.5,
  "trend_direction_filter": false,
  "exit_signal_mode": "zscore",
  "profit_target_sigma_frac": 0.65,
  "profit_target_usd": 0,
  "profit_target_min_cost_mult": 1.0,
  "max_hold_halflife_mult": 5.0,
  "max_hold_minutes": 0,
  "max_hold_z_progress_min": 0.5,
  "stop_loss_capital_pct": 1.0,
  "max_loss_usd": 0,
  "trailing_stop_pct": 0,
  "trailing_stop_floor_pct": 0,
  "hurst_exit_enabled": false,
  "hurst_exit_threshold": 0.55,
  "hurst_exit_n_ticks": 3,
  "velocity_exit_enabled": false,
  "velocity_exit_pts_per_min": 2.0,
  "velocity_exit_n_ticks": 5,
  "velocity_exit_window_ticks": 20,
  "entry_execution_mode": "LIMIT",
  "exit_execution_mode": "LIMIT",
  "limit_order_timeout_sec": 30,
  "limit_order_price_offset_bps": 1.0,
  "entry_slices": 1,
  "entry_slice_interval_sec": 2.0,
  "min_fill_ratio": 0.95,
  "spot_maker_fee_bps": 0.8,
  "spot_taker_fee_bps": 2.7,
  "futures_maker_fee_bps": 0.8,
  "futures_taker_fee_bps": 2.7,
  "slippage_bps": 0.5,
  "paper_trading": false,
  "algo_enabled": false,
  "entry_cooldown_seconds": 60,
  "verify_exchange_position": true,
  "orphan_recovery_timeout_sec": 60,
  "daily_max_loss_usd": 0,
  "telegram_enabled": false,
  "telegram_bot_token": "",
  "telegram_chat_id": "",
  "telegram_notify_trades": true,
  "telegram_notify_signals": false,
  "telegram_notify_errors": true,
  "auto_tune_enabled": false,
  "rfq_notional_threshold_usd": 0
}
```

### Trade Object (all fields)

```json
{
  "id": 17,
  "asset": "BTC",
  "position_type": "LONG",
  "entry_time": "2026-06-24T16:57:36Z",
  "exit_time": "2026-06-24T17:20:48Z",
  "entry_spot_price": 1585.85,
  "entry_futures_price": 59779.00,
  "exit_spot_price": 1572.27,
  "exit_futures_price": 59444.45,
  "entry_spread": 425.81,
  "exit_spread": -278.33,
  "entry_zscore": 4.35,
  "exit_zscore": 2.56,
  "entry_spread_mean": -122.45,
  "entry_spread_std": 125.30,
  "quantity": 0.033681,
  "notional_usd": 2011.43,
  "margin_usd": 260.82,
  "capital_locked_usd": 286.90,
  "pnl_usd": -9.18,
  "pnl_gross_usd": -7.78,
  "pnl_percent": -0.23,
  "pnl_pct_on_capital": -3.52,
  "fees_usd": 1.40,
  "entry_fees_usd": 0.62,
  "exit_fees_usd": 0.78,
  "is_open": false,
  "is_paper": false,
  "exit_reason": "STOP_LOSS",
  "spot_order_id": "3684875705401008128",
  "futures_order_id": "3684875725131014146",
  "entry_placed_at": "2026-06-24T16:57:33Z",
  "entry_filled_at": "2026-06-24T16:57:36Z",
  "entry_latency_ms": 3140,
  "unrealized_pnl": null
}
```

### Engine Status Object (from `/api/engine/status`)

```json
{
  "is_running": true,
  "algo_enabled": true,
  "paper_trading": false,
  "asset": "BTC",
  "position": "LONG",
  "spot_connected": true,
  "futures_connected": true,
  "executing_trade": false,
  "sl_cooldown_remaining": 0,
  "sl_cooldown_sec": 300,
  "daily_loss_usd": -9.18,
  "daily_loss_limit": 0,
  "entry_execution_mode": "LIMIT",
  "exit_execution_mode": "LIMIT",
  "execution_backend": "WS",
  "position_mismatch": null,
  "open_trade": { /* Trade object with unrealized_pnl populated */ },
  "spot_tick": { "bid": 1572.0, "ask": 1572.5, "mid": 1572.25 },
  "futures_tick": { "bid": 59440.0, "ask": 59445.0, "mid": 59442.5 },
  "signal": { /* Signal object — all fields from signal event */ }
}
```
