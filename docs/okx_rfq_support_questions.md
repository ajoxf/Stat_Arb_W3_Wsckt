# Note to OKX — RFQ / Block Trading clarifications before going live

Copy-ready for OKX institutional / API support. Goal: confirm the API semantics
and operational details we need before routing large two-leg orders through RFQ.

---

**Subject: RFQ / Block Trading — API & liquidity questions before enabling automated large-order execution**

Hello OKX team,

We run an automated, market-neutral strategy that trades a **two-leg spread**
between two USDT-margined perpetual swaps — **ETH-USDT-SWAP** and
**BTC-USDT-SWAP** — via the v5 API. To eliminate legging risk as we scale, we
want to execute both legs **atomically** through Block Trading / RFQ
(`/api/v5/rfq/*`) rather than the order book. Target size is on the order of
**$100k notional per leg** ($200k per structure), and we intend to drive it
programmatically (create-rfq → poll quotes → execute-quote).

A specific concern is **risk-driven (stop-loss) exits**, where we must close both
legs **immediately**. The RFQ request→quote→execute cycle appears too slow for
that, and on the order book we currently hit frequent **POST_ONLY rejections
(`cancelSource=31`)** that leave one leg filled and the other unfilled (an orphan
position). We want the lowest-latency way to close both legs safely — see
**Section D**.

Before we enable on a live account, we'd appreciate confirmation on the
following. Numbered for easy point-by-point reply.

## A. Execution semantics (most important)

1. **Leg size units.** In `create-rfq` and `execute-quote`, is the leg `sz` for a
   USDT-margined SWAP denominated in **contracts** (e.g. 22 = 2.2 ETH at
   ctVal 0.1), consistent with `/api/v5/trade/order`? We want to be 100% sure we
   are not sending base-coin amounts.
2. **Leg field schema for `create-rfq`.** For a SWAP leg, which fields are
   **required vs optional**: `instId`, `sz`, `side`, `posSide`, `tdMode`,
   `tgtCcy`? Specifically: is `posSide` required when the account is in
   **long/short (hedge) mode**, and is `tdMode` valid/needed at the leg level?
3. **`execute-quote` matching rules.** Must the `legs` we submit to
   `execute-quote` exactly match the chosen quote (instId, side, sz, px)? What
   error is returned on a mismatch, and is there any tolerance?
4. **Atomicity guarantee.** For a 2-leg structure, can you confirm execution is
   strictly **all-or-none** — there is no scenario where one leg fills and the
   other does not? Is partial execution of a multi-leg RFQ ever possible (and is
   error `70507` the relevant rejection if a partial is attempted)?
5. **Closing / reducing via RFQ in hedge mode.** When we send an RFQ whose legs
   are the opposite side of an open position (same `posSide`), does it **net/reduce**
   the existing position as expected? Is there any `reduceOnly` equivalent for
   RFQ, or any case where a closing RFQ could open an opposite position instead?

## B. Eligibility, liquidity & fees

6. **Account eligibility.** What are the exact requirements to use Block
   Trading / RFQ on a **live** account (minimum assets, KYC tier, region), and is
   it enabled by default for an API key with Trade permission or does it need
   activation?
7. **Minimum notional.** What is the **current minimum per leg / per RFQ** for
   ETH-USDT-SWAP and BTC-USDT-SWAP specifically? (We've seen figures ranging from
   ~$1k up to $50k+ and want the authoritative number for these instruments.)
8. **Counterparty liquidity for a crypto-vs-crypto structure.** This is key for
   us: do market makers actively **quote a two-leg structure composed of two
   different USDT perps** (ETH-SWAP + BTC-SWAP), or is RFQ liquidity primarily for
   options / single-underlying blocks? Typically, how many quotes and at what
   **response latency** should we expect for a ~$100k/leg request?
9. **Fees.** What is the **fee treatment for the taker executing a quote** on a
   SWAP block — taker rate on both legs, any maker rebate, and how does it compare
   to the standard order-book taker fee at our VIP tier?

## C. Large-order / scaling specifics

10. **Maximum size & tiers.** Is there an **upper size limit** per RFQ/block, and
    any size-based tiering or special handling for larger structures?
11. **Margin at execution.** For a 2-leg block, is the required margin for **both
    legs reserved/checked atomically** at execution? Any difference vs placing the
    two legs separately on the book?
12. **Information leakage / anonymity.** With `anonymous=true`, what exactly is
    disclosed to makers, and are there best practices to minimize market impact on
    larger structures?
13. **Validity windows.** Please confirm the **active duration of an RFQ** and the
    **validity of a maker quote** (we currently assume ~2 min live / 60s demo).
14. **Rate limits.** What are the rate limits for `create-rfq`, `get quotes`
    (we poll), and `execute-quote`?

## D. Urgent (stop-loss) closes, latency & avoiding orphan legs

15. **Lowest-latency atomic close.** For risk-driven exits we must close both
    legs immediately. Is RFQ appropriate for this, or is the quote cycle too slow
    — and if so, what is the **recommended low-latency mechanism to close a
    two-leg position atomically** (or as close to atomic as possible)?
16. **execute-quote latency.** Typical and worst-case time from `execute-quote`
    submission to confirmed fill?
17. **Market close endpoint.** Does `/api/v5/trade/close-position`
    (`mgnMode`/`posSide`) market-close a position **immediately**, and what is its
    latency vs a standard market order? Is it suitable for an emergency per-leg
    flatten?
18. **Server-side stop / algo orders.** Do you support **conditional/algo orders**
    (e.g. stop-market via `/api/v5/trade/order-algo`) that trigger **at the
    exchange** without a client round-trip? Are they **single-instrument only**
    (i.e. no atomic multi-leg stop), and what is the trigger-to-fill latency?
19. **Avoiding POST_ONLY rejection on closes.** We see frequent `cancelSource=31`
    POST_ONLY rejections on closes, which orphan one leg. To **guarantee a fill**
    on an urgent close, is a plain (non-post-only) limit crossing the spread, or a
    market order, the recommended approach? Is there any **IOC/FOK** option that
    guarantees both-legs-or-neither across two different instruments?
20. **Lowest-latency order path.** For time-critical placement/cancellation, do
    you recommend the **WebSocket** trade endpoint over REST, and is there a
    measurable latency difference?

Thank you — happy to share our `clRfqId` / structure format if that helps you
advise on the above.

Best regards,
[Name / Org / OKX UID]
