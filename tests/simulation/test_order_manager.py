"""Order manager resilience tests on the paper exchange (timeouts, lost responses, rejects, partial fills)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from btc_inventory_quant.config.schema import RiskLimits
from btc_inventory_quant.domain.inverse_contract import OrderSide
from btc_inventory_quant.exchange.base import ExchangeAPIError, ExchangeTimeout
from btc_inventory_quant.exchange.models import OrderStatus
from btc_inventory_quant.exchange.paper import PaperExchange, StaticMarketData
from btc_inventory_quant.execution.order_manager import (
    DuplicateRiskError,
    OrderManager,
    OrderState,
    UnresolvedOrderError,
)
from btc_inventory_quant.execution.policies import (
    ExecutionContext,
    MakerFirstExecution,
    MarketExecution,
    TwapExecution,
)
from btc_inventory_quant.execution.rebalancer import OrderIntent, Rebalancer
from btc_inventory_quant.risk.limits import RiskLimitViolation
from btc_inventory_quant.storage.database import Database

D = Decimal
MARK = D("100000")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(f"sqlite:///{tmp_path}/om.sqlite3")
    d.create_all()
    return d


@pytest.fixture
def exchange(spec) -> PaperExchange:  # type: ignore[no-untyped-def]
    return PaperExchange(StaticMarketData(spec, MARK), spec, initial_btc=D("1"))


@pytest.fixture
def manager(exchange: PaperExchange, db: Database, spec) -> OrderManager:  # type: ignore[no-untyped-def]
    return OrderManager(
        exchange,
        db,
        spec,
        RiskLimits(),
        "BTCUSD_PERP",
        "shadow",
        sleep=lambda s: None,
        poll_interval_seconds=0,
    )


def intent(side: OrderSide, qty: str, short: str = "0", target: str = "0.5") -> OrderIntent:
    return OrderIntent(
        side, D(qty), side is OrderSide.BUY, "rebalance", D(target), D("0"), D(qty), D(short)
    )


def test_market_order_fills_and_is_recorded(
    manager: OrderManager, exchange: PaperExchange, db: Database
) -> None:
    fill = manager.submit(
        intent(OrderSide.SELL, "500"),
        exchange_short_contracts=D("0"),
        equity_btc=D("1"),
        mark_price=MARK,
    )
    assert fill.status is OrderState.FILLED
    assert fill.filled_qty == D("500")
    assert fill.avg_price is not None and fill.avg_price < MARK  # slippage on a SELL
    assert fill.commission_btc > 0
    rec = db.get_order(fill.client_order_id)
    assert rec is not None and rec.status == "FILLED" and rec.exchange_order_id is not None
    assert len(db.fills_for_order(fill.client_order_id)) == 1
    assert exchange.position.contracts == D("-500")
    assert manager.unresolved_orders() == []


def test_lost_response_after_acceptance_does_not_duplicate(
    manager: OrderManager, exchange: PaperExchange, db: Database
) -> None:
    exchange.lose_response_next_place = 1
    fill = manager.submit(
        intent(OrderSide.SELL, "300"),
        exchange_short_contracts=D("0"),
        equity_btc=D("1"),
        mark_price=MARK,
    )
    assert fill.status is OrderState.FILLED
    assert len(exchange.orders) == 1  # exactly one exchange order despite the timeout
    assert exchange.position.contracts == D("-300")
    rec = db.get_order(fill.client_order_id)
    assert rec is not None and rec.status == "FILLED"


def test_timeout_before_acceptance_resolves_to_not_placed_then_resubmits_once(
    manager: OrderManager, exchange: PaperExchange, db: Database
) -> None:
    exchange.fail_next_place = [ExchangeTimeout("network")]  # the exchange never saw the order
    fill = manager.submit(
        intent(OrderSide.SELL, "200"),
        exchange_short_contracts=D("0"),
        equity_btc=D("1"),
        mark_price=MARK,
    )
    assert fill.status is OrderState.NOT_PLACED
    assert len(exchange.orders) == 0
    assert db.get_order(fill.client_order_id).status == "NOT_PLACED"  # type: ignore[union-attr]
    # NOT_PLACED is terminal → a fresh submission is allowed and creates exactly one order
    fill2 = manager.submit(
        intent(OrderSide.SELL, "200"),
        exchange_short_contracts=D("0"),
        equity_btc=D("1"),
        mark_price=MARK,
    )
    assert fill2.status is OrderState.FILLED
    assert len(exchange.orders) == 1
    assert fill2.client_order_id != fill.client_order_id


def test_unreachable_exchange_leaves_order_uncertain_and_blocks_new_orders(
    manager: OrderManager, exchange: PaperExchange, db: Database
) -> None:
    exchange.lose_response_next_place = 1
    exchange.query_failures = 99
    with pytest.raises(UnresolvedOrderError):
        manager.submit(
            intent(OrderSide.SELL, "100"),
            exchange_short_contracts=D("0"),
            equity_btc=D("1"),
            mark_price=MARK,
        )
    uncertain = manager.unresolved_orders()
    assert len(uncertain) == 1 and uncertain[0].status == "UNCERTAIN"
    exchange.query_failures = 0
    with pytest.raises(DuplicateRiskError):
        manager.submit(
            intent(OrderSide.SELL, "100"),
            exchange_short_contracts=D("0"),
            equity_btc=D("1"),
            mark_price=MARK,
        )
    # restart recovery resolves it (the exchange did fill it) and no duplicate is created
    report = manager.recover()
    assert report["resolved"] == [uncertain[0].client_order_id]
    assert manager.unresolved_orders() == []
    assert len(exchange.orders) == 1


def test_rejected_order_is_terminal_and_counted(
    manager: OrderManager, exchange: PaperExchange
) -> None:
    exchange.fail_next_place = [ExchangeAPIError(-2019, "Margin is insufficient.")]
    fill = manager.submit(
        intent(OrderSide.SELL, "100"),
        exchange_short_contracts=D("0"),
        equity_btc=D("1"),
        mark_price=MARK,
    )
    assert fill.status is OrderState.REJECTED
    assert manager.consecutive_rejects == 1
    assert manager.unresolved_orders() == []


def test_buy_exceeding_short_is_refused_before_reaching_the_exchange(
    manager: OrderManager, exchange: PaperExchange
) -> None:
    manager.submit(
        intent(OrderSide.SELL, "100"),
        exchange_short_contracts=D("0"),
        equity_btc=D("1"),
        mark_price=MARK,
    )
    with pytest.raises(RiskLimitViolation):
        manager.submit(
            intent(OrderSide.BUY, "101", short="100"),
            exchange_short_contracts=D("100"),
            equity_btc=D("1"),
            mark_price=MARK,
        )
    assert len(exchange.orders) == 1
    fill = manager.submit(
        intent(OrderSide.BUY, "100", short="100"),
        exchange_short_contracts=D("100"),
        equity_btc=D("1"),
        mark_price=MARK,
    )
    assert fill.status is OrderState.FILLED
    assert exchange.position.contracts == D("0")


def test_partial_market_fill_is_reported(manager: OrderManager, exchange: PaperExchange) -> None:
    exchange.partial_fill_fraction = D("0.4")
    fill = manager.submit(
        intent(OrderSide.SELL, "100"),
        exchange_short_contracts=D("0"),
        equity_btc=D("1"),
        mark_price=MARK,
    )
    assert fill.filled_qty == D("40")
    assert not fill.fully_filled
    assert fill.status is OrderState.EXPIRED  # remainder cancelled by the exchange


def test_unknown_exchange_order_detected_on_recovery(
    manager: OrderManager, exchange: PaperExchange
) -> None:
    exchange.inject_unknown_order()
    report = manager.recover()
    assert len(report["unknown_exchange_orders"]) == 1
    assert manager.cancel_all() >= 0
    assert exchange.open_orders("BTCUSD_PERP") == []


def test_maker_first_policy_fills_at_the_touch_or_falls_back(
    manager: OrderManager, exchange: PaperExchange, spec
) -> None:  # type: ignore[no-untyped-def]
    policy = MakerFirstExecution(manager, exchange, wait_seconds=0, sleep=lambda s: None)
    ctx = ExecutionContext(D("0"), D("1"), MARK)
    # resting limit never fills within the wait → cancelled → market remainder
    report = policy.execute(intent(OrderSide.SELL, "50"), ctx)
    assert report.complete
    assert [f.status for f in report.fills][-1] is OrderState.FILLED
    limit_order = next(o for o in exchange.orders.values() if o.request.order_type.value == "LIMIT")
    assert limit_order.status is OrderStatus.CANCELED
    assert exchange.position.contracts == D("-50")


def test_twap_splits_into_slices(manager: OrderManager, exchange: PaperExchange) -> None:
    policy = TwapExecution(manager, slices=4, interval_seconds=0, sleep=lambda s: None)
    report = policy.execute(intent(OrderSide.SELL, "100"), ExecutionContext(D("0"), D("1"), MARK))
    assert report.complete
    assert len(report.fills) == 4
    assert exchange.position.contracts == D("-100")


def test_market_policy_and_rebalancer_end_to_end(
    manager: OrderManager, exchange: PaperExchange, spec
) -> None:  # type: ignore[no-untyped-def]
    rb = Rebalancer(spec, RiskLimits())
    plan = rb.plan(D("0.8"), D("0"), D("0"), D("1"), MARK)
    assert (
        plan.intent is not None
        and plan.intent.side is OrderSide.SELL
        and plan.intent.qty == D("200")
    )  # capped 0 → 0.2
    report = MarketExecution(manager).execute(plan.intent, ExecutionContext(D("0"), D("1"), MARK))
    assert report.complete and exchange.position.contracts == D("-200")
    # reduce: target 0 from 0.2 → BUY 200 reduce-only, never more than the short
    plan2 = rb.plan(D("0"), D("0.2"), D("200"), D("1"), MARK)
    assert (
        plan2.intent is not None
        and plan2.intent.side is OrderSide.BUY
        and plan2.intent.qty == D("200")
        and plan2.intent.reduce_only
    )
