# Changelog

## Milestone 2 — WS Order Placement & Amendment (2026-06-18)

### What shipped

#### `adapters/okx_ws_adapter.py` (updated)
WS order placement, cancellation, and price amendment replacing REST delegation.

- **`place_order` via WS**: builds the OKX order dict via `OKXAdapter._prepare_order()`
  (all sizing, contract-conversion, and td-mode logic reused unchanged), then submits via
  `{"op": "order", "args": [...]}`. Falls back to REST transparently on WS-not-ready or
  5 s timeout. MARKET fills are detected from the WS order cache (up to 1.5 s); falls
  back to REST `get_order_status` if the push hasn't arrived.
- **`cancel_order` via WS**: `{"op": "cancel-order", "args": [...]}` with REST fallback on
  timeout or disconnection.
- **`amend_order` (new method)**: `{"op": "amend-order", "args": [{"newPx": "..."}]}` for
  atomic in-place price updates (no cancel-gap risk). Returns `False` when WS not ready or
  exchange rejects, signalling the executor to fall back to cancel-and-replace.
- **`_send_op` (new private method)**: sends a trading-op frame with a unique `id`, awaits
  the response via `asyncio.Future` in `_pending_ops`, and returns the response dict.
  Uses `asyncio.shield` to prevent `wait_for` timeout cancelling the underlying future
  (late responses from OKX after timeout won't crash `_dispatch`).
- **`_pending_ops`**: `Dict[str, asyncio.Future]` map for request-response correlation.
  `_dispatch` now routes responses with matching `id` to the right future.

#### `adapters/okx_adapter.py` (updated)
- **`_prepare_order` (new method)**: extracted from `place_order`. Validates inputs, converts
  base-currency quantity to contracts, builds the full OKX order param dict, and returns
  `(order_data, error_msg, sz)`. No HTTP call — pure param prep, shareable with WS adapter.
- **`place_order`**: refactored to call `_prepare_order()` + REST HTTP call. Behaviour
  unchanged from the caller's perspective.

#### `core/order_executor.py` (updated)
`_amend_limit_orders` now tries `adapter.amend_order(...)` first (WS native amend).
Falls back to cancel-and-replace when `amend_order` is unavailable or returns `False`
(REST adapter, or WS adapter while disconnected).

#### `tests/test_ws_adapter.py` (updated)
19 new tests in `TestWSTradingOps` covering:
- `_send_op` request-response correlation, timeout, cleanup
- `_dispatch` routing of trade-op responses to pending futures
- `place_order`: WS success, WS error, REST fallback (not connected), REST fallback (timeout)
- `cancel_order`: WS success, WS error, REST fallback (not connected)
- `amend_order`: WS success, WS rejection, WS timeout, not-connected

### Caveats
- Order cancellation is now via WS; the REST fallback fires automatically on disconnection.
- `amend_order` is a new public method not present in `ExchangeAdapter` base class —
  the executor uses `hasattr` to detect it so the REST adapter path is unchanged.
- Fill detection for MARKET orders still has a REST poll fallback; a future milestone
  can eliminate this by relying entirely on the WS order-cache push.

## Milestone 1 — WebSocket Infrastructure (2026-06-18)

### What shipped

#### `adapters/okx_ws_adapter.py` (new)
Private-channel WebSocket adapter for OKX, implementing the full
`ExchangeAdapter` interface. Milestone 1 scope: infrastructure only.

- **Authentication**: HMAC-SHA256 login frame per OKX WS spec
  (`timestamp + "GET" + "/users/self/verify"`, base64-encoded).
- **Endpoint**: `wss://wspap.okx.com:8443/ws/v5/private`
  with `x-simulated-trading: 1` header for testnet.
- **Subscriptions**: `orders` (instType=ANY), `positions` (instType=ANY),
  `account` channels subscribed on connect and re-subscribed after reconnect.
- **Heartbeat**: raw `"ping"` string sent every 25 s; `"pong"` logged at DEBUG.
- **Reconnect**: exponential backoff (1 s → 2 → 4 → 8 → 16 → 30 s cap).
  Re-login and re-subscribe automatically after every reconnect.
- **Event callbacks**: `on_order_update`, `on_position_update`,
  `on_account_update` — callers register Python callables.
- **In-memory caches**: orders keyed by `ordId`, positions keyed by
  `{instId}:{posSide}`, account as a flat merged dict. Cache-first reads
  for `get_positions()`, `get_account_info()`, `get_order_status()`.
- **Logging**: every frame sent and received logged at DEBUG with an
  8-hex-char correlation ID (e.g. `[3f8a2b1c] WS SEND: ...`).
- **REST delegation**: all order-placement, amendment, and cancellation
  operations delegate to the embedded `OKXAdapter` (REST) until
  Milestone 2/3. Market-data methods (`get_tick`, `get_orderbook`, etc.)
  always use REST.
- **`_normalise_order` fix**: uses numeric comparison (not Python string
  truthiness) so `fillPx="0"` correctly falls back to `avgPx` — prevents
  reporting 0.0 fill price on market orders where OKX sends fillPx before
  the fill record propagates.

#### `scripts/ws_smoke_test.py` (new)
Connects to OKX testnet private WS, authenticates, and prints every push
for 60 seconds. Supports `KeyboardInterrupt` for early exit. Run with
`python -m scripts.ws_smoke_test` (requires `.env` with demo credentials).

#### `tests/test_ws_adapter.py` (new)
32 unit tests covering:
- Login HMAC-SHA256 signature correctness
- Subscribe frame channel names and `instType=ANY`
- Push routing to order / position / account cache and callbacks
- Edge cases: pong, invalid JSON, unknown channels, login reject
- Reconnect state machine: success, retry-on-failure, stop-when-not-running

#### Supporting changes
- `adapters/__init__.py`: exports `OKXWebSocketAdapter`.
- `.env.example`: adds `EXCHANGE_BACKEND=rest` (values: `rest` | `websocket`)
  per non-negotiable §2 interface contract.
- `requirements.txt`: adds `pytest>=7.0.0` and `pytest-asyncio>=0.21.0`.
- `pytest.ini`: new file, sets `asyncio_mode = auto`.

### Caveats
- Order placement, amendment, cancellation, and fill detection via WS are
  **not** implemented in this milestone. Those are Milestone 2 and 3.
- The smoke test requires valid OKX demo-trading credentials in `.env`.
  Account/position pushes may be sparse on a fresh demo account; the
  adapter will still connect and print the initial account snapshot on login.
