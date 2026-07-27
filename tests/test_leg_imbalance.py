"""Tests for the entry size-imbalance leg-risk fix (#136).

A one-sided/partial entry fill (spot FILLED, futures PARTIAL) tripped none of the
existing leg-risk checks (has_partial_fill's XOR and has_orphan_risk's FAILED-leg
requirement both read it as "both filled"), so no recovery/flatten ran and the
naked leg was left for the 60s orphan-guard to market-flatten at a loss. These
cover the new detector and the immediate reduce-only flatten.
"""
from types import SimpleNamespace

from core.order_executor import OrderExecutor, SpreadOrder, LegOrder, LegStatus


def _order(spot_fill, spot_qty, spot_status, fut_fill, fut_qty, fut_status):
    # SHORT entry geometry (matches live #136): spot SELL/short, futures BUY/long
    spot = LegOrder(symbol="ETH-USDT-SWAP", side="SELL", quantity=spot_qty,
                    filled_qty=spot_fill, status=spot_status, pos_side="short",
                    order_id="s1")
    fut = LegOrder(symbol="BTC-USDT-SWAP", side="BUY", quantity=fut_qty,
                   filled_qty=fut_fill, status=fut_status, pos_side="long",
                   order_id="f1")
    return SpreadOrder(spot_leg=spot, futures_leg=fut, is_entry=True,
                       position_type="SHORT")


# ── has_size_imbalance detector ───────────────────────────────────────────
def test_imbalance_detects_full_vs_partial():
    # #136: spot 49/49 FILLED, futures 2.6/14 PARTIAL
    so = _order(49, 49, LegStatus.FILLED, 2.6, 14, LegStatus.PARTIAL)
    assert so.has_size_imbalance is True
    # ...and the existing checks genuinely miss it:
    assert so.has_partial_fill is False
    assert so.has_orphan_risk is False


def test_no_imbalance_on_complete_fill():
    so = _order(49, 49, LegStatus.FILLED, 15, 15, LegStatus.FILLED)
    assert so.has_size_imbalance is False


def test_no_imbalance_when_both_unfilled():
    so = _order(0, 49, LegStatus.OPEN, 0, 15, LegStatus.OPEN)
    assert so.has_size_imbalance is False


def test_no_imbalance_within_tolerance():
    # 96% vs 100% — nearly hedged; don't flatten a near-complete fill
    so = _order(49, 49, LegStatus.FILLED, 14.4, 15, LegStatus.PARTIAL)
    assert so.has_size_imbalance is False


# ── _flatten_residual_fills ───────────────────────────────────────────────
class _MockAdapter:
    def __init__(self):
        self.calls = []

    async def place_order(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(success=True, order_id="x", error=None)

    async def get_tick(self, symbol):
        return SimpleNamespace(mid=2000.0)


def _executor():
    eng = OrderExecutor.__new__(OrderExecutor)
    eng.spot_adapter = _MockAdapter()
    eng.futures_adapter = _MockAdapter()
    return eng


async def test_flatten_residual_closes_both_reduce_only():
    eng = _executor()
    so = _order(49, 49, LegStatus.FILLED, 2.6, 14, LegStatus.PARTIAL)

    await eng._flatten_residual_fills(so)

    # spot leg (SELL/short, 49 filled) closed with BUY reduce-only, qty 49
    assert len(eng.spot_adapter.calls) == 1
    sc = eng.spot_adapter.calls[0]
    assert sc["side"] == "BUY" and sc["reduce_only"] is True
    assert sc["pos_side"] == "short" and sc["quantity"] == 49
    # futures leg (BUY/long, 2.6 filled) closed with SELL reduce-only, qty 2.6
    assert len(eng.futures_adapter.calls) == 1
    fc = eng.futures_adapter.calls[0]
    assert fc["side"] == "SELL" and fc["reduce_only"] is True
    assert fc["pos_side"] == "long" and fc["quantity"] == 2.6
    # both legs marked FAILED so the engine rejects the entry cleanly
    assert so.spot_leg.status == LegStatus.FAILED
    assert so.futures_leg.status == LegStatus.FAILED


async def test_flatten_skips_zero_fill_leg():
    eng = _executor()
    # spot filled, futures ZERO fill (cancelled) → only spot is flattened
    so = _order(49, 49, LegStatus.FILLED, 0, 14, LegStatus.CANCELLED)
    await eng._flatten_residual_fills(so)
    assert len(eng.spot_adapter.calls) == 1
    assert len(eng.futures_adapter.calls) == 0
