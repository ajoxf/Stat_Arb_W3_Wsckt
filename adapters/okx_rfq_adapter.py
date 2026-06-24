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
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


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

    # ------------------------------------------------------------------ auth

    def _get_timestamp(self) -> str:
        now = datetime.utcnow()
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
    ) -> Optional[Dict[str, Any]]:
        """
        Single signed request with basic error logging.
        Returns the full response dict on success (code="0"), None on failure.
        """
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession()

        full_path = path
        if params:
            full_path = path + "?" + "&".join(f"{k}={v}" for k, v in params.items())

        url = self.BASE_URL + full_path
        body = json.dumps(data) if data else ""
        headers = self._get_headers(method, full_path, body)

        try:
            async with self._session.request(
                method, url, headers=headers, data=body or None
            ) as resp:
                result = await resp.json()
                if result.get("code") != "0":
                    logger.error(
                        "[rfq_adapter] %s %s — code=%s msg=%s data=%s",
                        method, path,
                        result.get("code"), result.get("msg"),
                        result.get("data"),
                    )
                    return None
                return result
        except Exception as exc:
            logger.error("[rfq_adapter] request error %s %s: %s", method, path, exc)
            return None

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ RFQ

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
        """
        payload: Dict[str, Any] = {
            "rfqId": rfq_id,
            "quoteId": quote_id,
            "legs": legs,
        }
        result = await self._request("POST", "/api/v5/rfq/execute-quote", data=payload)
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
