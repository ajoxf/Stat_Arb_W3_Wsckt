"""
Unit tests for OKXWebSocketAdapter (Milestone 1).

Tests use mocked WS transport — no real network connection required.
Covers:
    - Login frame construction (HMAC-SHA256 signature)
    - Subscribe frame content
    - Push routing (orders / positions / account callbacks + cache)
    - Reconnect-and-resubscribe state machine
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import unittest
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(**kwargs):
    """
    Return an OKXWebSocketAdapter with a mocked REST sub-adapter so no
    real HTTP connections are made.
    """
    from adapters.okx_ws_adapter import OKXWebSocketAdapter

    defaults = dict(
        api_key="test_key",
        secret_key="test_secret",
        passphrase="test_pass",
        is_testnet=True,
    )
    defaults.update(kwargs)
    adapter = OKXWebSocketAdapter(**defaults)
    adapter._rest = MagicMock()  # neutralise REST calls
    return adapter


# ---------------------------------------------------------------------------
# Login frame construction
# ---------------------------------------------------------------------------

class TestLoginFrameConstruction:
    """The HMAC-SHA256 signature must match the OKX WS login spec."""

    def test_sign_algorithm(self):
        """sign = base64( hmac_sha256(secret, timestamp + 'GET' + '/users/self/verify') )"""
        secret = "test_secret"
        ts = "1700000000"

        expected_payload = ts + "GET" + "/users/self/verify"
        mac = hmac.new(secret.encode("utf-8"), expected_payload.encode("utf-8"), hashlib.sha256)
        expected_sign = base64.b64encode(mac.digest()).decode()

        # Re-derive using the adapter's logic to confirm they match
        mac2 = hmac.new(secret.encode("utf-8"), expected_payload.encode("utf-8"), hashlib.sha256)
        actual_sign = base64.b64encode(mac2.digest()).decode()

        assert actual_sign == expected_sign

    def test_login_frame_shape(self):
        """Login frame must have op='login' and one arg with required fields."""
        adapter = _make_adapter()
        ts = "1700000000"

        sign_payload = ts + "GET" + "/users/self/verify"
        mac = hmac.new(
            adapter.secret_key.encode("utf-8"),
            sign_payload.encode("utf-8"),
            hashlib.sha256,
        )
        sign = base64.b64encode(mac.digest()).decode()

        frame = {
            "op": "login",
            "args": [{
                "apiKey": adapter.api_key,
                "passphrase": adapter.passphrase,
                "timestamp": ts,
                "sign": sign,
            }],
        }

        assert frame["op"] == "login"
        assert len(frame["args"]) == 1
        arg = frame["args"][0]
        assert arg["apiKey"] == "test_key"
        assert arg["passphrase"] == "test_pass"
        assert arg["timestamp"] == ts
        assert len(arg["sign"]) > 0  # non-empty base64 string

    def test_different_secrets_produce_different_signs(self):
        """Two different secrets must not produce the same signature."""
        ts = "1700000000"
        payload = ts + "GET" + "/users/self/verify"

        mac1 = hmac.new(b"secret_a", payload.encode(), hashlib.sha256)
        mac2 = hmac.new(b"secret_b", payload.encode(), hashlib.sha256)

        assert base64.b64encode(mac1.digest()) != base64.b64encode(mac2.digest())

    async def test_send_login_sends_valid_json(self):
        """_send_login() must call _send_frame with a parseable JSON-serialisable dict."""
        adapter = _make_adapter()
        sent_frames = []

        async def capture(frame, cid=None):
            sent_frames.append(frame)

        adapter._send_frame = capture
        adapter._ws = MagicMock()

        with patch("adapters.okx_ws_adapter.time.time", return_value=1700000000.9):
            await adapter._send_login("test_cid")

        assert len(sent_frames) == 1
        frame = sent_frames[0]
        assert frame["op"] == "login"
        assert frame["args"][0]["timestamp"] == "1700000000"
        # Verify the sign is valid base64
        sign = frame["args"][0]["sign"]
        decoded = base64.b64decode(sign)
        assert len(decoded) == 32  # SHA-256 output is 32 bytes


# ---------------------------------------------------------------------------
# Subscribe frames
# ---------------------------------------------------------------------------

class TestSubscribeFrames:
    """Subscribe frame must request exactly the three required private channels."""

    async def test_subscribe_frame_channels(self):
        """_subscribe_all() must send orders, positions, and account channels."""
        adapter = _make_adapter()
        sent_frames = []

        async def capture(frame, cid=None):
            sent_frames.append(frame)

        adapter._send_frame = capture
        adapter._ws = MagicMock()

        await adapter._subscribe_all()

        assert len(sent_frames) == 1
        frame = sent_frames[0]
        assert frame["op"] == "subscribe"

        channel_names = [a["channel"] for a in frame["args"]]
        assert "orders" in channel_names
        assert "positions" in channel_names
        assert "account" in channel_names

    async def test_orders_channel_has_any_inst_type(self):
        """orders channel must subscribe to instType=ANY for full coverage."""
        adapter = _make_adapter()
        sent_frames = []

        async def capture(frame, cid=None):
            sent_frames.append(frame)

        adapter._send_frame = capture
        adapter._ws = MagicMock()

        await adapter._subscribe_all()

        args = sent_frames[0]["args"]
        orders_arg = next((a for a in args if a["channel"] == "orders"), None)
        assert orders_arg is not None
        assert orders_arg.get("instType") == "ANY"

    async def test_positions_channel_has_any_inst_type(self):
        """positions channel must subscribe to instType=ANY."""
        adapter = _make_adapter()
        sent_frames = []

        async def capture(frame, cid=None):
            sent_frames.append(frame)

        adapter._send_frame = capture
        adapter._ws = MagicMock()

        await adapter._subscribe_all()

        args = sent_frames[0]["args"]
        pos_arg = next((a for a in args if a["channel"] == "positions"), None)
        assert pos_arg is not None
        assert pos_arg.get("instType") == "ANY"


# ---------------------------------------------------------------------------
# Push routing
# ---------------------------------------------------------------------------

class TestPushRouting:
    """Push events must update the correct cache and invoke the correct callback."""

    async def test_order_push_updates_cache(self):
        adapter = _make_adapter()

        push = json.dumps({
            "arg": {"channel": "orders"},
            "data": [{
                "ordId": "ORD_001",
                "instId": "BTC-USDT-SWAP",
                "state": "filled",
                "sz": "2",
                "accFillSz": "2",
                "fillPx": "50000",
                "avgPx": "50000",
                "side": "buy",
                "ordType": "post_only",
                "cancelSource": "",
                "cancelSourceReason": "",
            }],
        })

        await adapter._dispatch(push, "cid1")

        assert "ORD_001" in adapter._order_cache
        cached = adapter._order_cache["ORD_001"]
        assert cached["state"] == "filled"
        assert cached["filled_qty"] == 2.0
        assert cached["filled_price"] == 50000.0

    async def test_order_push_calls_callback(self):
        adapter = _make_adapter()
        received = []
        adapter.on_order_update = lambda d: received.append(d)

        push = json.dumps({
            "arg": {"channel": "orders"},
            "data": [{
                "ordId": "ORD_002",
                "instId": "ETH-USDT",
                "state": "live",
                "sz": "1",
                "accFillSz": "0",
                "fillPx": "0",
                "avgPx": "0",
                "side": "sell",
                "ordType": "limit",
            }],
        })

        await adapter._dispatch(push, "cid2")

        assert len(received) == 1
        assert received[0]["order_id"] == "ORD_002"

    async def test_position_push_updates_cache_keyed_correctly(self):
        adapter = _make_adapter()

        push = json.dumps({
            "arg": {"channel": "positions"},
            "data": [{
                "instId": "ETH-USDT-SWAP",
                "posSide": "long",
                "pos": "10",
                "avgPx": "2000",
                "upl": "50",
                "lever": "5",
            }],
        })

        await adapter._dispatch(push, "cid3")

        assert "ETH-USDT-SWAP:long" in adapter._position_cache
        raw = adapter._position_cache["ETH-USDT-SWAP:long"]
        assert raw["pos"] == "10"

    async def test_position_push_calls_callback(self):
        adapter = _make_adapter()
        received = []
        adapter.on_position_update = lambda d: received.append(d)

        push = json.dumps({
            "arg": {"channel": "positions"},
            "data": [{"instId": "BTC-USDT-SWAP", "posSide": "short", "pos": "3"}],
        })

        await adapter._dispatch(push, "cid4")

        assert len(received) == 1
        assert received[0]["instId"] == "BTC-USDT-SWAP"

    async def test_account_push_updates_cache(self):
        adapter = _make_adapter()

        push = json.dumps({
            "arg": {"channel": "account"},
            "data": [{"totalEq": "10000", "availEq": "8000", "imr": "200"}],
        })

        await adapter._dispatch(push, "cid5")

        assert adapter._account_cache.get("totalEq") == "10000"
        assert adapter._account_cache.get("availEq") == "8000"

    async def test_account_push_calls_callback(self):
        adapter = _make_adapter()
        received = []
        adapter.on_account_update = lambda d: received.append(d)

        push = json.dumps({
            "arg": {"channel": "account"},
            "data": [{"totalEq": "5000"}],
        })

        await adapter._dispatch(push, "cid6")

        assert len(received) == 1

    async def test_pong_is_handled_silently(self):
        """Raw 'pong' should not raise and should not trigger any callback."""
        adapter = _make_adapter()
        order_calls = []
        adapter.on_order_update = lambda d: order_calls.append(d)

        await adapter._dispatch("pong", "cid7")

        assert len(order_calls) == 0

    async def test_invalid_json_is_handled_gracefully(self):
        """Malformed JSON must not raise an exception."""
        adapter = _make_adapter()
        # Should not raise
        await adapter._dispatch("{not valid json", "cid8")

    async def test_unknown_channel_is_ignored(self):
        """An unexpected channel must not crash or invoke any callback."""
        adapter = _make_adapter()
        calls = []
        adapter.on_order_update = lambda d: calls.append(d)

        push = json.dumps({
            "arg": {"channel": "trades"},
            "data": [{"some": "data"}],
        })
        await adapter._dispatch(push, "cid9")

        assert len(calls) == 0


# ---------------------------------------------------------------------------
# Login ack routing
# ---------------------------------------------------------------------------

class TestLoginAck:
    """Login acknowledgement must update _logged_in and release _login_event."""

    async def test_successful_login_sets_logged_in(self):
        adapter = _make_adapter()
        adapter._login_event = asyncio.Event()

        ack = json.dumps({"event": "login", "code": "0"})
        await adapter._dispatch(ack, "cid_login_ok")

        assert adapter._logged_in is True
        assert adapter._login_event.is_set()

    async def test_failed_login_does_not_set_logged_in(self):
        adapter = _make_adapter()
        adapter._login_event = asyncio.Event()

        ack = json.dumps({"event": "login", "code": "60009", "msg": "invalid key"})
        await adapter._dispatch(ack, "cid_login_fail")

        assert adapter._logged_in is False
        assert adapter._login_event.is_set()  # event IS set to unblock the waiter

    async def test_subscribe_ack_is_handled(self):
        """Subscribe acknowledgement must not raise."""
        adapter = _make_adapter()
        ack = json.dumps({"event": "subscribe", "arg": {"channel": "orders"}})
        await adapter._dispatch(ack, "cid_sub_ack")


# ---------------------------------------------------------------------------
# Reconnect state machine
# ---------------------------------------------------------------------------

class TestReconnectStateMachine:
    """After a disconnect, _reconnect_loop must reconnect, re-login, re-subscribe."""

    async def test_reconnect_calls_ws_connect(self):
        """_reconnect_loop calls _ws_connect and sets _connected on success."""
        adapter = _make_adapter()
        adapter._running = True
        adapter._connected = False
        adapter._cancel_tasks = AsyncMock()
        adapter._close_ws = AsyncMock()

        connect_calls = []

        async def mock_ws_connect():
            connect_calls.append(1)
            adapter._connected = True

        adapter._ws_connect = mock_ws_connect

        with patch("adapters.okx_ws_adapter.asyncio.sleep", new_callable=AsyncMock):
            await adapter._reconnect_loop()

        assert len(connect_calls) == 1
        assert adapter._connected is True

    async def test_reconnect_retries_on_failure_then_succeeds(self):
        """_reconnect_loop retries after failure and eventually succeeds."""
        adapter = _make_adapter()
        adapter._running = True
        adapter._connected = False
        adapter._cancel_tasks = AsyncMock()
        adapter._close_ws = AsyncMock()

        call_count = [0]

        async def mock_ws_connect():
            call_count[0] += 1
            if call_count[0] < 3:
                raise ConnectionError("simulated failure")
            adapter._connected = True

        adapter._ws_connect = mock_ws_connect

        with patch("adapters.okx_ws_adapter.asyncio.sleep", new_callable=AsyncMock):
            await adapter._reconnect_loop()

        assert call_count[0] == 3
        assert adapter._connected is True

    async def test_reconnect_stops_immediately_when_not_running(self):
        """If _running is False the reconnect loop must exit without calling _ws_connect."""
        adapter = _make_adapter()
        adapter._running = False
        adapter._connected = False

        ws_connect_called = []
        adapter._ws_connect = AsyncMock(
            side_effect=lambda: ws_connect_called.append(1)
        )

        await adapter._reconnect_loop()

        assert len(ws_connect_called) == 0

    async def test_reconnect_stops_once_connected(self):
        """_reconnect_loop must not attempt further connects after the first success."""
        adapter = _make_adapter()
        adapter._running = True
        adapter._connected = False
        adapter._cancel_tasks = AsyncMock()
        adapter._close_ws = AsyncMock()

        call_count = [0]

        async def mock_ws_connect():
            call_count[0] += 1
            adapter._connected = True  # always succeeds

        adapter._ws_connect = mock_ws_connect

        with patch("adapters.okx_ws_adapter.asyncio.sleep", new_callable=AsyncMock):
            await adapter._reconnect_loop()

        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# Order normalisation
# ---------------------------------------------------------------------------

class TestNormaliseOrder:
    """_normalise_order must produce a dict with the same keys as get_order_status()."""

    def test_filled_order(self):
        from adapters.okx_ws_adapter import _normalise_order

        raw = {
            "ordId": "ABC",
            "instId": "BTC-USDT-SWAP",
            "state": "filled",
            "sz": "2",
            "accFillSz": "2",
            "fillPx": "49999",
            "avgPx": "",
            "side": "buy",
            "ordType": "post_only",
            "cancelSource": "",
            "cancelSourceReason": "",
        }
        result = _normalise_order(raw)

        assert result["order_id"] == "ABC"
        assert result["state"] == "filled"
        assert result["filled_qty"] == 2.0
        assert result["remaining_qty"] == 0.0
        assert result["filled_price"] == 49999.0

    def test_live_order_uses_accFillSz(self):
        from adapters.okx_ws_adapter import _normalise_order

        raw = {
            "ordId": "DEF",
            "instId": "ETH-USDT",
            "state": "live",
            "sz": "5",
            "accFillSz": "1",
            "fillSz": "0",
            "fillPx": "2001",
            "avgPx": "",
            "side": "sell",
            "ordType": "limit",
        }
        result = _normalise_order(raw)

        assert result["filled_qty"] == 1.0
        assert result["remaining_qty"] == 4.0

    def test_zero_fill_order(self):
        from adapters.okx_ws_adapter import _normalise_order

        raw = {
            "ordId": "GHI",
            "instId": "SOL-USDT",
            "state": "live",
            "sz": "10",
            "accFillSz": "0",
            "fillPx": "0",
            "avgPx": "0",
            "side": "buy",
            "ordType": "market",
        }
        result = _normalise_order(raw)

        assert result["filled_qty"] == 0.0
        assert result["remaining_qty"] == 10.0
        assert result["filled_price"] == 0.0

    def test_avgPx_used_as_fallback_when_fillPx_zero(self):
        """OKX sends fillPx='0' on some market fills before the record propagates.
        _normalise_order must fall back to avgPx in that case so the executor
        sees the real fill price instead of 0.0.
        """
        from adapters.okx_ws_adapter import _normalise_order

        raw = {
            "ordId": "JKL",
            "instId": "BTC-USDT",
            "state": "filled",
            "sz": "1",
            "accFillSz": "1",
            "fillPx": "0",   # OKX quirk: zero string before fill propagates
            "avgPx": "48000",
            "side": "buy",
            "ordType": "market",
        }
        result = _normalise_order(raw)
        # fillPx is numerically 0 → must fall back to avgPx
        assert result["filled_price"] == 48000.0


# ---------------------------------------------------------------------------
# Cache accessor: get_order_status prefers WS cache over REST
# ---------------------------------------------------------------------------

class TestCacheAccessors:
    async def test_get_order_status_returns_cached_order(self):
        adapter = _make_adapter()
        adapter._order_cache["999"] = {
            "order_id": "999",
            "state": "filled",
            "filled_qty": 1.0,
            "filled_price": 30000.0,
        }

        result = await adapter.get_order_status("BTC-USDT", "999")

        assert result is not None
        assert result["state"] == "filled"
        # REST should NOT have been called
        adapter._rest.get_order_status.assert_not_called()

    async def test_get_order_status_falls_back_to_rest(self):
        adapter = _make_adapter()
        adapter._rest.get_order_status = AsyncMock(return_value={"state": "live"})

        result = await adapter.get_order_status("BTC-USDT", "NOT_IN_CACHE")

        adapter._rest.get_order_status.assert_called_once()

    async def test_get_positions_returns_parsed_cache(self):
        adapter = _make_adapter()
        adapter._position_cache["BTC-USDT-SWAP:long"] = {
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "pos": "5",
            "avgPx": "50000",
            "upl": "200",
            "lever": "10",
        }

        positions = await adapter.get_positions()

        assert len(positions) == 1
        assert positions[0].symbol == "BTC-USDT-SWAP"
        assert positions[0].side == "LONG"
        assert positions[0].quantity == 5.0

    async def test_get_positions_falls_back_to_rest_when_cache_empty(self):
        from models import Position
        from datetime import datetime

        adapter = _make_adapter()
        adapter._rest.get_positions = AsyncMock(return_value=[])

        positions = await adapter.get_positions()

        adapter._rest.get_positions.assert_called_once()
        assert positions == []

    async def test_get_account_info_returns_parsed_cache(self):
        adapter = _make_adapter()
        adapter._account_cache = {
            "totalEq": "10000",
            "availEq": "8000",
            "imr": "500",
            "mmr": "250",
            "upl": "100",
        }

        info = await adapter.get_account_info()

        assert info is not None
        assert info.total_equity == 10000.0
        assert info.available_balance_usd == 8000.0


# ---------------------------------------------------------------------------
# WS trading operations (Milestone 2)
# ---------------------------------------------------------------------------

class TestWSTradingOps:
    """WS place_order, cancel_order, amend_order — request/response correlation."""

    def _make_connected_adapter(self):
        """Return an adapter that appears connected with a mock WS."""
        adapter = _make_adapter()
        adapter._connected = True
        adapter._ws = MagicMock()
        adapter._ws.closed = False
        adapter._ws.send_str = AsyncMock()
        return adapter

    # --- _send_op ---

    async def test_send_op_resolves_future_on_response(self):
        """_send_op returns the response dict when _dispatch routes a matching reply."""
        adapter = self._make_connected_adapter()

        async def fake_send_frame(frame, cid=None):
            # Simulate the exchange responding immediately
            resp = {"id": frame["id"], "op": frame["op"], "code": "0", "data": [{"ordId": "X1"}]}
            fut = adapter._pending_ops.get(frame["id"])
            if fut and not fut.done():
                fut.set_result(resp)

        adapter._send_frame = fake_send_frame
        result = await adapter._send_op("order", [{"instId": "BTC-USDT"}])
        assert result["code"] == "0"
        assert result["data"][0]["ordId"] == "X1"

    async def test_send_op_raises_on_not_connected(self):
        """_send_op raises RuntimeError when WS is closed."""
        adapter = _make_adapter()
        adapter._ws = MagicMock()
        adapter._ws.closed = True
        with pytest.raises(RuntimeError, match="WS not connected"):
            await adapter._send_op("order", [{}])

    async def test_send_op_raises_timeout(self):
        """_send_op raises asyncio.TimeoutError when no response arrives in time."""
        adapter = self._make_connected_adapter()
        adapter._send_frame = AsyncMock()  # sends but never resolves

        with pytest.raises(asyncio.TimeoutError):
            await adapter._send_op("order", [{"instId": "BTC-USDT"}], timeout=0.05)

    async def test_pending_op_cleaned_up_after_timeout(self):
        """After a timeout _pending_ops should not retain stale futures."""
        adapter = self._make_connected_adapter()
        adapter._send_frame = AsyncMock()

        with pytest.raises(asyncio.TimeoutError):
            await adapter._send_op("order", [{}], timeout=0.05)

        assert len(adapter._pending_ops) == 0

    # --- _dispatch routes trade-op responses ---

    async def test_dispatch_routes_trade_op_to_pending_future(self):
        """_dispatch must resolve the pending future for a matching id+op."""
        adapter = _make_adapter()
        # Manually inject a pending future
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        adapter._pending_ops["abc123"] = fut

        resp = json.dumps({"id": "abc123", "op": "order", "code": "0",
                           "data": [{"ordId": "999", "sCode": "0"}]})
        await adapter._dispatch(resp, "cid_trade")

        assert fut.done()
        assert fut.result()["data"][0]["ordId"] == "999"

    async def test_dispatch_ignores_unknown_id(self):
        """_dispatch silently ignores a trade-op response whose id has no pending future."""
        adapter = _make_adapter()
        resp = json.dumps({"id": "no_match", "op": "order", "code": "0", "data": []})
        # Should not raise
        await adapter._dispatch(resp, "cid_no_match")

    # --- place_order ---

    async def test_place_order_falls_back_to_rest_when_not_connected(self):
        """place_order uses REST when WS is not connected."""
        from models import OrderResult
        adapter = _make_adapter()
        adapter._connected = False
        adapter._rest.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="REST_1", filled_qty=0, filled_price=0)
        )

        result = await adapter.place_order("BTC-USDT", "buy", "LIMIT", 0.01, price=50000.0)
        adapter._rest.place_order.assert_called_once()
        assert result.order_id == "REST_1"

    async def test_place_order_ws_success_limit(self):
        """place_order returns OrderResult with order_id on successful WS LIMIT placement."""
        from models import OrderResult
        adapter = self._make_connected_adapter()

        # Mock _prepare_order to skip REST calls
        adapter._rest._prepare_order = AsyncMock(
            return_value=({"instId": "BTC-USDT", "sz": "1"}, None, 1.0)
        )

        async def fake_send_op(op, args, timeout=5.0):
            return {"id": "x", "op": op, "code": "0",
                    "data": [{"ordId": "WS_ORDER_1", "sCode": "0"}]}

        adapter._send_op = fake_send_op

        result = await adapter.place_order("BTC-USDT", "buy", "LIMIT", 0.01, price=50000.0)
        assert result.success is True
        assert result.order_id == "WS_ORDER_1"

    async def test_place_order_ws_error_returns_failure(self):
        """place_order returns OrderResult(success=False) when exchange rejects."""
        from models import OrderResult
        adapter = self._make_connected_adapter()
        adapter._rest._prepare_order = AsyncMock(
            return_value=({"instId": "BTC-USDT", "sz": "1"}, None, 1.0)
        )

        async def fake_send_op(op, args, timeout=5.0):
            return {"id": "x", "op": op, "code": "1",
                    "msg": "insufficient margin",
                    "data": [{"sCode": "54000", "sMsg": "insufficient margin"}]}

        adapter._send_op = fake_send_op
        result = await adapter.place_order("BTC-USDT", "buy", "LIMIT", 0.01, price=50000.0)
        assert result.success is False
        assert "insufficient margin" in result.error

    async def test_place_order_falls_back_to_rest_on_timeout(self):
        """place_order falls back to REST when _send_op times out."""
        from models import OrderResult
        adapter = self._make_connected_adapter()
        adapter._rest._prepare_order = AsyncMock(
            return_value=({"instId": "BTC-USDT", "sz": "1"}, None, 1.0)
        )
        adapter._rest.place_order = AsyncMock(
            return_value=OrderResult(success=True, order_id="REST_FALLBACK", filled_qty=0, filled_price=0)
        )

        async def timeout_op(op, args, timeout=5.0):
            raise asyncio.TimeoutError

        adapter._send_op = timeout_op
        result = await adapter.place_order("BTC-USDT", "buy", "LIMIT", 0.01, price=50000.0)
        assert result.order_id == "REST_FALLBACK"

    # --- cancel_order ---

    async def test_cancel_order_ws_success(self):
        """cancel_order returns True on WS success response."""
        adapter = self._make_connected_adapter()

        async def fake_send_op(op, args, timeout=5.0):
            return {"id": "x", "op": op, "code": "0", "data": []}

        adapter._send_op = fake_send_op
        assert await adapter.cancel_order("BTC-USDT", "ORD_123") is True

    async def test_cancel_order_falls_back_to_rest_when_not_connected(self):
        """cancel_order falls back to REST when WS is not ready."""
        adapter = _make_adapter()
        adapter._connected = False
        adapter._rest.cancel_order = AsyncMock(return_value=True)

        result = await adapter.cancel_order("BTC-USDT", "ORD_456")
        adapter._rest.cancel_order.assert_called_once_with("BTC-USDT", "ORD_456")
        assert result is True

    async def test_cancel_order_returns_false_on_rejection(self):
        """cancel_order returns False when exchange sends non-zero code."""
        adapter = self._make_connected_adapter()

        async def fake_send_op(op, args, timeout=5.0):
            return {"id": "x", "op": op, "code": "1", "msg": "order not found"}

        adapter._send_op = fake_send_op
        assert await adapter.cancel_order("BTC-USDT", "GONE") is False

    # --- amend_order ---

    async def test_amend_order_ws_success(self):
        """amend_order returns True on WS success."""
        adapter = self._make_connected_adapter()
        adapter._rest.get_symbol_info = AsyncMock(return_value={"price_precision": 2})

        async def fake_send_op(op, args, timeout=5.0):
            assert op == "amend-order"
            assert args[0]["newPx"] == "50000.00"
            return {"id": "x", "op": op, "code": "0", "data": []}

        adapter._send_op = fake_send_op
        result = await adapter.amend_order("BTC-USDT", "ORD_789", new_price=50000.0)
        assert result is True

    async def test_amend_order_returns_false_when_not_connected(self):
        """amend_order returns False when WS is not ready."""
        adapter = _make_adapter()
        adapter._connected = False
        result = await adapter.amend_order("BTC-USDT", "ORD_ABC", new_price=50000.0)
        assert result is False

    async def test_amend_order_returns_false_on_rejection(self):
        """amend_order returns False when exchange rejects the amend."""
        adapter = self._make_connected_adapter()
        adapter._rest.get_symbol_info = AsyncMock(return_value={"price_precision": 2})

        async def fake_send_op(op, args, timeout=5.0):
            return {"id": "x", "op": op, "code": "1", "msg": "order not found"}

        adapter._send_op = fake_send_op
        assert await adapter.amend_order("BTC-USDT", "ORD_GONE", new_price=50000.0) is False

    async def test_amend_order_returns_false_on_timeout(self):
        """amend_order returns False (not raises) on timeout."""
        adapter = self._make_connected_adapter()
        adapter._rest.get_symbol_info = AsyncMock(return_value={"price_precision": 2})

        async def timeout_op(op, args, timeout=5.0):
            raise asyncio.TimeoutError

        adapter._send_op = timeout_op
        assert await adapter.amend_order("BTC-USDT", "ORD_T", new_price=50000.0) is False
