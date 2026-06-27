# Email to OKX — RFQ / Block Trading questions (10 points)

Copy-ready. Fill in your name / company / OKX UID at the bottom before sending.

---

**Subject: Questions about RFQ / Block Trading before we go live**

Hi OKX team,

We run an automated strategy that trades two USDT perpetual swaps together as a
pair — **ETH-USDT-SWAP** and **BTC-USDT-SWAP** — through the v5 API. To avoid one
leg filling without the other, we want to execute both legs together using
**Block Trading / RFQ**, at around **$100k per leg**. Before enabling this on a
live account, could you help with the following:

1. **Order size units** — When we submit a leg, is the size (`sz`) in **contracts**
   (e.g. 22 = 2.2 ETH), like a normal swap order — or in coins?

2. **All-or-nothing** — For a 2-leg RFQ, can you confirm both legs **always fill
   together or neither does** (no way to end up with just one leg filled)?

3. **Closing a position** — When we send an RFQ to close (opposite side, same
   position side in hedge mode), does it correctly **reduce our existing position**
   rather than open a new one? Which leg fields are required (e.g. `posSide`,
   `tdMode`)?

4. **Account access** — What do we need to use RFQ/Block on a **live account**
   (minimum balance, verification, region), and is it enabled by default for an API
   key with trade permission?

5. **Min and max size** — What is the **minimum and maximum** notional per leg for
   ETH-USDT-SWAP and BTC-USDT-SWAP?

6. **Will makers quote our pair?** — Do market makers actually quote a structure
   made of **two different USDT perps** (ETH + BTC)? Roughly how many quotes, and
   how fast, for ~$100k per leg?

7. **Fees** — What fees apply when we execute a block quote (taker on both legs?
   any rebate?), compared to normal order-book fees at our tier?

8. **Closing fast on a stop-loss** — When we must exit immediately to cap a loss,
   the RFQ quote process seems too slow. What is the **fastest way to close both
   legs at once**? Do you offer **server-side stop orders** or an instant market
   **"close position"** we can rely on?

9. **Avoiding rejected / orphaned orders** — On the order book we often get
   POST_ONLY orders rejected (`cancelSource=31`), which leaves one leg filled and
   the other not. To **guarantee a fill** on an urgent close, do you recommend a
   market order — and is there any **all-or-none (IOC/FOK)** option that covers
   both instruments together?

10. **Timing and limits** — How long does an **RFQ (and a maker's quote) stay
    valid**, and what are the **API rate limits** for creating an RFQ, polling for
    quotes, and executing?

Thanks very much — we're ready to test once these are clear.

Best regards,
[Name / Company / OKX UID]
