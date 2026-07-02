"""
OKX RFQ (Request for Quote) REST adapter — atomic multi-leg execution.

OKX RFQ API: https://www.okx.com/docs-v5/en/#rfq
Auth: identical to OKXAdapter (HMAC-SHA256, ISO timestamp, per-request signing).

Flow:
  1. create_rfq()   → rfqId   (submit both legs to market makers)
  2. get_quotes()   → list[RFQQuote]  (market makers respond with prices)
  3. execute_quote()→ trade_data  (atomic fill of ALL legs simultaneously)
  4. cancel_rfq()   → bool    (clean up stale RFQs if no quotes received)

Minimum notional is set by OKX per instrument tier; for BTC/ETH-USDT-SWAP
this is typically $50k–$500k per leg. Attempting an RFQ below minimum will
return an error from create_rfq (handled gracefully — caller falls back).
"""
import asyncio
import hmac
import hashlib
import base64
import json
import logging
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Re-measure the OKX clock offset at least this often so a drifting host clock
# never accumulates past OKX's ±30s signing window between RFQ requests.
_CLOCK_RESYNC_SEC = 120.0


class RFQQuote:
    """
    A single quote received from an OKX market maker in response to an RFQ.

    legs: OKX returns per-leg pricing with instId, side, px, sz, fee, feeCcy.
    """
    def __init__(self, data: Dict[str, Any]):
        self.quote_id: str = data.get("quoteId", "")
        self.rfq_id: str = data.get("rfqId", "")
        self.state: str = data.get("state", "")
        self.exp_time: str = data.get("expTime", "")
        self.legs: List[Dict[str, Any]] = data.get("legs", [])

    def price_for(self, inst_id: str) -> float:
        """Return the quoted price for a specific instrument, or 0.0."""
        for leg in self.legs:
            if leg.get("instId") == inst_id:
                return float(leg.get("px", 0.0) or 0.0)
        return 0.0

    def size_for(self, inst_id: str) -> str:
        """Return the maker's quoted size (contracts) for an instrument as a
        string, or '' if absent. Executing 'as-is' means echoing the quote's own
        size rather than re-deriving it from our request."""
        for leg in self.legs:
            if leg.get("instId") == inst_id:
                sz = leg.get("sz")
                return str(sz) if sz not in (None, "") else ""
        return ""

    def is_active(self) -> bool:
        return self.state == "active"

    def __repr__(self) -> str:
        prices = ", ".join(
            f"{l.get('instId')} {l.get('side')}@{l.get('px')}" for l in self.legs
        )
        return f"RFQQuote(id={self.quote_id}, state={self.state}, [{prices}])"


