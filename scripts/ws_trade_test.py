"""
OKX WebSocket Milestone 2 trade smoke test.

Connects to OKX testnet, places a passive POST_ONLY limit order 5% below
market (guaranteed not to fill), verifies the WS order push lands in the
order cache, amends the price, then cancels via WS.  Prints a PASS/FAIL
summary at the end.

Run:
    python -m scripts.ws_trade_test

Prerequisites (.env):
    OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE  — demo-trading credentials
    OKX_DEMO_MODE=true

The test uses ETH-USDT-260626 (the dated futures leg from the live bot).
Adjust SYMBOL below if you are on a different contract expiry.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("adapters.okx_ws_adapter").setLevel(logging.DEBUG)

from adapters.okx_ws_adapter import OKXWebSocketAdapter  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# ETH-USDT-SWAP = perpetual swap, used as WS smoke-test proxy.
# The dated futures contract (ETH-USDT-260626) returns sCode 50014
# "Parameter instIdCode can not be empty" on the demo WS endpoint — a
# demo-specific quirk for dated FUTURES not present on the live endpoint.
# The perpetual swap exercises the same place/amend/cancel WS paths.
# Switch back to "ETH-USDT-260626" when testing on the live endpoint.
SYMBOL = "ETH-USDT-SWAP"

# 1 contract = 0.01 ETH on OKX ETH-USDT-SWAP. _prepare_order converts
# base-currency quantity to contracts automatically via get_symbol_info.
QUANTITY = 0.1   # ETH base units → 10 contracts

# How far below market to post the passive BUY (will not fill).
DISCOUNT_PCT = 0.05   # 5% below mid

# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]


def _ok(cond: bool) -> str:
    return "PASS" if cond else "FAIL"


async def _wait_for_cache(adapter, order_id: str, predicate, timeout: float = 3.0) -> bool:
    """Poll adapter._order_cache until predicate(cached_order) is True or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        cached = adapter._order_cache.get(order_id)
        if cached and predicate(cached):
            return True
        await asyncio.sleep(0.1)
    return False


