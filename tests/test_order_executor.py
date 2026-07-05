"""
Unit tests for order executor module.

Tests order placement, cancellation, status checking, and edge cases.
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timedelta

from core.order_executor import (
    OrderExecutor, SpreadOrder, LegOrder, LegStatus, ExecutionMode
)
from models import TradingConfig, MarketTick, OrderResult


@pytest.fixture
def trading_config():
    """Create a test trading configuration."""
    return TradingConfig(
        asset="BTC",
        spot_symbol="BTC-USDT",
        futures_symbol="BTC-USDT-SWAP",
        entry_threshold=2.0,
        exit_threshold=0.5,
        stop_loss_threshold=4.0,
        lookback_period=100,
        position_size_usd=1000.0,
        paper_trading=False,
        order_execution_mode="LIMIT",
        limit_order_timeout_sec=30,
        limit_order_price_offset_bps=2.0,
        futures_leverage=1,
    )


@pytest.fixture
def mock_spot_adapter(spot_tick):
    """Create a mock spot adapter."""
    adapter = Mock()
    adapter.place_order = AsyncMock()
    adapter.cancel_order = AsyncMock(return_value=True)
    adapter.get_order_status = AsyncMock()
    # The executor re-fetches ticks mid-flow (POST_ONLY reprice loop); a bare
    # AsyncMock here returns MagicMocks that blow up in price arithmetic.
    adapter.get_tick = AsyncMock(return_value=spot_tick)
    return adapter


@pytest.fixture
def mock_futures_adapter(futures_tick):
    """Create a mock futures adapter."""
    adapter = Mock()
    adapter.place_order = AsyncMock()
    adapter.cancel_order = AsyncMock(return_value=True)
    adapter.get_order_status = AsyncMock()
    adapter.get_tick = AsyncMock(return_value=futures_tick)
    return adapter


@pytest.fixture
def spot_tick():
    """Create a test spot tick."""
    return MarketTick(
        symbol="BTC-USDT",
        bid=67000.0,
        ask=67010.0,
        last=67005.0,
        volume_24h=1000000.0,
        timestamp=datetime.utcnow(),
    )


@pytest.fixture
def futures_tick():
    """Create a test futures tick."""
    return MarketTick(
        symbol="BTC-USDT-SWAP",
        bid=68000.0,
        ask=68010.0,
        last=68005.0,
        volume_24h=2000000.0,
        timestamp=datetime.utcnow(),
    )


class TestOrderExecutorInit:
    """Test OrderExecutor initialization."""

    def test_init_creates_executor(self, trading_config, mock_spot_adapter, mock_futures_adapter):
        """Test that executor is created with correct config."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        assert executor.config == trading_config
        assert executor.spot_adapter == mock_spot_adapter
        assert executor.futures_adapter == mock_futures_adapter
        assert executor.active_order is None
        assert executor._executing is False


