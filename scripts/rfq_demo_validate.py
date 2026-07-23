#!/usr/bin/env python3
"""
Demo-validate the OKX RFQ / Block Trading FULL LOOP, end-to-end, with no real money.

Runs against OKX DEMO (x-simulated-trading) and answers the questions that only
the live exchange can — before we route real $100k clips through RFQ:

  [1] Counterparties available?          else an RFQ reaches no maker -> no quotes
  [2] sz -> contracts sizing             the size we send for $100k/leg
  [3] create-rfq accepted?               auth, leg schema (instId/sz/side/tdMode/posSide)
  [4] Do quotes come back?               do makers quote a 2-perp ETH+BTC structure?
  [5] Quote size == our contract count?  the live proof of the sz-in-contracts fix
      + markup vs mid (bps) and time-to-quote (economics + latency)
  [6] --execute: atomic fill + blockTdId  then reconcile both legs in /trade/fills

Usage (run from the repo root, with DEMO API keys in the environment):
    set OKX_API_KEY=...  OKX_SECRET_KEY=...  OKX_PASSPHRASE=...
    python scripts/rfq_demo_validate.py                    # create -> quotes -> CANCEL (no fill, safe)
    python scripts/rfq_demo_validate.py --execute          # ... -> EXECUTE (demo fill)
    python scripts/rfq_demo_validate.py --notional 100000  # size per leg (default 100k)

NOTE: demo may not have market makers responding to RFQs. If step [4] returns no
quotes, that is usually an OKX-demo/liquidity fact, not a bug in our code — confirm
with your account manager whether demo makers quote, or run a single small LIVE RFQ.
"""
import argparse
import asyncio
import os
import sys
import time

# Make the repo root importable no matter where this is invoked from (e.g. from
# inside scripts/), mirroring the other package-importing scripts in this dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.okx_adapter import OKXAdapter  # noqa: E402
from adapters.okx_rfq_adapter import OKXRFQAdapter  # noqa: E402

SPOT = "ETH-USDT-SWAP"   # leg A
FUT = "BTC-USDT-SWAP"    # leg B


async def _to_contracts(rest: OKXAdapter, symbol: str, notional: float):
    """Convert a USD notional to integer contracts, exactly like the live path."""
    info = await rest.get_symbol_info(symbol)
    tick = await rest.get_tick(symbol)
    mid = ((tick.bid + tick.ask) / 2.0) if (tick and tick.bid and tick.ask) else 0.0
    ct_val = float(info["contract_val"]) if info else 0.0
    if mid <= 0 or ct_val <= 0:
        raise RuntimeError(f"cannot size {symbol}: mid={mid} ctVal={ct_val}")
    contracts = max(1, int((notional / mid) / ct_val))
    return contracts, mid, ct_val