async def main() -> None:
    api_key    = os.getenv("OKX_API_KEY", "")
    secret_key = os.getenv("OKX_SECRET_KEY", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")

    if not all([api_key, secret_key, passphrase]):
        print(
            "ERROR: OKX_API_KEY, OKX_SECRET_KEY, and OKX_PASSPHRASE must be set "
            "in .env (use demo-trading credentials with OKX_DEMO_MODE=true)",
            file=sys.stderr,
        )
        sys.exit(1)

    adapter = OKXWebSocketAdapter(
        api_key=api_key,
        secret_key=secret_key,
        passphrase=passphrase,
        is_testnet=True,
    )

    order_events: list = []
    adapter.on_order_update = lambda d: order_events.append(d)

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------
    print(f"[{_ts()}] Connecting to OKX testnet …")
    if not await adapter.connect():
        print(f"[{_ts()}] ERROR: {adapter.last_error}", file=sys.stderr)
        sys.exit(1)
    print(f"[{_ts()}] Connected, authenticated, subscribed.")

    results: dict = {}

    try:
        # ------------------------------------------------------------------
        # 1. Get current market price
        # ------------------------------------------------------------------
        tick = await adapter.get_tick(SYMBOL)
        if not tick:
            print(f"[{_ts()}] ERROR: could not fetch tick for {SYMBOL}", file=sys.stderr)
            return

        mid = (tick.bid + tick.ask) / 2
        order_price = round(mid * (1 - DISCOUNT_PCT), 2)
        amend_price = round(order_price * 0.99, 2)   # 1% lower than initial

        print(f"[{_ts()}] {SYMBOL}: bid={tick.bid:.2f}  ask={tick.ask:.2f}  mid={mid:.2f}")
        print(f"[{_ts()}] Will place BUY POST_ONLY at {order_price:.2f}  "
              f"(amend target: {amend_price:.2f})")

        # ------------------------------------------------------------------
        # 2. place_order via WS
        # ------------------------------------------------------------------
        print()
        print(f"[{_ts()}] ─── Step 1: place_order ───")
        result = await adapter.place_order(
            symbol=SYMBOL,
            side="buy",
            order_type="POST_ONLY",
            quantity=QUANTITY,
            price=order_price,
        )
        results["place"] = result.success
        if result.success:
            order_id = result.order_id
            print(f"[{_ts()}] PASS  ordId={order_id}")
        else:
            print(f"[{_ts()}] FAIL  {result.error}")
            return   # can't proceed without an order

        # ------------------------------------------------------------------
        # 3. Wait for WS order push to appear in cache
        # ------------------------------------------------------------------
        print()
        print(f"[{_ts()}] ─── Step 2: WS push lands in order cache ───")
        push_ok = await _wait_for_cache(
            adapter, order_id,
            predicate=lambda o: o.get("state") in ("live", "partially_filled", "filled"),
        )
        results["push"] = push_ok
        if push_ok:
            state = adapter._order_cache[order_id]["state"]
            print(f"[{_ts()}] PASS  state={state}  (received {len(order_events)} order event(s))")
        else:
            print(f"[{_ts()}] WARN  push not received within 3 s (cache may lag on testnet)")

        # ------------------------------------------------------------------
        # 4. amend_order via WS
        # ------------------------------------------------------------------
        print()
        print(f"[{_ts()}] ─── Step 3: amend_order (price {order_price:.2f} → {amend_price:.2f}) ───")
        before_events = len(order_events)
        amended = await adapter.amend_order(SYMBOL, order_id, new_price=amend_price)
        results["amend"] = amended
        print(f"[{_ts()}] {'PASS' if amended else 'FAIL'}  amend_order returned {amended}")
        if amended:
            # Brief wait for push
            await asyncio.sleep(0.5)
            after_events = len(order_events)
            print(f"[{_ts()}]       new order events since amend: {after_events - before_events}")

        # ------------------------------------------------------------------
        # 5. cancel_order via WS
        # ------------------------------------------------------------------
        print()
        print(f"[{_ts()}] ─── Step 4: cancel_order ───")
        before_events = len(order_events)
        cancelled = await adapter.cancel_order(SYMBOL, order_id)
        results["cancel"] = cancelled
        print(f"[{_ts()}] {'PASS' if cancelled else 'FAIL'}  cancel_order returned {cancelled}")

        # Wait for cancel push
        cancel_push_ok = await _wait_for_cache(
            adapter, order_id,
            predicate=lambda o: o.get("state") in ("canceled", "cancelled"),
        )
        results["cancel_push"] = cancel_push_ok
        if cancel_push_ok:
            print(f"[{_ts()}] PASS  cancel state confirmed in WS cache")
        else:
            final_state = (adapter._order_cache.get(order_id) or {}).get("state", "not in cache")
            print(f"[{_ts()}] WARN  cancel state not seen within 3 s (cache state: {final_state})")

    finally:
        await adapter.disconnect()
        print()
        print(f"[{_ts()}] ══════════════════════════════")
        print(f"[{_ts()}]  Milestone 2 trade test results")
        print(f"[{_ts()}] ══════════════════════════════")
        print(f"[{_ts()}]  place_order  (WS) : {_ok(results.get('place', False))}")
        print(f"[{_ts()}]  WS push to cache  : {_ok(results.get('push', False))}")
        print(f"[{_ts()}]  amend_order  (WS) : {_ok(results.get('amend', False))}")
        print(f"[{_ts()}]  cancel_order (WS) : {_ok(results.get('cancel', False))}")
        print(f"[{_ts()}]  cancel push recv  : {_ok(results.get('cancel_push', False))}")
        print(f"[{_ts()}]  Total order events: {len(order_events)}")
        print(f"[{_ts()}] ══════════════════════════════")

        all_pass = all(results.get(k, False) for k in ("place", "amend", "cancel"))
        print(f"[{_ts()}]  Overall: {'ALL PASS' if all_pass else 'SOME FAILURES'}")
        print(f"[{_ts()}] Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
