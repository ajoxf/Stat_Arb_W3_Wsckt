# 13 — OKX RFQ / Block Trading: Feasibility, Validation & Integration Design

Status: **implemented & reviewed.** An RFQ implementation already existed
(`adapters/okx_rfq_adapter.py`, `core/rfq_executor.py`, wired in `app.py`,
gated by `rfq_notional_threshold_usd`). A correctness review found a showstopper
and several gaps — all fixed (see **§6**). RFQ remains **disabled** by default
(`rfq_notional_threshold_usd = 0`); do the demo validation in §3 before enabling.

---

## 0. TL;DR

- **Atomic two-leg execution is real and accessible.** OKX Block Trading / RFQ
  executes a multi-leg structure (incl. two USDT perps) all-or-none on a single
  maker quote. Group (multi-leg) RFQs **cannot be partially executed** — both
  legs fill or neither does. This deletes the entire leg-risk / orphan failure
  class by construction.
- **Minimums are low, not six-figure.** Liquid Marketplace min notional ≈ **$1,000**;
  account must hold ≈ **$10,000** in assets to use block trading. We're well above
  both at $100k/leg. (My earlier "needs six figures" claim was wrong.)
- **It is NOT cheaper — it trades cost for safety.** You execute as the *taker*
  on the maker's quote, so you pay taker fees on *both* open and close, **plus**
  the maker's embedded spread. That is materially more than the all-maker CLOB
  path we just hardened. RFQ wins only if the residual leg-risk cost on the fixed
  CLOB exceeds the RFQ premium, or if you're paying for tail-risk elimination.
- **Recommendation: validate before building.** Pull real demo quotes, measure
  the all-in cost and latency against our ~$150–300/trade edge, and measure the
  *residual* orphan rate on the now-fixed CLOB at the new size. Decide with data.
- **Target architecture: hybrid.** RFQ for planned entries/exits (atomic);
  CLOB market order (the path hardened in this session) as the emergency-stop
  fallback, since RFQ's request→quote→execute cycle is too slow for a blowout.

---

## 1. API reference (what we'd build against)

All endpoints are private (`/api/v5/rfq/...`) and use the **standard OKX v5 auth**
we already implement in `adapters/okx_adapter.py` (`OK-ACCESS-KEY`, HMAC sign,
timestamp, passphrase). No new auth scheme. Requirements:

- API key with **Trade** permission.
- Account eligible for block trading (≈ $10k assets; region/KYC dependent).
- Works in **demo trading** (`x-simulated-trading: 1`) — quotes live 60s in demo
  vs 2min in production. This is how we validate without real funds.

### REST (taker role — that's us)

| Purpose | Method / Path | Key params |
|---|---|---|
| List makers | `GET /api/v5/rfq/counterparties` | — |
| Create RFQ | `POST /api/v5/rfq/create-rfq` | `counterparties[]`, `anonymous`, `clRfqId`, `allowPartialExecution`, `legs[]` |
| Cancel RFQ | `POST /api/v5/rfq/cancel-rfq` | `rfqId` / `clRfqId` |
| Execute quote | `POST /api/v5/rfq/execute-quote` | `rfqId`, `quoteId`, `legs[]` |
| Poll RFQs | `GET /api/v5/rfq/rfqs` | `rfqId`, `state`, ... |
| Poll quotes | `GET /api/v5/rfq/quotes` | `rfqId`, `quoteId`, `state`, ... |

`create-rfq` `legs[]` element (per leg):

```jsonc
{
  "instId": "ETH-USDT-SWAP",   // each leg is a normal instrument
  "sz":     "22",              // SIZE IN CONTRACTS for SWAP (same units as place_order's sz)
  "side":   "sell",            // this leg's direction
  "posSide":"short"            // hedge-mode position side (we run long_short_mode)
}
```

A short-spread entry = `[{ETH-SWAP, sell, short}, {BTC-SWAP, buy, long}]`; the exit
is the same two legs with sides/posSide reversed. `execute-quote` echoes `legs[]`
with the full sizes — for a group RFQ you **must** pass the full leg size or you
get error `70507` (no partial execution). That error is a feature for us: it is
the atomicity guarantee.

### WebSocket (private channels)

- `rfqs` — status of our RFQs (active / canceled / filled / expired)
- `quotes` — incoming maker quotes (this is the signal to execute)
- `struc-block-trades` — confirmation of executed block trades (our fills)

We already run a private WS (`adapters/okx_ws_adapter.py`); these channels would
be added there (subscribe alongside orders/positions/account).

### Timing