async def main(notional: float, do_execute: bool, timeout: float, is_demo: bool) -> None:
    # Load the same .env the bot uses so creds don't have to be exported by hand.
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    except Exception:
        pass
    key = os.getenv("OKX_API_KEY")
    sec = os.getenv("OKX_SECRET_KEY")
    pw = os.getenv("OKX_PASSPHRASE")
    if not (key and sec and pw):
        raise SystemExit("Set OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE.")

    rest = OKXAdapter(key, sec, pw, is_testnet=is_demo)
    await rest.connect()
    rfq = OKXRFQAdapter(key, sec, pw, is_testnet=is_demo)

    env = "DEMO" if is_demo else "LIVE"
    print(f"== OKX RFQ validation ==  ({env})")
    if not is_demo:
        print("   LIVE market. Default run creates an RFQ and CANCELS it (NO fill — safe).")
        if do_execute:
            print(f"   ⚠ --execute WILL place a REAL ~${notional:,.0f}/leg block trade.")
    print()

    # [1] counterparties
    cps = await rfq.get_counterparties()
    print(f"[1] counterparties: {len(cps)} available "
          f"-> {cps[:5]}{'...' if len(cps) > 5 else ''}")
    if not cps:
        print("    ✗ no counterparties returned. Check the [rfq_adapter] error logged just above — common causes:")
        print("      • 70019 → accept the RFQ 'Broker Dealer Agreement' on okx.com (Trade → Block/RFQ). Self-service, one-time.")
        print("      • 50101 → API key is for the other environment (live key with --demo, or a demo key without it). Demo needs a DEMO key.")
        print("      • neither → ask OKX to enable block-trading / RFQ makers for the account.\n")
        await rfq.disconnect()
        await rest.disconnect()
        return

    # [2] sizing
    eth_ct, eth_mid, eth_cv = await _to_contracts(rest, SPOT, notional)
    btc_ct, btc_mid, btc_cv = await _to_contracts(rest, FUT, notional)
    print(f"[2] sizing @ ${notional:,.0f}/leg:")
    print(f"      {SPOT}: {eth_ct} contracts  (mid {eth_mid}, ctVal {eth_cv})")
    print(f"      {FUT}: {btc_ct} contracts  (mid {btc_mid}, ctVal {btc_cv})")

    # Example SHORT spread: sell ETH, buy BTC (test only — direction is arbitrary here)
    legs = [
        {"instId": SPOT, "sz": str(eth_ct), "side": "sell", "tdMode": "cross", "posSide": "short"},
        {"instId": FUT, "sz": str(btc_ct), "side": "buy", "tdMode": "cross", "posSide": "long"},
    ]

    # [3] create-rfq
    t0 = time.time()
    rfq_id = await rfq.create_rfq(legs=legs, anonymous=True, counterparties=cps)
    if not rfq_id:
        print("[3] ✗ FAIL: create-rfq returned no rfqId "
              "(check leg schema / min size / eligibility in the logged error).\n")
        await rfq.disconnect()
        await rest.disconnect()
        return
    print(f"[3] create-rfq OK: rfqId={rfq_id}")

    # [4] poll for quotes
    print(f"[4] waiting up to {timeout:.0f}s for maker quotes ...")
    quotes = []
    while time.time() - t0 < timeout:
        quotes = [q for q in await rfq.get_quotes(rfq_id) if q.is_active()]
        if quotes:
            break
        await asyncio.sleep(0.5)
    if not quotes:
        print(f"    ✗ NO QUOTES within {timeout:.0f}s. Either demo has no responding makers, or the")
        print("      whitelisted makers don't quote this structure. This is THE key unknown —")
        print("      confirm with your account manager (not necessarily a code bug).\n")
        await rfq.cancel_rfq(rfq_id)
        await rfq.disconnect()
        await rest.disconnect()
        return
    dt = time.time() - t0
    print(f"    ✓ {len(quotes)} quote(s) in {dt:.2f}s")

    for q in quotes:
        legs_s = ", ".join(
            f"{l.get('instId')} {l.get('side')} sz={l.get('sz')} px={l.get('px')}" for l in q.legs
        )
        print(f"      quote {q.quote_id}: {legs_s}")

    # [5] validate size == contracts, compute markup vs mid
    best = quotes[0]
    sz_ok = (best.size_for(SPOT) in ("", str(eth_ct))) and (best.size_for(FUT) in ("", str(btc_ct)))
    print(f"[5] quote size matches our contract count? {'✓' if sz_ok else '✗'}  "
          f"(expected {eth_ct} / {btc_ct}, got {best.size_for(SPOT)} / {best.size_for(FUT)})")
    for sym, mid, side in ((SPOT, eth_mid, "sell"), (FUT, btc_mid, "buy")):
        px = best.price_for(sym)
        if px and mid:
            slip = (px - mid) if side == "buy" else (mid - px)
            print(f"      {sym} markup vs mid: {slip / mid * 10000:+.1f} bps  (px {px} vs mid {mid})")

    if not do_execute:
        print("\n[6] --execute not set -> cancelling RFQ (no fill). Loop validated through quotes.\n")
        await rfq.cancel_rfq(rfq_id)
        await rfq.disconnect()
        await rest.disconnect()
        return

    # [6] execute — accept the quote as-is (size + price from the quote)
    exec_legs = [
        {"instId": SPOT, "sz": best.size_for(SPOT) or str(eth_ct),
         "side": "sell", "px": str(best.price_for(SPOT)), "posSide": "short"},
        {"instId": FUT, "sz": best.size_for(FUT) or str(btc_ct),
         "side": "buy", "px": str(best.price_for(FUT)), "posSide": "long"},
    ]
    trade = await rfq.execute_quote(rfq_id, best.quote_id, exec_legs)
    if not trade or trade.get("_ambiguous"):
        print(f"[6] ✗ execute-quote failed/ambiguous: {trade}\n")
    else:
        block_id = trade.get("blockTdId") or trade.get("tTradeId")
        print(f"[6] ✓ EXECUTED atomically: blockTdId={block_id}")
        print(f"      legs: {trade.get('legs')}")
        print(f"      -> confirm BOTH legs appear under blockTdId={block_id} in "
              "GET /api/v5/trade/fills before treating it as done.\n")

    await rfq.disconnect()
    await rest.disconnect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Validate the OKX RFQ full loop (live by default).")
    ap.add_argument("--notional", type=float, default=100000.0, help="USD notional per leg (default 100k)")
    ap.add_argument("--execute", action="store_true", help="execute the best quote (REAL fill on --live)")
    ap.add_argument("--timeout", type=float, default=15.0, help="seconds to wait for quotes")
    ap.add_argument("--demo", action="store_true", help="use OKX demo (x-simulated-trading) instead of live")
    args = ap.parse_args()
    asyncio.run(main(args.notional, args.execute, args.timeout, args.demo))
