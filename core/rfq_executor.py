"""
RFQ Executor — atomic spread execution via OKX Request for Quote.

Called by OrderExecutor when per-leg notional >= config.rfq_notional_threshold_usd.

Returns RFQResult (a plain dataclass) so OrderExecutor can populate its
SpreadOrder without any circular import. OrderExecutor maps fills back onto
the SpreadOrder after this call returns.

Execution flow:
  1. Build RFQ legs from the SpreadOrder (instId, side, sz, tdMode, posSide)
  2. create_rfq() → rfqId
  3. Poll get_quotes() every 0.5s until min_quotes received or timeout
  4. Select best quote: lowest total slippage vs current mid prices
  5. execute_quote() → atomic fill
  6. Parse filled prices from trade_data, populate RFQResult
  7. On any failure → RFQResult(success=False); caller falls back to order book
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any, TYPE_CHECKING

from models import TradingConfig, MarketTick

if TYPE_CHECKING:
    from adapters.okx_rfq_adapter import OKXRFQAdapter, RFQQuote

logger = logging.getLogger(__name__)


@dataclass
class RFQResult:
    """
    Outcome of an RFQ execution — returned to OrderExecutor which maps
    these values onto its SpreadOrder (both legs filled atomically).
    """
    success: bool
    # Spot leg (e.g. ETH-USDT-SWAP)
    spot_symbol: str = ""
    spot_filled_price: float = 0.0
    spot_filled_qty: float = 0.0
    # Futures leg (e.g. BTC-USDT-SWAP)
    futures_symbol: str = ""
    futures_filled_price: float = 0.0
    futures_filled_qty: float = 0.0
    # Execution metadata
    trade_id: str = ""
    quote_id: str = ""
    error: str = ""


class RFQExecutor:
    """
    Executes spread trades atomically via OKX RFQ.

    Instantiated in app.py alongside the REST/WS adapters and registered
    on OrderExecutor via set_rfq_executor(). OrderExecutor calls
    execute_spread_rfq() when the per-leg notional check passes.
    """

    def __init__(self, rfq_adapter: "OKXRFQAdapter", config: TradingConfig):
        self.rfq_adapter = rfq_adapter
        self.config = config

    def update_config(self, config: TradingConfig) -> None:
        self.config = config

    async def execute_spread_rfq(
        self,
        spot_symbol: str,
        futures_symbol: str,
        spot_side: str,
        futures_side: str,
        spot_qty: float,
        futures_qty: float,
        spot_pos_side: Optional[str],
        futures_pos_side: Optional[str],
        spot_tick: MarketTick,
        futures_tick: MarketTick,
        label: str = "",
    ) -> RFQResult:
        """
        Execute a two-legged spread atomically via OKX RFQ.

        Parameters mirror the SpreadOrder legs; OrderExecutor extracts and
        passes them so this module stays free of SpreadOrder imports.

        Returns RFQResult(success=True) with filled prices on success,
        RFQResult(success=False, error=...) on any failure.
        """
        timeout = float(getattr(self.config, 'rfq_quote_timeout_sec', 10.0))
        min_quotes = int(getattr(self.config, 'rfq_min_quotes', 1))
        anonymous = bool(getattr(self.config, 'rfq_anonymous', True))
        raw_cps = str(getattr(self.config, 'rfq_counterparties', "") or "")
        counterparties = [c.strip() for c in raw_cps.split(",") if c.strip()] or None

        logger.info(
            "[rfq_exec] %s RFQ: %s %s qty=%s | %s %s qty=%s | timeout=%ss anonymous=%s",
            label or "SPREAD",
            spot_side, spot_symbol, spot_qty,
            futures_side, futures_symbol, futures_qty,
            timeout, anonymous,
        )

        # 1. Build legs
        rfq_legs = self._build_legs(
            spot_symbol, spot_side, spot_qty, spot_pos_side,
            futures_symbol, futures_side, futures_qty, futures_pos_side,
        )

        # 2. Create RFQ
        rfq_id = await self.rfq_adapter.create_rfq(
            legs=rfq_legs, anonymous=anonymous, counterparties=counterparties
        )
        if not rfq_id:
            return RFQResult(success=False, error="RFQ creation failed — no rfqId returned")

        # 3. Poll for quotes
        quotes = await self._poll_quotes(rfq_id, timeout, min_quotes)
        if not quotes:
            await self.rfq_adapter.cancel_rfq(rfq_id)
            return RFQResult(
                success=False,
                error=f"no quotes received within {timeout}s for rfqId={rfq_id}",
            )

        # 4. Best quote
        best = self._select_best_quote(
            quotes,
            spot_symbol, spot_side, spot_tick,
            futures_symbol, futures_side, futures_tick,
        )
        logger.info("[rfq_exec] selected quoteId=%s from %d quote(s): %s",
                    best.quote_id, len(quotes), best)

        # 5. Execute
        exec_legs = self._build_execute_legs(
            best,
            spot_symbol, spot_side, spot_qty, spot_pos_side,
            futures_symbol, futures_side, futures_qty, futures_pos_side,
        )
        trade_data = await self.rfq_adapter.execute_quote(rfq_id, best.quote_id, exec_legs)
        if not trade_data:
            return RFQResult(
                success=False,
                error=f"execute-quote failed rfqId={rfq_id} quoteId={best.quote_id}",
            )

        # 6. Parse fills
        return self._parse_fills(
            trade_data, best,
            spot_symbol, spot_qty,
            futures_symbol, futures_qty,
        )

    # -------------------------------------------------------------- helpers

    def _build_legs(
        self,
        spot_symbol: str, spot_side: str, spot_qty: float, spot_pos_side: Optional[str],
        fut_symbol: str, fut_side: str, fut_qty: float, fut_pos_side: Optional[str],
    ) -> List[Dict[str, Any]]:
        legs = []
        for symbol, side, qty, pos_side in (
            (spot_symbol, spot_side, spot_qty, spot_pos_side),
            (fut_symbol, fut_side, fut_qty, fut_pos_side),
        ):
            leg: Dict[str, Any] = {
                "instId": symbol,
                "sz": str(qty),
                "side": side.lower(),
                "tdMode": "cross",
            }
            if pos_side:
                leg["posSide"] = pos_side
            legs.append(leg)
        return legs

    def _build_execute_legs(
        self,
        quote: "RFQQuote",
        spot_symbol: str, spot_side: str, spot_qty: float, spot_pos_side: Optional[str],
        fut_symbol: str, fut_side: str, fut_qty: float, fut_pos_side: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Merge quoted prices into execute-quote legs."""
        legs = []
        for symbol, side, qty, pos_side in (
            (spot_symbol, spot_side, spot_qty, spot_pos_side),
            (fut_symbol, fut_side, fut_qty, fut_pos_side),
        ):
            px = quote.price_for(symbol)
            leg: Dict[str, Any] = {
                "instId": symbol,
                "sz": str(qty),
                "side": side.lower(),
                "px": str(px),
            }
            if pos_side:
                leg["posSide"] = pos_side
            legs.append(leg)
        return legs

    async def _poll_quotes(
        self, rfq_id: str, timeout_sec: float, min_quotes: int
    ) -> List["RFQQuote"]:
        """Poll for active quotes every 500ms until min_quotes met or timeout."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_sec
        poll_interval = 0.5
        while loop.time() < deadline:
            quotes = await self.rfq_adapter.get_quotes(rfq_id)
            active = [q for q in quotes if q.is_active()]
            if len(active) >= min_quotes:
                return active
            await asyncio.sleep(poll_interval)
        return []

    def _select_best_quote(
        self,
        quotes: List["RFQQuote"],
        spot_symbol: str, spot_side: str, spot_tick: MarketTick,
        futures_symbol: str, futures_side: str, futures_tick: MarketTick,
    ) -> "RFQQuote":
        """
        Select the quote with the lowest total slippage vs current mid prices.

        Slippage for BUY  leg = quoted_px − mid  (we pay above mid)
        Slippage for SELL leg = mid − quoted_px  (we receive below mid)

        Lowest total slippage = best execution quality.
        """
        mid = {
            spot_symbol: spot_tick.mid if spot_tick else 0.0,
            futures_symbol: futures_tick.mid if futures_tick else 0.0,
        }
        side_map = {spot_symbol: spot_side.upper(), futures_symbol: futures_side.upper()}

        def cost(q: "RFQQuote") -> float:
            total = 0.0
            for sym in (spot_symbol, futures_symbol):
                px = q.price_for(sym)
                m = mid.get(sym, px)
                if m and px:
                    total += (px - m) if side_map[sym] == "BUY" else (m - px)
            return total

        return min(quotes, key=cost)

    def _parse_fills(
        self,
        trade_data: Dict[str, Any],
        quote: "RFQQuote",
        spot_symbol: str, spot_qty: float,
        futures_symbol: str, futures_qty: float,
    ) -> RFQResult:
        """
        Extract filled prices from the execute-quote response.
        Falls back to quoted prices if the trade response omits filled fields
        (shouldn't happen, but defensive).
        """
        trade_legs = trade_data.get("legs", [])
        fill_price: Dict[str, float] = {}
        fill_qty: Dict[str, float] = {}
        for tl in trade_legs:
            inst = tl.get("instId", "")
            fill_price[inst] = float(tl.get("px") or tl.get("fillPx") or 0.0)
            fill_qty[inst] = float(tl.get("sz") or tl.get("fillSz") or 0.0)

        spot_px = fill_price.get(spot_symbol) or quote.price_for(spot_symbol)
        fut_px = fill_price.get(futures_symbol) or quote.price_for(futures_symbol)
        spot_q = fill_qty.get(spot_symbol) or spot_qty
        fut_q = fill_qty.get(futures_symbol) or futures_qty

        if not spot_px or not fut_px:
            return RFQResult(
                success=False,
                error=f"zero fill prices in trade response: {trade_data}",
            )

        return RFQResult(
            success=True,
            spot_symbol=spot_symbol,
            spot_filled_price=spot_px,
            spot_filled_qty=spot_q,
            futures_symbol=futures_symbol,
            futures_filled_price=fut_px,
            futures_filled_qty=fut_q,
            trade_id=trade_data.get("tTradeId", ""),
            quote_id=quote.quote_id,
        )
