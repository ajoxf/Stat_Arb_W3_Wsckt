"""
Abstract base class for exchange adapters.
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from models import MarketTick, OrderResult, Position, AccountInfo


class ExchangeAdapter(ABC):
    """
    Abstract base class for cryptocurrency exchange adapters.

    All exchange-specific implementations must inherit from this class
    and implement all abstract methods.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str = "",
        is_testnet: bool = False,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.is_testnet = is_testnet
        self._connected = False
        self._last_error = ""

    @property
    def connected(self) -> bool:
        """Check if adapter is connected."""
        return self._connected

    @property
    def last_error(self) -> str:
        """Get last error message."""
        return self._last_error

    @abstractmethod
    async def connect(self) -> bool:
        """
        Establish connection to the exchange.

        Returns:
            True if connection successful, False otherwise.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the exchange."""
        pass

    @abstractmethod
    async def get_tick(self, symbol: str) -> Optional[MarketTick]:
        """
        Get current market tick for a symbol.

        Args:
            symbol: Trading pair symbol (exchange-specific format).

        Returns:
            MarketTick object or None if unavailable.
        """
        pass

    @abstractmethod
    async def get_orderbook(
        self, symbol: str, depth: int = 5
    ) -> Optional[Dict[str, Any]]:
        """
        Get order book for a symbol.

        Args:
            symbol: Trading pair symbol.
            depth: Number of price levels to fetch.

        Returns:
            Dict with 'bids' and 'asks' lists, or None if unavailable.
        """
        pass

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        """
        Place an order.

        Args:
            symbol: Trading pair symbol.
            side: "BUY" or "SELL".
            order_type: "MARKET" or "LIMIT".
            quantity: Order quantity.
            price: Limit price (required for LIMIT orders).
            reduce_only: If True, only reduce position (futures).

        Returns:
            OrderResult with execution details.
        """
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        Cancel an open order.

        Args:
            symbol: Trading pair symbol.
            order_id: Exchange order ID.

        Returns:
            True if cancellation successful.
        """
        pass

    @abstractmethod
    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """
        Get open positions.

        Args:
            symbol: Optional symbol to filter positions.

        Returns:
            List of Position objects.
        """
        pass

    @abstractmethod
    async def close_position(self, symbol: str) -> OrderResult:
        """
        Close an open position.

        Args:
            symbol: Trading pair symbol.

        Returns:
            OrderResult with execution details.
        """
        pass

    @abstractmethod
    async def get_account_info(self) -> Optional[AccountInfo]:
        """
        Get account information.

        Returns:
            AccountInfo object or None if unavailable.
        """
        pass

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get funding rate for perpetual futures.

        Args:
            symbol: Perpetual futures symbol.

        Returns:
            Dict with funding rate info or None.
        """
        pass

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get symbol trading information.

        Args:
            symbol: Trading pair symbol.

        Returns:
            Dict with min qty, price precision, etc.
        """
        pass

    async def get_instruments(self, inst_type: str = "SPOT") -> List[Dict[str, Any]]:
        """
        List tradable instruments of a given type (e.g. "SPOT" / "SWAP").

        Not abstract: adapters that don't support instrument discovery
        inherit this no-op returning an empty list. Each entry should be a
        dict with at least {instId, base, quote, label}.

        Args:
            inst_type: Instrument category to list.

        Returns:
            List of instrument dicts, or [] if unsupported.
        """
        return []

    def _set_error(self, error: str) -> None:
        """Set last error message."""
        self._last_error = error

    def _clear_error(self) -> None:
        """Clear last error message."""
        self._last_error = ""
