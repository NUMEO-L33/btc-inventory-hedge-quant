"""Execution policies: how an order intent becomes exchange orders.

* MarketExecution     — one market order (default for the first small live runs)
* MakerFirstExecution — post-only limit at the touch, wait, cancel, market the remainder
* TwapExecution       — N market slices spaced in time
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from btc_inventory_quant.domain.inverse_contract import ZERO, OrderSide
from btc_inventory_quant.exchange.base import ExchangeAdapter
from btc_inventory_quant.exchange.models import OrderType, TimeInForce
from btc_inventory_quant.execution.order_manager import FillSummary, OrderManager, OrderState
from btc_inventory_quant.execution.rebalancer import OrderIntent
from btc_inventory_quant.monitoring.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    exchange_short_contracts: Decimal
    equity_btc: Decimal
    mark_price: Decimal


@dataclass(slots=True)
class ExecutionReport:
    intent: OrderIntent
    fills: list[FillSummary] = field(default_factory=list)

    @property
    def filled_qty(self) -> Decimal:
        return sum((f.filled_qty for f in self.fills), ZERO)

    @property
    def remaining_qty(self) -> Decimal:
        return self.intent.qty - self.filled_qty

    @property
    def commission_btc(self) -> Decimal:
        return sum((f.commission_btc for f in self.fills), ZERO)

    @property
    def realized_pnl_btc(self) -> Decimal:
        return sum((f.realized_pnl_btc for f in self.fills), ZERO)

    @property
    def avg_price(self) -> Decimal | None:
        qty = ZERO
        inv = ZERO
        for f in self.fills:
            if f.filled_qty > ZERO and f.avg_price:
                qty += f.filled_qty
                inv += f.filled_qty / f.avg_price
        return qty / inv if inv > ZERO else None

    @property
    def complete(self) -> bool:
        return self.remaining_qty <= ZERO

    @property
    def rejected(self) -> bool:
        return bool(self.fills) and all(f.status is OrderState.REJECTED for f in self.fills)


class ExecutionPolicy(Protocol):
    name: str

    def execute(self, intent: OrderIntent, ctx: ExecutionContext) -> ExecutionReport: ...


def _ctx_after(ctx: ExecutionContext, intent: OrderIntent, filled: Decimal) -> ExecutionContext:
    short = ctx.exchange_short_contracts + (filled if intent.side is OrderSide.SELL else -filled)
    return ExecutionContext(short, ctx.equity_btc, ctx.mark_price)


class MarketExecution:
    name = "market"

    def __init__(self, manager: OrderManager) -> None:
        self.manager = manager

    def execute(self, intent: OrderIntent, ctx: ExecutionContext) -> ExecutionReport:
        report = ExecutionReport(intent)
        fill = self.manager.submit(
            intent,
            exchange_short_contracts=ctx.exchange_short_contracts,
            equity_btc=ctx.equity_btc,
            mark_price=ctx.mark_price,
        )
        report.fills.append(fill)
        return report


class MakerFirstExecution:
    name = "maker_first"

    def __init__(
        self,
        manager: OrderManager,
        exchange: ExchangeAdapter,
        wait_seconds: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.manager = manager
        self.exchange = exchange
        self.wait_seconds = wait_seconds
        self._sleep = sleep

    def execute(self, intent: OrderIntent, ctx: ExecutionContext) -> ExecutionReport:
        report = ExecutionReport(intent)
        book = self.exchange.book_ticker(self.manager.symbol)
        price = book.ask_price if intent.side is OrderSide.SELL else book.bid_price
        limit_fill = self.manager.submit(
            intent,
            exchange_short_contracts=ctx.exchange_short_contracts,
            equity_btc=ctx.equity_btc,
            mark_price=ctx.mark_price,
            order_type=OrderType.LIMIT,
            price=price,
            time_in_force=TimeInForce.GTX,
            wait=False,
        )
        if limit_fill.status not in (
            OrderState.REJECTED,
            OrderState.NOT_PLACED,
            OrderState.EXPIRED,
        ):
            order = self.manager.exchange.query_order(
                self.manager.symbol, client_order_id=limit_fill.client_order_id
            )
            order = self.manager.wait_terminal(
                limit_fill.client_order_id, order, timeout=self.wait_seconds
            )
            if not order.status.is_terminal:
                cancelled = self.manager.cancel(limit_fill.client_order_id)
                order = cancelled or order
            limit_fill = self.manager.collect_fills(limit_fill.client_order_id, order)
        report.fills.append(limit_fill)
        remaining = intent.qty - limit_fill.filled_qty
        if remaining > ZERO:
            rest = OrderIntent(
                side=intent.side,
                qty=remaining,
                reduce_only=intent.reduce_only,
                reason=f"{intent.reason}:maker_fallback",
                target_ratio=intent.target_ratio,
                current_ratio=intent.current_ratio,
                target_contracts=intent.target_contracts,
                current_short_contracts=intent.current_short_contracts,
            )
            report.fills.append(
                self.manager.submit(
                    rest,
                    exchange_short_contracts=_ctx_after(
                        ctx, intent, limit_fill.filled_qty
                    ).exchange_short_contracts,
                    equity_btc=ctx.equity_btc,
                    mark_price=ctx.mark_price,
                )
            )
        return report


class TwapExecution:
    name = "twap"

    def __init__(
        self,
        manager: OrderManager,
        slices: int = 4,
        interval_seconds: float = 300.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.manager = manager
        self.slices = max(1, slices)
        self.interval = interval_seconds
        self._sleep = sleep

    def execute(self, intent: OrderIntent, ctx: ExecutionContext) -> ExecutionReport:
        report = ExecutionReport(intent)
        spec = self.manager.spec
        per_slice = spec.round_qty_down(intent.qty / self.slices)
        if per_slice < spec.min_qty:
            per_slice = intent.qty
        remaining = intent.qty
        current_ctx = ctx
        i = 0
        while remaining > ZERO:
            qty = min(per_slice, remaining) if i < self.slices - 1 else remaining
            piece = OrderIntent(
                intent.side,
                qty,
                intent.reduce_only,
                f"{intent.reason}:twap{i + 1}",
                intent.target_ratio,
                intent.current_ratio,
                intent.target_contracts,
                intent.current_short_contracts,
            )
            fill = self.manager.submit(
                piece,
                exchange_short_contracts=current_ctx.exchange_short_contracts,
                equity_btc=current_ctx.equity_btc,
                mark_price=current_ctx.mark_price,
            )
            report.fills.append(fill)
            if fill.status in (OrderState.REJECTED, OrderState.NOT_PLACED):
                break
            remaining -= fill.filled_qty
            current_ctx = _ctx_after(current_ctx, intent, fill.filled_qty)
            i += 1
            if remaining > ZERO:
                self._sleep(self.interval)
        return report
