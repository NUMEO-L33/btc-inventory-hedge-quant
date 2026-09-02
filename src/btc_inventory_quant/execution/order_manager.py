"""Order manager: idempotent, write-ahead, never-duplicate order lifecycle.

Lifecycle (prompt §11)::

    intent → pre-trade validation → unique client order id → DB(PENDING_SUBMIT)
          → exchange.place_order
              ├─ accepted            → DB(NEW/FILLED…) → wait terminal → fetch fills → DB
              ├─ rejected (API err)  → DB(REJECTED)   (the exchange never had the order)
              └─ timeout/lost        → DB(UNCERTAIN)  → query origClientOrderId
                                          ├─ found        → adopt exchange state
                                          ├─ -2013 twice  → DB(NOT_PLACED)  (safe to resubmit with a NEW id)
                                          └─ unreachable  → stays UNCERTAIN → caller enters SAFE_HOLD

A new order is refused while any order for the symbol is non-terminal (PENDING_SUBMIT /
SUBMITTED / UNCERTAIN / NEW / PARTIALLY_FILLED). BUY orders are always reduce-only and are
validated against the *exchange* short size before submission.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from btc_inventory_quant.config.schema import RiskLimits
from btc_inventory_quant.domain.inverse_contract import ZERO, ContractSpec, OrderSide
from btc_inventory_quant.exchange.base import (
    ExchangeAdapter,
    ExchangeAPIError,
    ExchangeError,
    ExchangeTimeout,
    ExchangeUnavailable,
)
from btc_inventory_quant.exchange.models import (
    ExchangeOrder,
    ExchangeTrade,
    OrderRequest,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from btc_inventory_quant.execution.rebalancer import OrderIntent
from btc_inventory_quant.monitoring.logging import get_logger
from btc_inventory_quant.risk.limits import RiskLimitViolation, validate_order_against_position
from btc_inventory_quant.storage.database import Database

log = get_logger(__name__)
CLIENT_ID_MAX = 36
NO_SUCH_ORDER = -2013


class OrderState(StrEnum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    UNCERTAIN = "UNCERTAIN"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"
    REJECTED = "REJECTED"
    NOT_PLACED = "NOT_PLACED"


UNRESOLVED_STATES = [
    OrderState.PENDING_SUBMIT.value,
    OrderState.SUBMITTED.value,
    OrderState.UNCERTAIN.value,
    OrderState.NEW.value,
    OrderState.PARTIALLY_FILLED.value,
]
TERMINAL_STATES = {
    OrderState.FILLED,
    OrderState.CANCELED,
    OrderState.EXPIRED,
    OrderState.EXPIRED_IN_MATCH,
    OrderState.REJECTED,
    OrderState.NOT_PLACED,
}


class OrderManagerError(Exception):
    pass


class DuplicateRiskError(OrderManagerError):
    """Refusing to place a new order while another order is unresolved."""


class UnresolvedOrderError(OrderManagerError):
    """Outcome of an order could not be determined — caller must SAFE_HOLD."""


@dataclass(slots=True)
class FillSummary:
    client_order_id: str
    exchange_order_id: str | None
    side: OrderSide
    requested_qty: Decimal
    filled_qty: Decimal
    avg_price: Decimal | None
    commission_btc: Decimal
    realized_pnl_btc: Decimal
    status: OrderState
    trades: list[ExchangeTrade] = field(default_factory=list)

    @property
    def fully_filled(self) -> bool:
        return self.filled_qty >= self.requested_qty and self.status is OrderState.FILLED


def status_to_state(status: OrderStatus) -> OrderState:
    mapping = {
        OrderStatus.NEW: OrderState.NEW,
        OrderStatus.PARTIALLY_FILLED: OrderState.PARTIALLY_FILLED,
        OrderStatus.FILLED: OrderState.FILLED,
        OrderStatus.CANCELED: OrderState.CANCELED,
        OrderStatus.EXPIRED: OrderState.EXPIRED,
        OrderStatus.EXPIRED_IN_MATCH: OrderState.EXPIRED_IN_MATCH,
        OrderStatus.REJECTED: OrderState.REJECTED,
    }
    return mapping.get(status, OrderState.SUBMITTED)


class OrderManager:
    def __init__(
        self,
        exchange: ExchangeAdapter,
        db: Database,
        spec: ContractSpec,
        limits: RiskLimits,
        symbol: str,
        mode: str,
        *,
        client_id_prefix: str = "biq",
        order_timeout_seconds: float = 30.0,
        max_query_attempts: int = 5,
        query_grace_seconds: float = 2.0,
        poll_interval_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        bypass_cap: bool = False,
    ) -> None:
        self.exchange = exchange
        self.db = db
        self.spec = spec
        self.limits = limits
        self.symbol = symbol
        self.mode = mode
        self.prefix = client_id_prefix
        self.order_timeout = order_timeout_seconds
        self.max_query_attempts = max_query_attempts
        self.query_grace = query_grace_seconds
        self.poll_interval = poll_interval_seconds
        self._sleep = sleep
        self._clock = clock
        self.bypass_cap = bypass_cap
        self.consecutive_rejects = 0

    # --------------------------------------------------------------- ids
    def new_client_order_id(self) -> str:
        ts = datetime.fromtimestamp(self._clock(), tz=UTC).strftime("%Y%m%dT%H%M%S")
        cid = f"{self.prefix}-{self.mode[:1]}-{ts}-{secrets.token_hex(4)}"
        assert len(cid) <= CLIENT_ID_MAX
        return cid

    # ------------------------------------------------------------ queries
    def unresolved_orders(self) -> list[Any]:
        return self.db.orders_with_status(UNRESOLVED_STATES, symbol=self.symbol)

    def known_client_ids(self) -> set[str]:
        return {o.client_order_id for o in self.db.all_orders(limit=5000)}

    # ------------------------------------------------------------ submit
    def submit(
        self,
        intent: OrderIntent,
        *,
        exchange_short_contracts: Decimal,
        equity_btc: Decimal,
        mark_price: Decimal,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
        time_in_force: TimeInForce | None = None,
        wait: bool = True,
    ) -> FillSummary:
        unresolved = self.unresolved_orders()
        if unresolved:
            raise DuplicateRiskError(
                f"{len(unresolved)} unresolved order(s): {[o.client_order_id for o in unresolved]}"
            )
        qty = self.spec.round_qty_down(intent.qty)
        try:
            validate_order_against_position(
                intent.side,
                qty,
                exchange_short_contracts,
                equity_btc,
                mark_price,
                self.spec,
                self.limits,
                bypass_cap=self.bypass_cap,
            )
        except RiskLimitViolation as exc:
            self.db.add_event(
                "ERROR", "order_rejected_pre_trade", str(exc), {"intent": _intent_dict(intent)}
            )
            raise
        reduce_only = intent.side is OrderSide.BUY  # BUY is reduce-only by construction
        cid = self.new_client_order_id()
        request = OrderRequest(
            symbol=self.symbol,
            side=intent.side,
            order_type=order_type,
            quantity=qty,
            client_order_id=cid,
            reduce_only=reduce_only,
            price=price,
            time_in_force=time_in_force,
        )
        # write-ahead: the DB knows about the order before the exchange does
        self.db.upsert_order(
            client_order_id=cid,
            symbol=self.symbol,
            side=intent.side.value,
            order_type=order_type.value,
            requested_qty=qty,
            filled_qty=ZERO,
            limit_price=price,
            status=OrderState.PENDING_SUBMIT.value,
            reduce_only=reduce_only,
            reason=intent.reason,
            target_ratio=intent.target_ratio,
            mode=self.mode,
        )
        log.info(
            "submitting order",
            fields={
                "cid": cid,
                "side": intent.side.value,
                "qty": str(qty),
                "type": order_type.value,
                "reduce_only": reduce_only,
            },
        )
        try:
            order = self.exchange.place_order(request)
        except ExchangeAPIError as exc:
            self.consecutive_rejects += 1
            self.db.upsert_order(
                client_order_id=cid, status=OrderState.REJECTED.value, last_error=str(exc)
            )
            self.db.add_event("ERROR", "order_rejected", str(exc), {"cid": cid})
            return FillSummary(
                cid, None, intent.side, qty, ZERO, None, ZERO, ZERO, OrderState.REJECTED
            )
        except (ExchangeTimeout, ExchangeUnavailable) as exc:
            self.db.upsert_order(
                client_order_id=cid, status=OrderState.UNCERTAIN.value, last_error=str(exc)
            )
            log.warning(
                "order outcome uncertain, querying exchange", fields={"cid": cid, "error": str(exc)}
            )
            resolved = self.resolve_uncertain(cid)  # raises UnresolvedOrderError if still unknown
            if resolved is None:
                return FillSummary(
                    cid, None, intent.side, qty, ZERO, None, ZERO, ZERO, OrderState.NOT_PLACED
                )
            order = resolved
        self.consecutive_rejects = 0
        self._record_exchange_order(order)
        if wait:
            order = self.wait_terminal(cid, order)
        return self.collect_fills(cid, order)

    # ---------------------------------------------------- uncertainty handling
    def resolve_uncertain(self, cid: str) -> ExchangeOrder | None:
        """Query the exchange until the order is found or provably absent.

        Returns the exchange order, or ``None`` when the exchange twice reported -2013
        (order does not exist) after a grace period — i.e. the order was NOT placed.
        Raises :class:`UnresolvedOrderError` if the exchange stays unreachable.
        """
        not_found = 0
        for attempt in range(1, self.max_query_attempts + 1):
            try:
                order = self.exchange.query_order(self.symbol, client_order_id=cid)
                self._record_exchange_order(order)
                return order
            except ExchangeAPIError as exc:
                if exc.code == NO_SUCH_ORDER:
                    not_found += 1
                    if not_found >= 2:
                        self.db.upsert_order(
                            client_order_id=cid,
                            status=OrderState.NOT_PLACED.value,
                            last_error=str(exc),
                        )
                        self.db.add_event(
                            "WARNING",
                            "order_not_placed",
                            "exchange reports no such order twice",
                            {"cid": cid},
                        )
                        return None
                    self._sleep(self.query_grace)
                    continue
                raise UnresolvedOrderError(f"query failed for {cid}: {exc}") from exc
            except (ExchangeTimeout, ExchangeUnavailable):
                self._sleep(min(30.0, 0.5 * (2 ** (attempt - 1))))
        self.db.add_event(
            "CRITICAL", "order_unresolved", "could not determine order outcome", {"cid": cid}
        )
        raise UnresolvedOrderError(
            f"order {cid} outcome unknown after {self.max_query_attempts} queries"
        )

    def _record_exchange_order(self, order: ExchangeOrder) -> None:
        state = status_to_state(order.status)
        self.db.upsert_order(
            client_order_id=order.client_order_id,
            exchange_order_id=order.order_id,
            filled_qty=order.executed_qty,
            avg_price=order.avg_price,
            status=state.value,
            raw=order.raw,
        )

    # ------------------------------------------------------------- waiting
    def wait_terminal(
        self, cid: str, order: ExchangeOrder, timeout: float | None = None
    ) -> ExchangeOrder:
        deadline = self._clock() + (timeout if timeout is not None else self.order_timeout)
        current = order
        while not current.status.is_terminal and self._clock() < deadline:
            self._sleep(self.poll_interval)
            try:
                current = self.exchange.query_order(self.symbol, client_order_id=cid)
                self._record_exchange_order(current)
            except (ExchangeTimeout, ExchangeUnavailable) as exc:
                log.warning("poll failed", fields={"cid": cid, "error": str(exc)})
        return current

    def cancel(self, cid: str) -> ExchangeOrder | None:
        order: ExchangeOrder | None
        try:
            order = self.exchange.cancel_order(self.symbol, client_order_id=cid)
        except ExchangeAPIError as exc:
            # -2011: already filled/cancelled — refresh the true state
            log.info("cancel rejected, refreshing", fields={"cid": cid, "error": str(exc)})
            try:
                order = self.exchange.query_order(self.symbol, client_order_id=cid)
            except ExchangeError:
                return None
        self._record_exchange_order(order)
        return order

    def cancel_all(self) -> int:
        open_orders = self.exchange.open_orders(self.symbol)
        if open_orders:
            self.exchange.cancel_all_open_orders(self.symbol)
        n = 0
        for rec in self.unresolved_orders():
            if rec.status in (
                OrderState.NEW.value,
                OrderState.PARTIALLY_FILLED.value,
                OrderState.SUBMITTED.value,
            ):
                try:
                    o = self.exchange.query_order(self.symbol, client_order_id=rec.client_order_id)
                    self._record_exchange_order(o)
                except ExchangeError:
                    self.db.upsert_order(
                        client_order_id=rec.client_order_id, status=OrderState.CANCELED.value
                    )
                n += 1
        return n

    # ---------------------------------------------------------------- fills
    def collect_fills(self, cid: str, order: ExchangeOrder) -> FillSummary:
        trades: list[ExchangeTrade] = []
        if order.executed_qty > ZERO:
            try:
                trades = self.exchange.user_trades(self.symbol, order_id=order.order_id)
            except ExchangeError as exc:
                log.warning("could not fetch user trades", fields={"cid": cid, "error": str(exc)})
        commission = ZERO
        realized = ZERO
        for t in trades:
            commission += t.commission
            realized += t.realized_pnl
            self.db.add_fill_if_new(
                trade_id=t.trade_id,
                client_order_id=cid,
                exchange_order_id=order.order_id,
                symbol=self.symbol,
                side=t.side.value,
                price=t.price,
                qty=t.qty,
                base_qty_btc=t.base_qty_btc,
                realized_pnl=t.realized_pnl,
                commission=t.commission,
                maker=t.maker,
                time=t.time,
            )
        avg = order.avg_price
        if avg is None and trades:
            total_qty = sum((t.qty for t in trades), ZERO)
            inv = sum((t.qty / t.price for t in trades), ZERO)
            avg = total_qty / inv if inv > 0 else None
        state = status_to_state(order.status)
        return FillSummary(
            cid,
            order.order_id,
            order.side,
            order.orig_qty,
            order.executed_qty,
            avg,
            commission,
            realized,
            state,
            trades,
        )

    # ------------------------------------------------------------- recovery
    def recover(self) -> dict[str, Any]:
        """Restart recovery: resolve every non-terminal DB order and detect unknown exchange orders."""
        report: dict[str, Any] = {
            "resolved": [],
            "not_placed": [],
            "unresolved": [],
            "unknown_exchange_orders": [],
        }
        for rec in self.unresolved_orders():
            try:
                order = self.resolve_uncertain(rec.client_order_id)
            except UnresolvedOrderError:
                report["unresolved"].append(rec.client_order_id)
                continue
            if order is None:
                report["not_placed"].append(rec.client_order_id)
            else:
                if not order.status.is_terminal:
                    order = self.wait_terminal(
                        rec.client_order_id, order, timeout=self.order_timeout
                    )
                self.collect_fills(rec.client_order_id, order)
                report["resolved"].append(rec.client_order_id)
        known = self.known_client_ids()
        for o in self.exchange.open_orders(self.symbol):
            if o.client_order_id not in known:
                report["unknown_exchange_orders"].append(o.client_order_id)
        return report


def _intent_dict(intent: OrderIntent) -> dict[str, Any]:
    return {
        "side": intent.side.value,
        "qty": str(intent.qty),
        "reason": intent.reason,
        "target_ratio": str(intent.target_ratio),
    }
