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
    # True only when execute-quote was attempted and its outcome is UNKNOWN
    # (e.g. network timeout). The caller MUST NOT fall back to the order book in
    # this case — the RFQ may have filled — and should reconcile positions.
    ambiguous: bool = False


class RFQExecutor:
    """
    Executes spread trades atomically via OKX RFQ.

    Instantiated in app.py alongside the REST/WS adapters and registered
    on OrderExecutor via set_rfq_executor(). OrderExecutor calls
    execute_spread_rfq() when the per-leg notional check passes.
    """

    def __init__(self, rfq_adapter: "OKXRFQAdapter", config: TradingConfig, quote_ws=None):
        self.rfq_adapter = rfq_adapter
        self.config = config
        # Optional OKXRFQWebSocket for push-based quote consumption (OKX's
        # recommended low-latency path). None => REST polling. Lazily connected
        # on first RFQ so app.py setup stays synchronous.
        self.quote_ws = quote_ws
        # Available maker codes, fetched once. OKX RFQ is NOT broadcast-to-all —
        # create-rfq must name counterparties or no maker sees the request.
        self._counterparties_cache: Optional[List[str]] = None

    async def _ensure_quote_ws(self) -> None:
        """Connect the quote WebSocket on first use (kept alive by its own
        reconnect loop thereafter). Failure is non-fatal — we REST-poll instead."""
        if self.quote_ws is not None and not self.quote_ws.is_connected:
            try:
                await self.quote_ws.connect()
            except Exception as e:
                logger.warning("[rfq_exec] quote WS connect failed — REST polling: %s", e)

    def update_config(self, config: TradingConfig) -> None:
        self.config = config

    async def _resolve_counterparties(self) -> List[str]:
        """Counterparties to send the RFQ to: the configured list if set, else
        every available maker (fetched once and cached). Empty => RFQ can't get
        a quote, so the caller falls back to the order book."""
        raw = str(getattr(self.config, 'rfq_counterparties', "") or "")
        configured = [c.strip() for c in raw.split(",") if c.strip()]
        if configured:
            return configured
        if self._counterparties_cache is None:
            try:
                self._counterparties_cache = await self.rfq_adapter.get_counterparties()
            except Exception as e:
                logger.warning("[rfq_exec] get_counterparties failed: %s", e)
                self._counterparties_cache = []
        return self._counterparties_cache

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

        # OKX RFQ is not broadcast — we must name counterparties or no maker sees
        # it. Empty list => cannot get a quote, so fall back to the order book.
        counterparties = await self._resolve_counterparties()
        if not counterparties:
            return RFQResult(
                success=False,
                error="no RFQ counterparties available (none configured / none returned) "
                      "— cannot request a quote",
            )

        logger.info(
            "[rfq_exec] %s RFQ: %s %s qty=%s | %s %s qty=%s | timeout=%ss anonymous=%s cps=%d",
            label or "SPREAD",
            spot_side, spot_symbol, spot_qty,
            futures_side, futures_symbol, futures_qty,
            timeout, anonymous, len(counterparties),
        )

        # 1. Build legs
        rfq_legs = self._build_legs(
            spot_symbol, spot_side, spot_qty, spot_pos_side,
            futures_symbol, futures_side, futures_qty, futures_pos_side,
        )

        # Ensure the quotes WS is up (subscribed) before we create the RFQ, so no
        # maker quote is missed. Non-fatal — falls back to REST polling.
        await self._ensure_quote_ws()

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

        # 4. Best quote — only consider quotes that price BOTH legs (a quote
        # missing a leg would otherwise look "cheap" and execute at px=0).
        best = self._select_best_quote(
            quotes,
            spot_symbol, spot_side, spot_tick,
            futures_symbol, futures_side, futures_tick,
        )
        if best is None:
            await self.rfq_adapter.cancel_rfq(rfq_id)
            return RFQResult(
                success=False,
                error=f"no quote covering both legs for rfqId={rfq_id}",
            )

        # 4b. Edge guard — refuse a quote whose markup vs mid exceeds the cap.
        max_markup = float(getattr(self.config, 'rfq_max_markup_bps', 0.0) or 0.0)
        if max_markup > 0:
            markup_bps = self._quote_markup_bps(
                best, spot_symbol, spot_side, spot_tick,
                futures_symbol, futures_side, futures_tick,
            )
            if markup_bps > max_markup:
                await self.rfq_adapter.cancel_rfq(rfq_id)
                return RFQResult(
                    success=False,
                    error=(f"best quote markup {markup_bps:.1f}bps > "
                           f"rfq_max_markup_bps {max_markup:.1f}bps — rejecting"),
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
        if trade_data and trade_data.get("_ambiguous"):
            # Execute timed out — outcome unknown. Do NOT let the caller fall back.
            return RFQResult(
                success=False,
                ambiguous=True,
                quote_id=best.quote_id,
                error=(f"execute-quote AMBIGUOUS (timeout) rfqId={rfq_id} "
                       f"quoteId={best.quote_id} — reconcile positions"),
            )
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
        """Build execute-quote legs, accepting the quote 'as-is': price AND size
        come from the quote object (OKX matches on the exact rfqId/quoteId), with
        our request as a fallback only if the quote omits a field."""
        legs = []
        for symbol, side, qty, pos_side in (
            (spot_symbol, spot_side, spot_qty, spot_pos_side),
            (fut_symbol, fut_side, fut_qty, fut_pos_side),
        ):
            px = quote.price_for(symbol)
            leg: Dict[str, Any] = {
                "instId": symbol,
                "sz": quote.size_for(symbol) or str(qty),
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
        """Return active quotes for the RFQ. Prefers the WS push cache (OKX's
        recommended low-latency path); falls back to REST polling if the quote
        WebSocket isn't connected."""
        if self.quote_ws is not None and self.quote_ws.is_connected:
            return await self.quote_ws.wait_for_quotes(rfq_id, min_quotes, timeout_sec)

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
    ) -> Optional["RFQQuote"]:
        """
        Select the quote with the lowest total slippage vs current mid prices.

        Slippage for BUY  leg = quoted_px − mid  (we pay above mid)
        Slippage for SELL leg = mid − quoted_px  (we receive below mid)

        Lowest total slippage = best execution quality. Quotes that don't price
        BOTH legs (px > 0 each) are discarded — otherwise a missing leg reads as
        0 cost and would be "selected" then executed at px=0. Returns None when
        no quote prices both legs.
        """
        mid = {
            spot_symbol: spot_tick.mid if spot_tick else 0.0,
            futures_symbol: futures_tick.mid if futures_tick else 0.0,
        }
        side_map = {spot_symbol: spot_side.upper(), futures_symbol: futures_side.upper()}

        valid = [
            q for q in quotes
            if q.price_for(spot_symbol) > 0 and q.price_for(futures_symbol) > 0
        ]
        if not valid:
            return None

        def cost(q: "RFQQuote") -> float:
            total = 0.0
            for sym in (spot_symbol, futures_symbol):
                px = q.price_for(sym)
                m = mid.get(sym, px)
                if m and px:
                    total += (px - m) if side_map[sym] == "BUY" else (m - px)
            return total

        return min(valid, key=cost)

    def _quote_markup_bps(
        self,
        quote: "RFQQuote",
        spot_symbol: str, spot_side: str, spot_tick: MarketTick,
        futures_symbol: str, futures_side: str, futures_tick: MarketTick,
    ) -> float:
        """Worst per-leg markup vs mid, in bps (the price we give up to the maker).

        Positive = unfavourable (we pay above mid on a buy / receive below on a
        sell). Returns the max across legs so a single bad leg trips the guard.
        """
        legs = (
            (spot_symbol, spot_side.upper(), spot_tick.mid if spot_tick else 0.0),
            (futures_symbol, futures_side.upper(), futures_tick.mid if futures_tick else 0.0),
        )
        worst = 0.0
        for sym, side, m in legs:
            px = quote.price_for(sym)
            if not (m and px):
                continue
            slip = (px - m) if side == "BUY" else (m - px)
            worst = max(worst, slip / m * 10000.0)
        return worst

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
            # blockTdId is the OKX anchor for reconciling both legs in /trade/fills
            # (struc-block-trades push uses it too); fall back to tTradeId.
            trade_id=trade_data.get("blockTdId") or trade_data.get("tTradeId", ""),
            quote_id=quote.quote_id,
        )