- An RFQ stays **active 2 minutes** then expires; maker quotes also live ~2 min
  (60s in demo). So once a usable quote arrives we have a wide window to execute,
  but in practice we execute within ~1 quote-update.
- **Latency profile:** create-rfq → first quotes is *seconds* (maker reaction +
  network), not the sub-100ms of a CLOB order. This is the defining constraint.

---

## 2. Economics — the honest number

At **$100k/leg ($200k/structure)**:

| Path | Exchange fee (round trip) | + Maker markup | Notes |
|---|---|---|---|
| **CLOB all-maker** (now achievable post-fix) | ~2bp × $400k = **~$80** | none (we *are* the maker) | what we run today |
| **RFQ / Block** | taker ~5bp × $400k = **~$200** | **unknown, must measure** (e.g. 1–3bp = $40–120) | taker both legs, both sides |

So RFQ likely costs **~$240–320 round trip vs ~$80** on the hardened CLOB — a
**~$160–240 atomicity tax per round trip**. Our per-trade edge at this size is
~$150–300. **RFQ can therefore eat most or all of the edge.** This is the make-or-
break number and is exactly what the validation measures. (Exact block-trade fee
for our VIP tier must be confirmed — some venues rebate the quoting maker and
charge the executing taker; assume taker-side until confirmed.)

### When RFQ is still the right call

1. **Residual leg-risk cost on the fixed CLOB > the RFQ tax.** Measure it (§3b).
2. **Tail-risk elimination.** One orphaned $100k leg in a fast market is a
   four-figure loss. Even if *rare*, RFQ caps that tail at exactly zero. A
   risk-averse operator may pay the tax purely to remove the fat tail — a
   legitimate preference EV alone doesn't capture.

---

## 3. Validation test — DO THIS BEFORE BUILDING

Cheap, ~1 day, mostly in demo. Two parallel measurements:

### 3a. RFQ quote quality (demo first, then 1–2 tiny live RFQs)

For ~20 RFQs at representative size, spread across calm and volatile minutes:

1. `create-rfq` for the 2-leg structure.
2. Record **time-to-first-usable-quote** (t_quote).
3. Record the quote's **all-in structure price vs the CLOB mid at request time**
   → that delta in $ is the **maker markup** (the real cost).
4. Record **# of quotes received** and **# of RFQs that got zero quotes**
   (liquidity reliability for a crypto-vs-crypto perp structure).
5. Execute a few in demo to confirm the atomic fill + `struc-block-trades` event.

**Pass criteria (all must hold):**
- Markup + taker fee (all-in round trip) **< ~50% of edge** (leaves net profit).
- t_quote acceptable vs hold time (target ≤ ~3–5s).
- Zero-quote rate low (e.g. < 10%) — else we can't rely on it for exits.

### 3b. Residual leg-risk on the FIXED CLOB (just run it)

Run the current hardened CLOB (POST_ONLY 3bp buffer + reduce_only orphan guard)
at the new size and log: `cancelSource=31` rate, orphan events, and $ cost of
each. This is the baseline RFQ must beat. If orphans are now rare and cheap, the
RFQ tax may not be worth it (outside tail-risk).

**Decision:** build RFQ if `3b cost (incl. tail aversion) > 3a all-in tax`.

---

## 4. Integration design (if validation passes)

### 4a. New adapter

`adapters/okx_rfq_adapter.py` — a thin client reusing `OKXAdapter`'s signing.
Methods: `get_counterparties()`, `create_rfq(legs)`, `cancel_rfq(rfq_id)`,
`get_quotes(rfq_id)`, `execute_quote(rfq_id, quote_id, legs)`. Subscribe to
`rfqs`/`quotes`/`struc-block-trades` on the existing private WS.

### 4b. Execution state machine (replaces leg-by-leg for planned trades)

```
BUILD_LEGS ─▶ CREATE_RFQ ─▶ AWAIT_QUOTES ─(quote ≤ price_limit)─▶ EXECUTE_QUOTE ─▶ CONFIRM(struc-block-trade)
                  │                │                                      │
                  │           (timeout/no quote)                    (reject/expire)
                  ▼                ▼                                      ▼
              FALLBACK ◀───────────┴──────────────────────────────────────┘
   FALLBACK = current CLOB path (entry: maker-peg loop; exit/stop: MARKET reduce_only)
```

Key points:
- A **price limit** gates EXECUTE: only accept a quote whose all-in spread price
  is within `rfq_max_markup_bps` of our model mid. Otherwise cancel + fall back.
  This protects the edge trade-by-trade.