class OKXRFQAdapter:
    """
    REST adapter for OKX RFQ — creates RFQs, collects quotes, executes atomically.

    Uses the same authentication scheme as OKXAdapter:
    - HMAC-SHA256 over (timestamp + method + path + body)
    - ISO timestamp, base64-encoded signature
    - x-simulated-trading: 1  for demo/paper accounts
    """
    BASE_URL = "https://www.okx.com"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        is_testnet: bool = False,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.is_testnet = is_testnet
        self._session = None  # aiohttp.ClientSession — created lazily
        # Signed timestamps are corrected by this measured offset so a drifted
        # host clock doesn't trigger OKX 50102 "Timestamp request expired".
        self._clock_offset_s: float = 0.0
        self._last_clock_sync: float = 0.0

    # ------------------------------------------------------------------ auth

    async def _sync_clock(self) -> None:
        """Measure local-vs-OKX clock offset so signed RFQ timestamps are
        server-relative. /api/v5/public/time is unauthenticated, so it works
        even when the host clock is the problem."""
        import aiohttp
        # Stamp FIRST so a concurrent caller doesn't also fire a sync.
        self._last_clock_sync = time.time()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(self.BASE_URL + "/api/v5/public/time",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    d = await r.json()
                    server_ms = float(d["data"][0]["ts"])
                    self._clock_offset_s = time.time() - server_ms / 1000.0
                    if abs(self._clock_offset_s) > 5:
                        logger.warning(
                            "[rfq_adapter] clock offset %.1fs vs OKX server — "
                            "RFQ timestamps auto-corrected.", self._clock_offset_s)
        except Exception as e:
            logger.debug("[rfq_adapter] clock sync failed (non-fatal): %s", e)

    def _get_timestamp(self) -> str:
        # Corrected by the measured server-clock offset so a drifted host clock
        # doesn't cause OKX 50102 "Timestamp request expired".
        now = datetime.utcfromtimestamp(time.time() - self._clock_offset_s)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + now.strftime("%f")[:3] + "Z"

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        message = timestamp + method + path + body
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode()

    def _get_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        ts = self._get_timestamp()
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.is_testnet:
            headers["x-simulated-trading"] = "1"
        return headers

    # --------------------------------------------------------------- request

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout_sec: float = 15.0,
        reraise_timeout: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Single signed request with basic error logging and a hard timeout so a
        hung connection can't block the engine indefinitely.

        Returns the full response dict on success (code="0"), None on failure.
        reraise_timeout=True re-raises asyncio.TimeoutError so a caller (e.g.
        execute_quote) can treat a timeout as AMBIGUOUS rather than a clean
        failure — the trade may have executed on the exchange.
        """
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession()

        # Keep the clock offset fresh (drift guard) before signing.
        if time.time() - self._last_clock_sync > _CLOCK_RESYNC_SEC:
            await self._sync_clock()

        full_path = path
        if params:
            full_path = path + "?" + "&".join(f"{k}={v}" for k, v in params.items())

        url = self.BASE_URL + full_path
        body = json.dumps(data) if data else ""

        # One retry: a 50102 "timestamp expired" means the clock drifted since
        # the last sync — resync and re-sign. RFQ create/execute are NOT retried
        # blindly elsewhere, but a 50102 is rejected by OKX before matching, so
        # re-signing the SAME request is safe.
        for attempt in range(2):
            headers = self._get_headers(method, full_path, body)
            try:
                async with self._session.request(
                    method, url, headers=headers, data=body or None,
                    timeout=aiohttp.ClientTimeout(total=timeout_sec),
                ) as resp:
                    result = await resp.json()
                    if result.get("code") == "50102" and attempt == 0:
                        logger.warning("[rfq_adapter] 50102 timestamp expired on %s %s "
                                       "— resyncing clock, retrying", method, path)
                        await self._sync_clock()
                        continue
                    if result.get("code") != "0":
                        logger.error(
                            "[rfq_adapter] %s %s — code=%s msg=%s data=%s",
                            method, path,
                            result.get("code"), result.get("msg"),
                            result.get("data"),
                        )
                        return None
                    return result
            except asyncio.TimeoutError:
                logger.error("[rfq_adapter] TIMEOUT %s %s after %ss", method, path, timeout_sec)
                if reraise_timeout:
                    raise
                return None
            except Exception as exc:
                logger.error("[rfq_adapter] request error %s %s: %s", method, path, exc)
                return None

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ RFQ

    async def get_counterparties(self) -> List[str]:
        """Return available block-trading counterparty codes (makers).

        create-rfq is NOT broadcast-to-all — an RFQ only reaches the makers you
        name, so these codes must be passed to create_rfq(counterparties=...).
        Returns the trader codes; empty list if none are available.
        """
        result = await self._request("GET", "/api/v5/rfq/counterparties")
        if not result:
            return []
        codes: List[str] = []
        for cp in result.get("data", []):
            code = cp.get("traderCode") or cp.get("traderName") or ""
            if code:
                codes.append(code)
        logger.info("[rfq_adapter] %d counterparties available", len(codes))
        return codes

    async def create_rfq(
        self,
        legs: List[Dict[str, Any]],
        anonymous: bool = True,
        counterparties: Optional[List[str]] = None,
        cl_rfq_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Submit a new RFQ to OKX and return the rfqId.

        legs format (one entry per instrument):
          {"instId": "ETH-USDT-SWAP", "sz": "1.1", "side": "buy",
           "tdMode": "cross", "posSide": "long"}

        anonymous=True hides your identity from market makers.
        counterparties=None broadcasts to all available makers.
        """
        payload: Dict[str, Any] = {
            "anonymous": "true" if anonymous else "false",
            "legs": legs,
        }
        if counterparties:
            payload["counterparties"] = counterparties
        if cl_rfq_id:
            payload["clRfqId"] = cl_rfq_id

        result = await self._request("POST", "/api/v5/rfq/create-rfq", data=payload)
        if not result:
            return None

        data_list = result.get("data") or [{}]
        rfq_id = data_list[0].get("rfqId", "") if data_list else ""
        if rfq_id:
            logger.info(
                "[rfq_adapter] RFQ created: rfqId=%s anonymous=%s legs=%d",
                rfq_id, anonymous, len(legs),
            )
        return rfq_id or None

    async def get_quotes(self, rfq_id: str) -> List[RFQQuote]:
        """
        Fetch active quotes for the given rfqId.
        Returns an empty list if none have arrived yet.
        """
        result = await self._request(
            "GET", "/api/v5/rfq/quotes",
            params={"rfqId": rfq_id, "state": "active"},
        )
        if not result:
            return []
        return [RFQQuote(q) for q in result.get("data", [])]

    async def execute_quote(
        self,
        rfq_id: str,
        quote_id: str,
        legs: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a specific quote — fills ALL legs atomically in one matching event.

        legs must include:
          instId, sz, side, px (from the quote), posSide (if applicable)

        Returns the trade_data dict (with tTradeId and filled legs) on success.
        On a network TIMEOUT the outcome is UNKNOWN — the exchange may have
        filled — so we return {"_ambiguous": True} instead of None. The caller
        must NOT fall back to the order book in that case (double-execution
        risk); it should reconcile against actual positions.
        """
        payload: Dict[str, Any] = {
            "rfqId": rfq_id,
            "quoteId": quote_id,
            "legs": legs,
        }
        try:
            result = await self._request(
                "POST", "/api/v5/rfq/execute-quote", data=payload, reraise_timeout=True,
            )
        except asyncio.TimeoutError:
            logger.critical(
                "[rfq_adapter] execute-quote TIMED OUT — trade state UNKNOWN; do NOT "
                "blindly retry/fall back, reconcile positions. rfqId=%s quoteId=%s",
                rfq_id, quote_id,
            )
            return {"_ambiguous": True}
        if not result:
            return None

        data_list = result.get("data") or [{}]
        trade = data_list[0] if data_list else {}
        if trade.get("tTradeId"):
            logger.info(
                "[rfq_adapter] executed: rfqId=%s quoteId=%s tTradeId=%s",
                rfq_id, quote_id, trade["tTradeId"],
            )
        return trade or None

    async def cancel_rfq(self, rfq_id: str) -> bool:
        """Cancel an active RFQ to prevent stale quotes from reaching execution."""
        result = await self._request(
            "POST", "/api/v5/rfq/cancel-rfq",
            data={"rfqId": rfq_id},
        )
        if result:
            logger.info("[rfq_adapter] cancelled rfqId=%s", rfq_id)
        return result is not None
