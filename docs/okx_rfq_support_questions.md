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

Before we enable it on a live account, we'd appreciate confirmation on the
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

## D. Testing

15. **Demo support.** Does **demo trading** (`x-simulated-trading: 1`) support the
    full RFQ flow end-to-end (create → receive maker quotes → execute), and will
    demo market makers actually respond to a 2-leg ETH/BTC-SWAP structure — or do
    we need a designated counterparty to test against?

We're ready to validate in demo as soon as we can confirm the above. Thank you —
happy to share our `clRfqId` / structure format if that helps you advise.

Best regards,
[Name / Org / OKX UID]
