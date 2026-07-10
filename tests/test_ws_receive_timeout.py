"""
Regression test for the price-WebSocket stall bug.

The price feed's receive loop did `continue` on a 35s receive timeout, so a
silently-dead socket was never detected — it re-armed receive() on the same
dead socket forever, ticks stopped, the tick-driven heartbeat went stale, and
the watchdog restarted the whole process every ~30 min. It must `break` on the
first timeout and reconnect (matching the execution adapter).
"""
import asyncio
from types import SimpleNamespace

from adapters.okx_websocket import OKXWebSocket


def test_receive_timeout_breaks_and_reconnects():
    mgr = OKXWebSocket.__new__(OKXWebSocket)   # bypass heavy __init__
    mgr._running = True
    reconnected = []
    calls = {"n": 0}

    async def fake_receive(timeout=None):
        calls["n"] += 1
        # Safety net: if the fix ever regresses to `continue`, don't hang the
        # test suite — force the loop to exit after a few spins.
        if calls["n"] > 3:
            mgr._ws.closed = True
            raise asyncio.CancelledError
        raise asyncio.TimeoutError

    async def fake_reconnect():
        reconnected.append(True)

    mgr._ws = SimpleNamespace(closed=False, receive=fake_receive)
    mgr._reconnect = fake_reconnect

    async def run():
        await mgr._receive_loop()
        await asyncio.sleep(0)      # let the scheduled reconnect task run
        await asyncio.sleep(0)

    asyncio.run(run())

    assert calls["n"] == 1          # broke on the FIRST 35s timeout, not `continue`
    assert reconnected == [True]    # ...and scheduled a reconnect
