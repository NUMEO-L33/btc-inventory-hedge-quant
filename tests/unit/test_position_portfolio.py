from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from btc_inventory_quant.domain.inverse_contract import (
    InverseMathError,
    OrderSide,
    PositionSide,
    short_pnl_btc,
)
from btc_inventory_quant.domain.ledger import Ledger, LedgerEntry, LedgerEntryType
from btc_inventory_quant.domain.portfolio import Portfolio
from btc_inventory_quant.domain.position import InversePosition, PositionFlipError

D = Decimal
FEE = D("0.0005")


def test_partial_entries_update_harmonic_average(spec) -> None:  # type: ignore[no-untyped-def]
    pos = InversePosition(spec=spec)
    pos.apply_fill(OrderSide.SELL, D("100"), D("100000"), D("0"))
    pos.apply_fill(OrderSide.SELL, D("100"), D("50000"), D("0"))
    assert pos.contracts == D("-200")
    assert pos.avg_entry_price is not None
    assert pos.avg_entry_price.quantize(D("0.01")) == D("66666.67")
    assert pos.lot_avg_entry() == pos.avg_entry_price


def test_partial_close_realizes_against_average_and_keeps_average(spec) -> None:  # type: ignore[no-untyped-def]
    pos = InversePosition(spec=spec)
    pos.apply_fill(OrderSide.SELL, D("100"), D("100000"), D("0"))
    pos.apply_fill(OrderSide.SELL, D("100"), D("50000"), D("0"))
    avg_before = pos.avg_entry_price
    res = pos.apply_fill(OrderSide.BUY, D("50"), D("40000"), FEE)
    assert res.closed_qty == D("50")
    assert res.opened_qty == D("0")
    expected = short_pnl_btc(D("50"), spec.contract_value_usd, avg_before, D("40000"))
    assert res.realized_pnl_btc == expected
    assert res.realized_pnl_btc > 0
    assert pos.contracts == D("-150")
    assert pos.avg_entry_price == avg_before  # reduce does not change the average
    assert res.fee_btc == D("50") * spec.contract_value_usd / D("40000") * FEE


def test_full_close_lot_pnl_equals_average_pnl(spec) -> None:  # type: ignore[no-untyped-def]
    pos = InversePosition(spec=spec)
    fills = [(D("30"), D("90000")), (D("70"), D("110000")), (D("50"), D("70000"))]
    for q, p in fills:
        pos.apply_fill(OrderSide.SELL, q, p, D("0"))
    exit_price = D("80000")
    lot_pnl = pos.lot_based_close_pnl(D("150"), exit_price)
    res = pos.apply_fill(OrderSide.BUY, D("150"), exit_price, D("0"))
    assert abs(res.realized_pnl_btc - lot_pnl) < D("1e-15")
    assert pos.contracts == D("0")
    assert pos.avg_entry_price is None
    assert pos.lots == []
    assert pos.side is PositionSide.FLAT


def test_never_flips_even_when_lots_allow(spec) -> None:  # type: ignore[no-untyped-def]
    pos = InversePosition(spec=spec)
    pos.apply_fill(OrderSide.SELL, D("10"), D("100000"), D("0"))
    with pytest.raises(PositionFlipError):
        pos.apply_fill(OrderSide.BUY, D("11"), D("100000"), D("0"))
    assert pos.contracts == D("-10")


def test_invalid_fill_inputs(spec) -> None:  # type: ignore[no-untyped-def]
    pos = InversePosition(spec=spec)
    with pytest.raises(InverseMathError):
        pos.apply_fill(OrderSide.SELL, D("0"), D("100000"), D("0"))
    with pytest.raises(InverseMathError):
        pos.apply_fill(OrderSide.SELL, D("1"), D("0"), D("0"))


def test_portfolio_equity_btc_and_usd(spec, t0) -> None:  # type: ignore[no-untyped-def]
    pf = Portfolio.open(spec, D("1"), t0)
    pf.apply_fill(OrderSide.SELL, D("1000"), D("100000"), D("0"), t0)
    assert pf.equity_btc(D("50000")) == D("2")
    assert pf.equity_usd(D("50000")) == D("100000")
    assert pf.equity_btc(D("200000")) == D("0.5")
    assert pf.equity_usd(D("200000")) == D("100000")
    assert pf.effective_hedge_ratio(D("100000")) == D("1")
    snap = pf.snapshot(t0, D("100000"))
    assert snap.short_contracts == D("1000")
    assert snap.short_notional_usd == D("100000")
    assert snap.effective_hedge_ratio == D("1")


def test_fees_and_funding_flow_through_wallet_and_ledger(spec, t0) -> None:  # type: ignore[no-untyped-def]
    pf = Portfolio.open(spec, D("1"), t0)
    res = pf.apply_fill(
        OrderSide.SELL, D("500"), D("100000"), FEE, t0, ref_id="o1", reference_price=D("100050")
    )
    assert pf.wallet_balance_btc == D("1") - res.fee_btc
    pay = pf.apply_funding(D("0.0001"), D("100000"), t0 + timedelta(hours=8))
    assert pay == D("0.00005")  # 50,000/100,000 = 0.5 BTC × 0.0001
    assert pf.wallet_balance_btc == D("1") - res.fee_btc + pay
    totals = pf.ledger.totals()
    assert totals.commission == -res.fee_btc
    assert totals.funding == pay
    assert totals.slippage_cost > 0  # sold below the reference price
    pf.check_ledger_invariant()
    kinds = {e.entry_type for e in pf.ledger.entries}
    assert {
        LedgerEntryType.OPENING_BALANCE,
        LedgerEntryType.COMMISSION,
        LedgerEntryType.FUNDING_FEE,
        LedgerEntryType.SLIPPAGE,
    } <= kinds


def test_external_flows_are_not_trading_pnl(spec, t0) -> None:  # type: ignore[no-untyped-def]
    pf = Portfolio.open(spec, D("1"), t0)
    pf.apply_fill(OrderSide.SELL, D("1000"), D("100000"), D("0"), t0)
    pf.deposit(D("0.5"), t0 + timedelta(days=1), ref_id="dep1")
    pf.apply_fill(
        OrderSide.BUY, D("1000"), D("50000"), D("0"), t0 + timedelta(days=2)
    )  # +1 BTC realized
    pf.withdraw(D("0.2"), t0 + timedelta(days=3), ref_id="wd1")
    assert pf.wallet_balance_btc == D("1") + D("0.5") + D("1") - D("0.2")
    totals = pf.ledger.totals()
    assert totals.net_external_flows == D("0.3")
    assert totals.trading_pnl == D("1")
    assert pf.ledger.net_btc_growth(pf.wallet_balance_btc) == D("1")
    pf.check_ledger_invariant()
    with pytest.raises(InverseMathError):
        pf.withdraw(D("100"), t0 + timedelta(days=4))


def test_ledger_rejects_out_of_order_and_naive_timestamps(spec, t0) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger()
    ledger.append(LedgerEntry(t0, LedgerEntryType.OPENING_BALANCE, D("1")))
    with pytest.raises(ValueError, match="time order"):
        ledger.append(LedgerEntry(t0 - timedelta(days=1), LedgerEntryType.DEPOSIT, D("1")))
    with pytest.raises(ValueError, match="timezone"):
        LedgerEntry(datetime(2024, 1, 1), LedgerEntryType.DEPOSIT, D("1"))
