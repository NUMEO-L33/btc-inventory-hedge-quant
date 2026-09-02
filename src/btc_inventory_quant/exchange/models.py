"""Exchange-neutral models (Decimal everywhere money is involved)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from btc_inventory_quant.domain.inverse_contract import OrderSide


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTX = "GTX"  # post-only


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.EXPIRED_IN_MATCH,
            OrderStatus.REJECTED,
        }

    @property
    def is_open(self) -> bool:
        return self in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}


class PositionMode(StrEnum):
    ONE_WAY = "ONE_WAY"
    HEDGE = "HEDGE"


class MarginType(StrEnum):
    CROSSED = "CROSSED"
    ISOLATED = "ISOLATED"


def ms_to_dt(ms: int | float | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


def dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    return Decimal(str(value))


def dec0(value: Any) -> Decimal:
    d = dec(value)
    return d if d is not None else Decimal("0")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal  # whole contracts
    client_order_id: str
    reduce_only: bool = False
    price: Decimal | None = None
    time_in_force: TimeInForce | None = None
    response_type: str = "RESULT"

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type is OrderType.LIMIT and (
            self.price is None or self.time_in_force is None
        ):
            raise ValueError("LIMIT orders need price and time_in_force")
        if len(self.client_order_id) > 36 or not self.client_order_id:
            raise ValueError("client_order_id must be 1..36 chars")


@dataclass(frozen=True, slots=True)
class ExchangeOrder:
    symbol: str
    order_id: str
    client_order_id: str
    side: OrderSide
    order_type: str
    status: OrderStatus
    orig_qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal | None
    reduce_only: bool
    position_side: str
    price: Decimal | None
    time_in_force: str | None
    update_time: datetime | None
    cum_base: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def remaining_qty(self) -> Decimal:
        return self.orig_qty - self.executed_qty


@dataclass(frozen=True, slots=True)
class ExchangeTrade:
    symbol: str
    trade_id: str
    order_id: str
    side: OrderSide
    price: Decimal
    qty: Decimal
    base_qty_btc: Decimal | None
    realized_pnl: Decimal
    commission: Decimal
    commission_asset: str
    time: datetime | None
    maker: bool
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    symbol: str
    contracts: Decimal  # signed (negative = short)
    entry_price: Decimal | None
    mark_price: Decimal | None
    unrealized_pnl: Decimal
    liquidation_price: Decimal | None
    leverage: int | None
    margin_type: str | None
    position_side: str
    update_time: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def short_contracts(self) -> Decimal:
        return -self.contracts if self.contracts < 0 else Decimal("0")


@dataclass(frozen=True, slots=True)
class AccountState:
    asset: str
    wallet_balance: Decimal
    cross_wallet_balance: Decimal
    unrealized_pnl: Decimal
    margin_balance: Decimal
    available_balance: Decimal
    maint_margin: Decimal
    positions: tuple[ExchangePosition, ...]
    can_trade: bool
    update_time: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def position_for(self, symbol: str) -> ExchangePosition | None:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None


@dataclass(frozen=True, slots=True)
class MarkPriceInfo:
    symbol: str
    mark_price: Decimal
    index_price: Decimal | None
    last_funding_rate: Decimal | None
    next_funding_time: datetime | None
    time: datetime


@dataclass(frozen=True, slots=True)
class IncomeRecord:
    income_type: str
    amount: Decimal
    asset: str
    time: datetime | None
    tran_id: str
    symbol: str
    info: str
    trade_id: str = ""


@dataclass(frozen=True, slots=True)
class CommissionRates:
    symbol: str
    maker: Decimal
    taker: Decimal


@dataclass(frozen=True, slots=True)
class LeverageBracket:
    bracket: int
    initial_leverage: int
    qty_cap: Decimal
    qty_floor: Decimal
    maint_margin_ratio: Decimal
    cum: Decimal


@dataclass(frozen=True, slots=True)
class BookTicker:
    symbol: str
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    time: datetime | None


@dataclass(frozen=True, slots=True)
class TickerPrice:
    symbol: str
    price: Decimal
    time: datetime | None
