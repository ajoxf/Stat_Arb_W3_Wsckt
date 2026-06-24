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