class TestExecutionLock:
    """Test execution lock prevents duplicate orders."""

    @pytest.mark.asyncio
    async def test_execute_entry_returns_none_if_already_executing(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test that execute_entry returns None if already executing."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)
        executor._executing = True  # Simulate already executing

        result = await executor.execute_entry("LONG", spot_tick, futures_tick, 0.01)

        assert result is None

    @pytest.mark.asyncio
    async def test_execute_exit_returns_none_if_already_executing(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test that execute_exit returns None if already executing."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)
        executor._executing = True

        result = await executor.execute_exit("LONG", spot_tick, futures_tick, 0.01)

        assert result is None


class TestLegSides:
    """Test that leg sides are set correctly for LONG and SHORT spreads."""

    @pytest.mark.asyncio
    async def test_long_spread_entry_sides(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test LONG spread entry: Buy spot, Sell futures (short pos_side)."""
        trading_config.order_execution_mode = "MARKET"
        trading_config.entry_execution_mode = "MARKET"
        trading_config.exit_execution_mode = "MARKET"
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        # Mock successful orders
        mock_spot_adapter.place_order.return_value = OrderResult(
            success=True, order_id="spot123", filled_qty=0.01, filled_price=67005.0
        )
        mock_futures_adapter.place_order.return_value = OrderResult(
            success=True, order_id="fut123", filled_qty=0.01, filled_price=68005.0
        )

        result = await executor.execute_entry("LONG", spot_tick, futures_tick, 0.01)

        # Check spot leg: BUY
        spot_call = mock_spot_adapter.place_order.call_args
        assert spot_call.kwargs["side"] == "BUY"

        # Check futures leg: SELL with pos_side="short"
        futures_call = mock_futures_adapter.place_order.call_args
        assert futures_call.kwargs["side"] == "SELL"
        assert futures_call.kwargs["pos_side"] == "short"

    @pytest.mark.asyncio
    async def test_short_spread_entry_sides(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test SHORT spread entry: Sell spot, Buy futures (long pos_side)."""
        trading_config.order_execution_mode = "MARKET"
        trading_config.entry_execution_mode = "MARKET"
        trading_config.exit_execution_mode = "MARKET"
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        mock_spot_adapter.place_order.return_value = OrderResult(
            success=True, order_id="spot123", filled_qty=0.01, filled_price=67005.0
        )
        mock_futures_adapter.place_order.return_value = OrderResult(
            success=True, order_id="fut123", filled_qty=0.01, filled_price=68005.0
        )

        result = await executor.execute_entry("SHORT", spot_tick, futures_tick, 0.01)

        # Check spot leg: SELL
        spot_call = mock_spot_adapter.place_order.call_args
        assert spot_call.kwargs["side"] == "SELL"

        # Check futures leg: BUY with pos_side="long"
        futures_call = mock_futures_adapter.place_order.call_args
        assert futures_call.kwargs["side"] == "BUY"
        assert futures_call.kwargs["pos_side"] == "long"

    @pytest.mark.asyncio
    async def test_close_long_spread_sides(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test close LONG spread: Sell spot, Buy futures (same pos_side="short")."""
        trading_config.order_execution_mode = "MARKET"
        trading_config.entry_execution_mode = "MARKET"
        trading_config.exit_execution_mode = "MARKET"
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        mock_spot_adapter.place_order.return_value = OrderResult(
            success=True, order_id="spot123", filled_qty=0.01, filled_price=67005.0
        )
        mock_futures_adapter.place_order.return_value = OrderResult(
            success=True, order_id="fut123", filled_qty=0.01, filled_price=68005.0
        )

        result = await executor.execute_exit("LONG", spot_tick, futures_tick, 0.01)

        # Check spot leg: SELL
        spot_call = mock_spot_adapter.place_order.call_args
        assert spot_call.kwargs["side"] == "SELL"

        # Check futures leg: BUY with pos_side="short" (same as entry to close)
        futures_call = mock_futures_adapter.place_order.call_args
        assert futures_call.kwargs["side"] == "BUY"
        assert futures_call.kwargs["pos_side"] == "short"


class TestOrderStatusChecking:
    """Test order status checking implementation."""

    @pytest.mark.asyncio
    async def test_check_order_status_updates_filled_leg(
        self, trading_config, mock_spot_adapter, mock_futures_adapter
    ):
        """Test that filled orders are detected and status updated."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol="BTC-USDT", side="BUY", quantity=0.01,
                order_id="spot123", status=LegStatus.OPEN
            ),
            futures_leg=LegOrder(
                symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01,
                order_id="fut123", status=LegStatus.OPEN, pos_side="short"
            ),
            is_entry=True,
            position_type="LONG",
        )

        # Mock filled status
        mock_spot_adapter.get_order_status.return_value = {
            "state": "filled",
            "filled_qty": 0.01,
            "filled_price": 67005.0,
        }
        mock_futures_adapter.get_order_status.return_value = {
            "state": "filled",
            "filled_qty": 0.01,
            "filled_price": 68005.0,
        }

        await executor._check_order_status(spread_order)

        assert spread_order.spot_leg.status == LegStatus.FILLED
        assert spread_order.spot_leg.filled_qty == 0.01
        assert spread_order.spot_leg.filled_price == 67005.0

        assert spread_order.futures_leg.status == LegStatus.FILLED
        assert spread_order.futures_leg.filled_qty == 0.01
        assert spread_order.futures_leg.filled_price == 68005.0

    @pytest.mark.asyncio
    async def test_check_order_status_detects_cancelled(
        self, trading_config, mock_spot_adapter, mock_futures_adapter
    ):
        """Test that externally cancelled orders are detected."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol="BTC-USDT", side="BUY", quantity=0.01,
                order_id="spot123", status=LegStatus.OPEN
            ),
            futures_leg=LegOrder(
                symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01,
                order_id="fut123", status=LegStatus.OPEN, pos_side="short"
            ),
            is_entry=True,
            position_type="LONG",
        )

        # Mock cancelled status
        mock_spot_adapter.get_order_status.return_value = {"state": "canceled", "filled_qty": 0, "filled_price": 0}
        mock_futures_adapter.get_order_status.return_value = {"state": "canceled", "filled_qty": 0, "filled_price": 0}

        await executor._check_order_status(spread_order)

        assert spread_order.spot_leg.status == LegStatus.CANCELLED
        assert spread_order.futures_leg.status == LegStatus.CANCELLED


class TestAmendOrders:
    """Test order amendment logic."""

    @pytest.mark.asyncio
    async def test_amend_only_places_new_order_if_cancel_succeeds(
        self, trading_config, mock_spot_adapter, mock_futures_adapter
    ):
        """Test that new order is only placed if cancel succeeds."""
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol="BTC-USDT", side="BUY", quantity=0.01,
                order_id="spot123", status=LegStatus.OPEN, target_price=67000.0
            ),
            futures_leg=LegOrder(
                symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01,
                order_id="fut123", status=LegStatus.OPEN, target_price=68000.0, pos_side="short"
            ),
            is_entry=True,
            position_type="LONG",
        )

        # Mock: status is live, cancel fails
        mock_spot_adapter.get_order_status.return_value = {"state": "live", "filled_qty": 0, "filled_price": 0}
        mock_spot_adapter.cancel_order.return_value = False  # Cancel fails
        mock_futures_adapter.get_order_status.return_value = {"state": "live", "filled_qty": 0, "filled_price": 0}
        mock_futures_adapter.cancel_order.return_value = False  # Cancel fails

        await executor._amend_limit_orders(spread_order)

        # place_order should NOT be called since cancel failed
        mock_spot_adapter.place_order.assert_not_called()
        mock_futures_adapter.place_order.assert_not_called()


class TestSpreadOrderProperties:
    """Test SpreadOrder property calculations."""

    def test_is_complete_when_both_filled(self):
        """Test is_complete returns True when both legs filled."""
        spread_order = SpreadOrder(
            spot_leg=LegOrder(symbol="BTC-USDT", side="BUY", quantity=0.01, status=LegStatus.FILLED),
            futures_leg=LegOrder(symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01, status=LegStatus.FILLED),
            is_entry=True,
            position_type="LONG",
        )

        assert spread_order.is_complete is True

    def test_is_complete_false_when_one_open(self):
        """Test is_complete returns False when one leg still open."""
        spread_order = SpreadOrder(
            spot_leg=LegOrder(symbol="BTC-USDT", side="BUY", quantity=0.01, status=LegStatus.FILLED),
            futures_leg=LegOrder(symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01, status=LegStatus.OPEN),
            is_entry=True,
            position_type="LONG",
        )

        assert spread_order.is_complete is False

    def test_has_partial_fill_detects_leg_risk(self):
        """Test has_partial_fill detects when one leg filled but not other."""
        spread_order = SpreadOrder(
            spot_leg=LegOrder(symbol="BTC-USDT", side="BUY", quantity=0.01, status=LegStatus.FILLED),
            futures_leg=LegOrder(symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01, status=LegStatus.OPEN),
            is_entry=True,
            position_type="LONG",
        )

        assert spread_order.has_partial_fill is True

    def test_is_failed_when_leg_fails(self):
        """Test is_failed returns True when a leg fails."""
        spread_order = SpreadOrder(
            spot_leg=LegOrder(symbol="BTC-USDT", side="BUY", quantity=0.01, status=LegStatus.FAILED),
            futures_leg=LegOrder(symbol="BTC-USDT-SWAP", side="SELL", quantity=0.01, status=LegStatus.OPEN),
            is_entry=True,
            position_type="LONG",
        )

        assert spread_order.is_failed is True


class TestMarketOrderExecution:
    """Test market order execution."""

    @pytest.mark.asyncio
    async def test_market_order_fills_both_legs(
        self, trading_config, mock_spot_adapter, mock_futures_adapter, spot_tick, futures_tick
    ):
        """Test that market orders fill both legs simultaneously."""
        trading_config.order_execution_mode = "MARKET"
        trading_config.entry_execution_mode = "MARKET"
        trading_config.exit_execution_mode = "MARKET"
        executor = OrderExecutor(trading_config, mock_spot_adapter, mock_futures_adapter)

        mock_spot_adapter.place_order.return_value = OrderResult(
            success=True, order_id="spot123", filled_qty=0.01, filled_price=67005.0
        )
        mock_futures_adapter.place_order.return_value = OrderResult(
            success=True, order_id="fut123", filled_qty=0.01, filled_price=68005.0
        )

        result = await executor.execute_entry("LONG", spot_tick, futures_tick, 0.01)

        assert result is not None
        assert result.is_complete is True
        assert result.spot_leg.status == LegStatus.FILLED
        assert result.futures_leg.status == LegStatus.FILLED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
