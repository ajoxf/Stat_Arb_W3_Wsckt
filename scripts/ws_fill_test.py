"""
OKX WebSocket Milestone 3 fill-detection smoke test.

Demonstrates push-driven fill detection through the SAME interface the executor
uses (`get_order_status`). Connects to OKX testnet, places a small marketable
LIMIT order that fills immediately, then confirms the fill is observed from the
WS order-push cache (no REST poll), then flattens the position.

Run:
    python -m scripts.ws_fill_test

Prerequisites (.env):
    OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE  — demo-trading credentials
    OKX_DEMO_MODE=true

WARNING: unlike ws_trade_test.py (which posts a passive, non-filling order),
this script INTENTIONALLY fills a small order on the demo account and then
closes the resulting position. It uses demo funds only.
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

SYMBOL = "ETH-USDT-260626"   # dated futures the live bot trades
QUANTITY = 0.1               # ETH base units → 1 contract (ctVal = 0.1)

# Cross the book by this fraction so the LIMIT fills immediately as a taker
# while staying bounded (no unbounded MARKET slippage on a thin demo book).
CROSS_PCT = 0.01             # 1% through the ask

# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]


def _ok(cond: bool) -> str:
    return "PASS" if cond else "FAIL"


async def _wait_filled_from_cache(adapter, order_id: str, timeout: float = 5.0):
    """
    Poll the executor-facing get_order_status until the order is filled.

    Returns (status_dict, from_cache) where from_cache is True when the filled
    state was sourced from the WS push cache (not a REST poll).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        # Push cache is the source of truth the executor reads first.
        cached = adapter._order_cache.get(order_id)
        if cached and cached.get("state") == "filled":
            status = await adapter.get_order_status(SYMBOL, order_id)
            return status, True
        await asyncio.sleep(0.05)
    # Last resort: whatever get_order_status returns (may be a REST poll).
    return await adapter.get_order_status(SYMBOL, order_id), False


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

    print(f"[{_ts()}] Connecting to OKX testnet …")
    if not await adapter.connect():
        print(f"[{_ts()}] ERROR: {adapter.last_error}", file=sys.stderr)
        sys.exit(1)
    print(f"[{_ts()}] Connected, authenticated, subscribed.")

    results: dict = {}
    order_id = None

    try:
        tick = await adapter.get_tick(SYMBOL)
        if not tick:
            print(f"[{_ts()}] ERROR: could not fetch tick for {SYMBOL}", file=sys.stderr)
            return

        order_price = round(tick.ask * (1 + CROSS_PCT), 2)
        print(f"[{_ts()}] {SYMBOL}: bid={tick.bid:.2f}  ask={tick.ask:.2f}")
        print(f"[{_ts()}] Will BUY {QUANTITY} ETH marketable LIMIT @ {order_price:.2f} (crosses, taker)")

        # ------------------------------------------------------------------
        # 1. Place a marketable LIMIT that fills immediately
        # ------------------------------------------------------------------
        print()
        print(f"[{_ts()}] ─── Step 1: place marketable order (WS) ───")
        result = await adapter.place_order(
            symbol=SYMBOL, side="buy", order_type="LIMIT",
            quantity=QUANTITY, price=order_price,
        )
        results["place"] = result.success
        if not result.success:
            print(f"[{_ts()}] FAIL  {result.error}")
            return
        order_id = result.order_id
        print(f"[{_ts()}] PASS  ordId={order_id}")

        # ------------------------------------------------------------------
        # 2. Detect the fill from the WS push cache (executor's interface)
        # ------------------------------------------------------------------
        print()
        print(f"[{_ts()}] ─── Step 2: fill detected via WS push (get_order_status) ───")
        status, from_cache = await _wait_filled_from_cache(adapter, order_id)
        filled = bool(status and status.get("state") == "filled"
                      and status.get("filled_qty", 0) > 0)
        results["fill_detected"] = filled
        results["fill_from_push"] = filled and from_cache
        if filled:
            print(f"[{_ts()}] PASS  state=filled  filled_qty={status['filled_qty']}  "
                  f"filled_price={status['filled_price']}")
            print(f"[{_ts()}] {'PASS' if from_cache else 'WARN'}  "
                  f"fill sourced from {'WS push cache' if from_cache else 'REST poll (cache miss)'}  "
                  f"({len(order_events)} order event(s))")
        else:
            st = (status or {}).get("state", "unknown")
            print(f"[{_ts()}] FAIL  order not filled within timeout (state={st})")

    finally:
        # ------------------------------------------------------------------
        # 3. Flatten the position created by the test fill
        # ------------------------------------------------------------------
        if order_id and results.get("fill_detected"):
            print()
            print(f"[{_ts()}] ─── Step 3: flatten test position ───")
            try:
                close = await adapter.close_position(SYMBOL)
                results["flatten"] = bool(close and close.success)
                print(f"[{_ts()}] {'PASS' if results.get('flatten') else 'WARN'}  "
                      f"close_position success={getattr(close, 'success', None)} "
                      f"{getattr(close, 'error', '') or ''}")
                await asyncio.sleep(0.5)
                positions = await adapter.get_positions(SYMBOL)
                net = sum(abs(p.quantity) for p in positions) if positions else 0.0
                print(f"[{_ts()}]       residual position size for {SYMBOL}: {net}")
            except Exception as e:
                print(f"[{_ts()}] WARN  flatten error: {e}")

        await adapter.disconnect()
        print()
        print(f"[{_ts()}] ══════════════════════════════")
        print(f"[{_ts()}]  Milestone 3 fill-detection results")
        print(f"[{_ts()}] ══════════════════════════════")
        print(f"[{_ts()}]  place (marketable) : {_ok(results.get('place', False))}")
        print(f"[{_ts()}]  fill detected      : {_ok(results.get('fill_detected', False))}")
        print(f"[{_ts()}]  fill via WS push   : {_ok(results.get('fill_from_push', False))}")
        print(f"[{_ts()}]  position flattened : {_ok(results.get('flatten', False))}")
        print(f"[{_ts()}] ══════════════════════════════")
        core_pass = all(results.get(k, False) for k in ("place", "fill_detected", "fill_from_push"))
        print(f"[{_ts()}]  Overall: {'ALL PASS' if core_pass else 'SOME FAILURES'}")
        print(f"[{_ts()}] Disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
