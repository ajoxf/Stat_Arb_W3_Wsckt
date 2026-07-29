"""
Regression tests for the runtime pair-change / market-feed re-point.

Bug this guards against (live): changing Leg A from BTC-USD_UM-260807 to
ETH-USDT-SWAP updated the config + dashboard label, but the ticker socket kept
streaming the OLD instrument. _on_websocket_tick routes strictly by instId, so
the post-change ticks matched neither leg and Leg A's price FROZE at the last
BTC-USD_UM value (~$64k, 10%-wide book) under an "ETH-USDT-SWAP" label.

resubscribe_pair() is the fix: it drops the old subscription + its stale tick,
records the new pair as the desired subscription (so auto-reconnect restores the
NEW pair), and subscribes what's newly added.
"""
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from adapters.okx_websocket import OKXWebSocket  # noqa: E402
from models import MarketTick  # noqa: E402


def _connected_ws():
    ws = OKXWebSocket(is_demo=False)
    ws._connected = True
    ws._ws = MagicMock()
    ws._ws.closed = False
    ws._ws.send_json = AsyncMock()
    return ws


def _tick(sym, bid, ask, last):
    return MarketTick(symbol=sym, bid=bid, ask=ask, last=last,
                      volume_24h=0.0, timestamp=datetime.utcnow())


@pytest.mark.asyncio
async def test_resubscribe_swaps_symbol_and_drops_stale_tick():
    ws = _connected_ws()
    ws._subscribed = {"BTC-USD_UM-260807", "BTC-USDT-SWAP"}
    ws._ticks = {
        "BTC-USD_UM-260807": _tick("BTC-USD_UM-260807", 60680, 67067, 63873),
        "BTC-USDT-SWAP": _tick("BTC-USDT-SWAP", 63600.0, 63600.1, 63600.05),
    }

    ok = await ws.resubscribe_pair(["ETH-USDT-SWAP", "BTC-USDT-SWAP"])

    assert ok is True
    # Desired subscription is exactly the new pair — a reconnect restores THIS,
    # not the old BTC-USD_UM symbol.
    assert ws._subscribed == {"ETH-USDT-SWAP", "BTC-USDT-SWAP"}
    # The frozen BTC-USD_UM tick is gone: get_tick can't hand back a stale price.
    assert ws.get_tick("BTC-USD_UM-260807") is None
    # The unchanged leg keeps its live tick (no needless churn).
    assert ws.get_tick("BTC-USDT-SWAP") is not None
    # One unsubscribe (old leg) + one subscribe (new leg).
    assert ws._ws.send_json.await_count == 2


@pytest.mark.asyncio
async def test_resubscribe_is_noop_when_pair_unchanged():
    ws = _connected_ws()
    ws._subscribed = {"ETH-USDT-SWAP", "BTC-USDT-SWAP"}

    await ws.resubscribe_pair(["ETH-USDT-SWAP", "BTC-USDT-SWAP"])

    # Nothing to unsubscribe or subscribe — no socket traffic.
    ws._ws.send_json.assert_not_awaited()
    assert ws._subscribed == {"ETH-USDT-SWAP", "BTC-USDT-SWAP"}


@pytest.mark.asyncio
async def test_resubscribe_records_desired_pair_even_while_disconnected():
    # Momentarily disconnected: we can't send, but the desired pair must still
    # be recorded so _reconnect() resubscribes the NEW pair once it's back.
    ws = OKXWebSocket(is_demo=False)
    ws._connected = False
    ws._subscribed = {"BTC-USD_UM-260807", "BTC-USDT-SWAP"}
    ws._ticks = {"BTC-USD_UM-260807": _tick("BTC-USD_UM-260807", 60680, 67067, 63873)}

    ok = await ws.resubscribe_pair(["ETH-USDT-SWAP", "BTC-USDT-SWAP"])

    assert ok is True
    assert ws._subscribed == {"ETH-USDT-SWAP", "BTC-USDT-SWAP"}
    assert ws.get_tick("BTC-USD_UM-260807") is None
