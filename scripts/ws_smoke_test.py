"""
OKX private-channel WebSocket smoke test.

Connects to OKX testnet (wss://wspap.okx.com:8443/ws/v5/private),
authenticates, subscribes to orders / positions / account channels,
prints every push received for 60 seconds, then exits cleanly.

Run:
    python -m scripts.ws_smoke_test

Prerequisites (in .env):
    OKX_API_KEY       — demo-trading API key
    OKX_SECRET_KEY    — demo-trading secret
    OKX_PASSPHRASE    — demo-trading passphrase
    OKX_DEMO_MODE=true  (or OKX_API_KEY etc. set to demo values)

Reconnect test:
    Disconnect your network for 10+ seconds mid-run.
    The adapter should reconnect, re-login, and re-subscribe automatically
    — watch for "[reconnect] attempt N" log lines followed by
    "[ws_adapter] ready — connected, authenticated, subscribed".
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Allow both `python scripts/ws_smoke_test.py` and `python -m scripts.ws_smoke_test`
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",  # %f is not supported by Windows strftime
)
# Quiet noisy libraries
logging.getLogger("aiohttp").setLevel(logging.WARNING)

from adapters.okx_ws_adapter import OKXWebSocketAdapter  # noqa: E402


RUN_SECONDS = 60


def _ts() -> str:
    return datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]


def _print_push(kind: str, data: object) -> None:
    print(f"[{_ts()}] PUSH {kind.upper():10s} {data}", flush=True)


async def main() -> None:
    api_key = os.getenv("OKX_API_KEY", "")
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

    # Register push callbacks
    adapter.on_order_update    = lambda d: _print_push("order",    d)
    adapter.on_position_update = lambda d: _print_push("position", d)
    adapter.on_account_update  = lambda d: _print_push("account",  d)

    print(f"[{_ts()}] Connecting to OKX testnet private WS …")
    connected = await adapter.connect()
    if not connected:
        print(f"[{_ts()}] ERROR: connect() returned False — {adapter.last_error}", file=sys.stderr)
        sys.exit(1)

    print(
        f"[{_ts()}] Connected and authenticated. "
        f"Listening for {RUN_SECONDS} s. "
        f"(Disconnect network briefly to test auto-reconnect.)",
        flush=True,
    )

    try:
        await asyncio.sleep(RUN_SECONDS)
    except KeyboardInterrupt:
        print(f"\n[{_ts()}] Interrupted by user")
    finally:
        print(f"[{_ts()}] Disconnecting …")
        await adapter.disconnect()
        print(f"[{_ts()}] Done.")


if __name__ == "__main__":
    asyncio.run(main())
