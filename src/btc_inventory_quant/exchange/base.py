"""Exchange adapter interface. Strategy/risk/execution code depends ONLY on this protocol."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from btc_inventory_quant.domain.inverse_contract import ContractSpec
from btc_inventory_quant.exchange.models import (
    AccountState,
    BookTicker,
    CommissionRates,
    ExchangeOrder,
    ExchangePosition,
    ExchangeTrade,
    IncomeRecord,
    LeverageBracket,
    MarkPriceInfo,
    OrderRequest,
    PositionMode,
    TickerPrice,
)


class ExchangeError(Exception):
    """Base class for adapter errors."""


class ExchangeAPIError(ExchangeError):
    def __init__(self, code: int, message: str, http_status: int | None = None) -> None:
        super().__init__(f"[{code}] {message} (http {http_status})")
        self.code = code
        self.message = message
        self.http_status = http_status


class ExchangeTimeout(ExchangeError):
    """Request timed out or the response was lost: the outcome is UNKNOWN."""


class ExchangeUnavailable(ExchangeError):
    """Connectivity / 5xx / rate-limit ban: retry later."""


@runtime_checkable
class MarketDataProvider(Protocol):
    def ping(self) -> bool: ...
    def server_time_ms(self) -> int: ...
    def exchange_info(self) -> dict[str, Any]: ...
    def contract_spec(self, symbol: str) -> ContractSpec: ...
    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]: ...
    def mark_price_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]: ...
    def index_price_klines(
        self,
        pair: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]: ...
    def funding_rate_history(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]: ...
    def premium_index(self, symbol: str) -> MarkPriceInfo: ...
    def book_ticker(self, symbol: str) -> BookTicker: ...
    def ticker_price(self, symbol: str) -> TickerPrice: ...


@runtime_checkable
class ExchangeAdapter(MarketDataProvider, Protocol):
    """Full trading interface. ``supports_trading`` is False for read-only adapters."""

    name: str
    environment: str

    @property
    def supports_trading(self) -> bool: ...

    # account
    def account(self) -> AccountState: ...
    def positions(self, pair: str | None = None) -> list[ExchangePosition]: ...
    def position_mode(self) -> PositionMode: ...
    def commission_rates(self, symbol: str) -> CommissionRates: ...
    def leverage_brackets(self, symbol: str) -> list[LeverageBracket]: ...
    def income(
        self,
        symbol: str | None = None,
        income_type: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[IncomeRecord]: ...
    def user_trades(
        self,
        symbol: str,
        order_id: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[ExchangeTrade]: ...

    # orders
    def place_order(self, request: OrderRequest) -> ExchangeOrder: ...
    def query_order(
        self, symbol: str, client_order_id: str | None = None, order_id: str | None = None
    ) -> ExchangeOrder: ...
    def cancel_order(
        self, symbol: str, client_order_id: str | None = None, order_id: str | None = None
    ) -> ExchangeOrder: ...
    def cancel_all_open_orders(self, symbol: str) -> None: ...
    def open_orders(self, symbol: str) -> list[ExchangeOrder]: ...

    # account configuration (setup only)
    def set_leverage(self, symbol: str, leverage: int) -> None: ...
    def set_margin_type(self, symbol: str, margin_type: str) -> None: ...

    # user data stream
    def start_user_stream(self) -> str: ...
    def keepalive_user_stream(self) -> None: ...
    def close_user_stream(self) -> None: ...
