# OKX Support / Technical Liaison — Questions

**Account:** [UID / sub-account] · **Current tier:** VIP4 (90-day trial)
**Strategy:** Automated statistical-arbitrage (delta-neutral spread) on USDT perpetual
swaps — primarily BTC-USDT-SWAP vs ETH-USDT-SWAP. Two-leg execution, predominantly
POST_ONLY (maker) limit orders with cancel/replace re-quoting to track top-of-book,
with a MARKET fallback. Order placement and fills via both REST (v5) and the private
WebSocket order channel.
**Direction:** Scaling toward ~$100K notional per leg; evaluating the Market Maker
program.

We have a technical contact on your side willing to support — the questions below are
grouped by priority and written so your engineering team can answer precisely.

---

## Priority 1 — Order-flow throttle (`cancelSource = 31`)

This is our single biggest operational blocker. Under cancel/replace re-quoting, our
POST_ONLY orders are being auto-cancelled on submission with `cancelSource=31`. When it
hits one leg of a pair, we cancel the other leg to avoid an orphan — so it kills whole
round-trips.

1. What exactly does `cancelSource=31` represent? Please confirm it is an order-flow /
   cancel-rate throttle and share the precise rule.
2. What is the **cancel-to-fill ratio** (or order-to-trade ratio) limit at **VIP4**, and
   over what **rolling window** is it measured?
3. Is the limit **account-wide, per-sub-account, per-instrument, or per-API-key**?
4. When it triggers, what is the **penalty/cooldown duration**, and how does the counter
   decay back to normal?
5. Do **POST_ONLY** (maker) cancels count the same as taker cancels? Do **amend**
   operations count as cancel+new for this ratio?
6. What are the **recommended remedies** — minimum order resting time, amend-vs-cancel,
   max re-quote frequency — to stay under the threshold?
7. Does the limit increase with **VIP tier** and/or **Market Maker** status? What
   specific level would we reach at VIP5/MM?

## Priority 2 — Market Maker program & VIP tier economics

The throttle and our fee load both point here. We want to understand the path and the
benefits.

8. Please confirm our **current VIP4 maker/taker fees** for USDT perpetual swaps, and the
   **30-day volume (and/or asset balance)** required to **retain VIP4** after the trial.
9. What are the **eligibility requirements** for the Market Maker program (volume, maker
   ratio, quoting obligations, spread/uptime commitments)?
10. What does the MM program provide that's relevant to us:
    - **Maker rebates** (negative maker fees)? At what tiers/levels?
    - **Higher rate limits** and a **higher/!waived cancel-ratio threshold**?
    - **Dedicated technical support / colocation / lower-latency endpoints**?
11. Is there a **fee/limit bridge** during ramp-up so the cancel throttle doesn't block us
    from generating the very volume needed to qualify?

## Priority 3 — Rate limits (REST & WebSocket)

12. Current **order placement / cancel / amend** rate limits at VIP4 for (a) REST v5 and
    (b) the WS order channel — per-endpoint and any aggregate caps.
13. Do **WS order operations** and **REST orders** share the same limit bucket, or are they
    independent?
14. How do these rate limits relate to the `cancelSource=31` cancel-ratio throttle — are
    they separate systems?
15. Best-practice **batch order** endpoints for placing/cancelling a two-leg pair in one
    request (and whether batching helps with both rate limits and atomicity).

## Priority 4 — Multi-leg / pair execution (legging risk)

Our core risk is one leg filling while the other doesn't.

16. Does OKX offer any **atomic multi-leg / spread** execution we could use for a
    BTC-SWAP vs ETH-SWAP delta-neutral pair (e.g., block trading, spread/combo products,
    or an RFQ workflow)?
17. If not atomic, what is the **recommended pattern** to minimise legging risk for a
    synchronized pair (IOC vs POST_ONLY, order sequencing, hedge-on-fill via WS)?
18. Is **Market Maker Protection (MMP)** available/relevant to us, and how is it
    configured?

## Priority 5 — Margin & capital efficiency (scaling to $100K/leg)

19. For a **hedged BTC/ETH perp pair**, does **Portfolio Margin** mode net the two legs and
    materially reduce initial/maintenance margin vs cross/isolated? What are the
    eligibility requirements?
20. Recommended **margin mode** (cross vs isolated vs portfolio) for a continuously
    delta-neutral book.
21. At ~$100K notional per leg on BTC-USDT-SWAP / ETH-USDT-SWAP, any **position tiers /
    maintenance-margin steps** or **position limits** we should plan around?

## Priority 6 — Fill reporting & data accuracy

22. Please confirm field semantics on order responses: is **`avgPx`** the cumulative
    volume-weighted average across all fills, and **`fillPx`** the price of the most recent
    fill chunk only? Which is authoritative for a multi-chunk fill?
23. For a MARKET order, what is the **expected latency** until fill is confirmable via (a)
    REST `get_order` and (b) the WS order channel? We have seen MARKET fills not confirmed
    within ~900 ms of REST polling.
24. Which **WS channel** is authoritative and lowest-latency for **fills and partial
    fills** (`orders` vs `fills`), and the recommended way to reconcile real-time position
    state to avoid engine/exchange drift?

## Priority 7 — Market data for sizing

25. Best source for **top-of-book depth / liquidity** on these instruments via API, so we
    can size child orders to a target % of resting depth and estimate market impact at
    $100K+.
26. Any guidance on **funding-rate scheduling** that materially affects holding a
    delta-neutral perp/perp spread across funding windows.

---

### Our current mitigations (for context)
- Prefer `avgPx` over `fillPx` for fill price.
- Treat unconfirmed MARKET fills as unfilled (poll rather than assume).
- Apply a 5-minute cooldown after a `cancelSource=31` event.
- Synchronized two-leg execution with optional TWAP slicing and a minimum-fill-ratio guard.

We'd value a short call with your technical team on Priorities 1–2. Thank you.
