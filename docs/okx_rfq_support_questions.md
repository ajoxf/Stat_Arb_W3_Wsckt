# Email to OKX — RFQ / Block Trading questions (16 points)

Copy-ready. Fill in your name / company / OKX UID at the bottom before sending.

---

**Subject: RFQ / Block Trading — questions before we go live**

Hi OKX team,

We run an automated strategy that trades two USDT perpetual swaps as a pair —
**ETH-USDT-SWAP** and **BTC-USDT-SWAP** — via the v5 API. To avoid one leg
filling without the other, we want to execute both legs together through
**Block Trading / RFQ**, at around **$100k per leg**, driven programmatically.
Before going live, could you confirm the following?

### How it works (technical)

1. **Size units** — When we submit a leg, is the size (`sz`) in **contracts**
   (e.g. 22 = 2.2 ETH) like a normal swap order, or in coins?

2. **Required leg fields** — For a swap leg in `create-rfq`, which fields are
   required vs optional (`instId`, `sz`, `side`, `posSide`, `tdMode`, `tgtCcy`)?
   Is `posSide` required when the account is in hedge (long/short) mode?

3. **Executing a quote** — When we execute a quote, must our legs **exactly match
   the quote** (instId, side, size, price)? What error is returned if they don't?

4. **All-or-nothing** — Can you confirm a 2-leg RFQ always fills **both legs
   together or neither**? And must a maker's quote cover the **full size** we
   requested, or can it be partial?

5. **Closing a position** — When we send an RFQ to close (opposite side, same
   position side in hedge mode), does it correctly **reduce our existing position**
   rather than open a new one? Is there a reduce-only concept for RFQ?

6. **Receiving quotes** — What is the recommended (and lowest-latency) way to
   receive quotes: the **WebSocket** channels (rfqs / quotes / block-trades) or
   **REST polling**?

7. **Confirming fills** — After we execute, **where does the trade appear**
   (positions, fills, order history), and what is the best way to confirm/reconcile
   that both legs filled?

### Access, liquidity & cost

8. **Account access** — What do we need to use RFQ/Block on a **live account**
   (minimum balance, verification, region), and is it on by default for an API key
   with trade permission, or does it need activation?

9. **Counterparties** — Do we need to onboard or whitelist specific market makers,
   or is **"broadcast to all"** available by default? With **anonymous** mode on,
   what is disclosed to makers?

10. **Min and max size** — What is the **minimum and maximum** notional per leg for
    ETH-USDT-SWAP and BTC-USDT-SWAP, and is size measured **per leg** or for the
    whole structure?

11. **Will makers quote our pair?** — Do market makers actively quote a structure
    made of **two different USDT perps** (ETH + BTC)? Roughly how many quotes, and
    how fast, for ~$100k per leg?

12. **Fees** — What fees apply when we execute a block quote (taker on both legs?
    any rebate?), versus normal order-book fees at our tier?

### Speed & risk (important for us)

13. **Margin** — Is the margin for **both legs reserved/checked together
    (atomically)** at execution, so an execution can't fail midway?

14. **Closing fast on a stop-loss** — When we must exit immediately to cap a loss,
    the RFQ quote process seems too slow. What is the **fastest way to close both
    legs at once**? Do you offer **server-side stop orders**, or an instant market
    **"close position"** we can rely on?

15. **Avoiding rejected / orphaned orders** — On the order book we often get
    POST_ONLY orders rejected (`cancelSource=31`), leaving one leg filled and the
    other not. To **guarantee a fill** on an urgent close, do you recommend a market
    order — and is there any **all-or-none (IOC/FOK)** option covering both
    instruments together? Also, is **WebSocket or REST** faster for placing /
    cancelling time-critical orders?

### Operations

16. **Timing, limits & housekeeping** — How long does an **RFQ (and a maker's
    quote) stay valid**? What are the **API rate limits** for creating an RFQ,
    polling for quotes, and executing? If we create an RFQ but don't execute (e.g.
    the price is too wide), does it **auto-expire or must we cancel** — and is there
    any **penalty** for creating RFQs we don't execute?

Lastly, is there a dedicated **institutional / API support contact** for Block
Trading we can reach during live operation?

Thanks very much — we're ready to test once these are clear.

Best regards,
[Name / Company / OKX UID]