- **No partial handling needed** — group RFQ is all-or-none, so the entire
  orphan-recovery machinery (`_handle_leg_risk`, maker recovery, the reduce_only
  flatten) is bypassed on the RFQ happy path.
- `clRfqId` carries our trade id for reconciliation, same pattern as `clOrdId`.

### 4c. Where it slots into the engine

`OrderExecutor` gains an `execute_spread_via_rfq(spread_order)` alongside the
existing LIMIT/MARKET paths. `TradingEngine` picks the path by config + context:
- planned entry / target exit → RFQ (fall back to CLOB on timeout/bad quote)
- **emergency stop / daily-loss / orphan cleanup → always CLOB MARKET** (RFQ too
  slow to trust when the spread is blowing out)

### 4d. Config additions

| Field | Meaning |
|---|---|
| `execution_venue` | `CLOB` \| `RFQ` \| `HYBRID` |
| `rfq_max_markup_bps` | reject quotes worse than this vs mid (edge guard) |
| `rfq_quote_wait_sec` | how long to await a usable quote before CLOB fallback |
| `rfq_min_notional_usd` | only route to RFQ above this size |

### 4e. Edge cases / risks

- **No quotes / illiquid structure** → timeout → CLOB fallback. Must alert if
  this happens often (RFQ unreliable for this pair).
- **Quote worse than limit** → cancel + fallback; log the markup.
- **Stop while an RFQ is in flight** → cancel RFQ, fire CLOB market immediately.
- **Demo ≠ prod liquidity** — demo proves the *mechanics*; real markup/fill
  reliability must be confirmed with small live RFQs before full cutover.

---

## 5. Recommendation & phasing

1. **Now:** keep trading on the hardened CLOB (this session's fixes). Measure 3b.
2. **Next (~1 day):** run the §3a demo validation. Get the markup/latency numbers.
3. **Decide** with 3a vs 3b. If RFQ's net edge survives (or tail-aversion wins),
   build §4 behind `execution_venue=HYBRID` and A/B it against CLOB.
4. **Cutover** to RFQ for planned trades only once live small-RFQ data confirms
   demo; keep CLOB MARKET as the permanent emergency-exit path.

Bottom line: atomic execution is the correct *direction* for scale and removes
our worst failure mode — but it's a strategy-cadence + cost change, not a free
win. Validate the two numbers above and the build pays for itself or it doesn't,
with no guesswork.

---

## 6. Implementation review & fixes applied

The existing code was reviewed end-to-end (adapter → executor → OrderExecutor
routing → engine exit logic → config). Findings and fixes:

| # | Severity | Issue | Fix |
|---|---|---|---|
| 1 | 🔴 Showstopper | RFQ leg `sz` was sent in **base units**, but OKX RFQ wants **contracts** (~10× off for ETH, ~100× for BTC) → wrong trade size | `OrderExecutor._rfq_contracts()` converts base→contracts via ctVal (mirrors `place_order`) and passes contract sizes; falls back to the order book if it can't size |
| 2 | 🟠 Safety | Stop exits routed *through* RFQ with up to a 10s quote wait | Engine flags urgent exits (anything not `EXIT`/`PROFIT_TARGET`/`MAX_HOLD`) and passes `allow_rfq=False` → stops always leg in on the order book |
| 3 | 🟠 Safety | No markup ceiling — executed the least-bad quote however wide | `rfq_max_markup_bps` (default 5) — reject + fall back if the best quote's worst-leg markup exceeds it |
| 4 | 🟡 Robustness | No HTTP timeout; an `execute-quote` hang left ambiguous state | 15s timeout on all RFQ calls; an execute timeout returns `ambiguous=True` → caller does **not** fall back (no double-execution), engine reconciles |
| 5 | 🟡 Robustness | A quote missing a leg read as 0-cost and could execute at px=0 | `_select_best_quote` discards quotes that don't price both legs (returns `None` → fall back) |
| 6 | 🟠 Blocker | RFQ config wasn't in `load_config`/`save_config`/schema → couldn't be enabled persistently | Added schema migration + load/save for all `rfq_*` fields (verified by round-trip test) |

**Still required before enabling (not code — operational):**
1. Demo validation per §3a — one RFQ at a known size; confirm the **fill size in
   contracts matches intent** (this is the live check for fix #1) and measure
   markup vs edge.
2. Calibrate `rfq_max_markup_bps` from observed demo markups (above typical,
   below edge).
3. Confirm OKX leg fields (`tdMode`/`posSide`) against the current spec in demo.
4. Set `rfq_notional_threshold_usd` high so only large clips route to RFQ.
