"""Required inverse-contract math tests (prompt §2 test cases 1-4 and more)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from btc_inventory_quant.domain.inverse_contract import (
    ContractSpec,
    InverseMathError,
    OrderSide,
    PositionSide,
    average_entry_price_inverse,
    combine_entry_price,
    effective_hedge_ratio,
    estimate_short_liquidation_price,
    funding_payment_btc,
    maintenance_margin_btc,
    notional_btc,
    short_pnl_btc,
    target_contracts,
    target_short_notional_usd,
    trading_fee_btc,
    unrealized_pnl_btc,
)
from btc_inventory_quant.domain.position import InversePosition, PositionFlipError
from btc_inventory_quant.risk.limits import RiskLimitViolation, validate_order_against_position

D = Decimal


def test_contract_spec_is_read_from_exchange_info(spec: ContractSpec) -> None:
    assert spec.symbol == "BTCUSD_PERP"
    assert spec.contract_value_usd == D("100")
    assert spec.qty_step == D("1")
    assert spec.min_qty == D("1")
    assert spec.market_max_qty == D("60000")
    assert spec.price_tick == D("0.1")
    assert spec.margin_asset == "BTC"
    assert spec.source == "fixture"


def test_case_1_price_halves_short_earns_one_btc(spec: ContractSpec) -> None:
    equity0 = D("1")
    entry = D("100000")
    contracts = target_contracts(equity0, entry, D("1"), spec)  # 100% notional for the math check
    assert contracts == D("1000")  # 100,000 USD / 100 USD
    exit_price = D("50000")
    pnl = short_pnl_btc(contracts, spec.contract_value_usd, entry, exit_price)
    assert pnl == D("1")
    equity = equity0 + pnl
    assert equity == D("2")
    assert equity * exit_price == D("100000")


def test_case_2_price_doubles_short_loses_half_btc(spec: ContractSpec) -> None:
    equity0 = D("1")
    entry = D("100000")
    contracts = D("1000")
    exit_price = D("200000")
    pnl = short_pnl_btc(contracts, spec.contract_value_usd, entry, exit_price)
    assert pnl == D("-0.5")
    equity = equity0 + pnl
    assert equity == D("0.5")
    assert equity * exit_price == D("100000")


@pytest.mark.parametrize("price_mult", ["1.5", "2", "5", "10", "100", "1000000"])
def test_case_3_85pct_hedge_never_flips_to_long_on_rally(
    spec: ContractSpec, price_mult: str
) -> None:
    equity0 = D("1")
    entry = D("100000")
    contracts = target_contracts(equity0, entry, D("0.85"), spec)
    assert contracts == D("850")
    pos = InversePosition(spec=spec)
    pos.apply_fill(OrderSide.SELL, contracts, entry, D("0"))
    mark = entry * D(price_mult)
    upnl = unrealized_pnl_btc(pos.contracts, spec.contract_value_usd, pos.avg_entry_price, mark)
    equity = equity0 + upnl
    # the position itself never changes sign because of price moves
    assert pos.side is PositionSide.SHORT
    assert pos.contracts == D("-850")
    # the short can lose at most its BTC face value at entry (0.85 BTC) → equity stays > 0.15 BTC
    assert equity > D("0.15")
    assert equity <= equity0
    # effective hedge ratio decays but stays below 1 → no leverage build-up
    ratio = effective_hedge_ratio(pos.short_contracts, spec.contract_value_usd, equity, mark)
    assert D("0") < ratio < D("1")
    assert (
        estimate_short_liquidation_price(
            equity0, pos.short_contracts, spec.contract_value_usd, entry, D("0.004")
        )
        is None
    )


def test_case_4_buy_exceeding_short_is_rejected(spec: ContractSpec) -> None:
    pos = InversePosition(spec=spec)
    pos.apply_fill(OrderSide.SELL, D("850"), D("100000"), D("0"))
    with pytest.raises(PositionFlipError):
        pos.validate_reduce_only_buy(D("851"))
    with pytest.raises(PositionFlipError):
        pos.apply_fill(OrderSide.BUY, D("851"), D("90000"), D("0"))
    assert pos.contracts == D("-850")  # untouched
    # and a flat position cannot BUY at all
    flat = InversePosition(spec=spec)
    with pytest.raises(PositionFlipError):
        flat.apply_fill(OrderSide.BUY, D("1"), D("90000"), D("0"))
    # exact-size BUY is fine (full close)
    pos.apply_fill(OrderSide.BUY, D("850"), D("90000"), D("0"))
    assert pos.contracts == D("0")
    assert pos.side is PositionSide.FLAT


def test_risk_validator_rejects_buy_exceeding_short(spec: ContractSpec, limits) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RiskLimitViolation) as exc:
        validate_order_against_position(
            OrderSide.BUY, D("2"), D("1"), D("1"), D("100000"), spec, limits
        )
    assert exc.value.code == "BUY_EXCEEDS_SHORT"


def test_fee_formula_matches_official_example(spec: ContractSpec) -> None:
    # official userTrades example: 1 contract at 8800 → baseQty 0.01136364 BTC, commission 0.00000454 at 0.04%
    n = notional_btc(D("1"), spec.contract_value_usd, D("8800"))
    assert n.quantize(D("1e-8")) == D("0.01136364")
    fee = trading_fee_btc(D("1"), spec.contract_value_usd, D("8800"), D("0.0004"))
    assert fee.quantize(D("1e-8")) == D(
        "0.00000455"
    )  # doc shows 0.00000454 (truncation); we round half-even
    assert abs(fee - D("0.00000454")) < D("1e-8")


def test_funding_sign_convention(spec: ContractSpec) -> None:
    short = D("-1000")
    long = D("1000")
    mark = D("50000")
    # positive rate: longs pay shorts. nominal = 100,000 / 50,000 = 2 BTC → 2 × 0.0001 = 0.0002 BTC
    assert funding_payment_btc(short, spec.contract_value_usd, mark, D("0.0001")) == D("0.0002")
    assert funding_payment_btc(long, spec.contract_value_usd, mark, D("0.0001")) == D("-0.0002")
    # negative rate: shorts pay
    assert funding_payment_btc(short, spec.contract_value_usd, mark, D("-0.0002")) == D("-0.0004")
    assert funding_payment_btc(D("0"), spec.contract_value_usd, mark, D("0.01")) == D("0")


def test_target_contracts_floors_to_whole_contracts(spec: ContractSpec) -> None:
    assert target_short_notional_usd(D("1"), D("100000"), D("0.85")) == D("85000")
    assert target_contracts(D("1"), D("100000"), D("0.85"), spec) == D("850")
    assert target_contracts(D("1"), D("100000"), D("0.333"), spec) == D("333")
    assert target_contracts(D("0.7"), D("64321.5"), D("0.45"), spec) == D(
        "202"
    )  # 20261.27 / 100 → 202
    assert target_contracts(D("0.001"), D("50000"), D("0.85"), spec) == D("0")  # below one contract
    with pytest.raises(InverseMathError):
        target_contracts(D("1"), D("100000"), D("1.2"), spec)


def test_effective_hedge_ratio(spec: ContractSpec) -> None:
    assert effective_hedge_ratio(D("850"), spec.contract_value_usd, D("1"), D("100000")) == D(
        "0.85"
    )
    # after a 20% rally with 850 contracts from 100k: equity = 1 + 85000*(1/120000 - 1/100000)
    mark = D("120000")
    eq = D("1") + short_pnl_btc(D("850"), spec.contract_value_usd, D("100000"), mark)
    r = effective_hedge_ratio(D("850"), spec.contract_value_usd, eq, mark)
    assert D("0.80") < r < D("0.85")
    with pytest.raises(InverseMathError):
        effective_hedge_ratio(D("1"), spec.contract_value_usd, D("0"), mark)


def test_inverse_average_entry_is_harmonic(spec: ContractSpec) -> None:
    lots = [(D("100"), D("100000")), (D("100"), D("50000"))]
    avg = average_entry_price_inverse(lots)
    assert avg is not None
    assert avg.quantize(D("0.01")) == D("66666.67")  # arithmetic mean would be 75,000 (wrong)
    # closing everything at 60,000 must give identical pnl via the average or via lots
    cv = spec.contract_value_usd
    pnl_avg = short_pnl_btc(D("200"), cv, avg, D("60000"))
    pnl_lots = sum(short_pnl_btc(q, cv, p, D("60000")) for q, p in lots)
    assert abs(pnl_avg - pnl_lots) < D("1e-18")
    assert combine_entry_price(D("0"), None, D("5"), D("123")) == D("123")
    assert average_entry_price_inverse([]) is None


def test_maintenance_margin_matches_official_example(spec: ContractSpec) -> None:
    # official FAQ: 100 contracts @ 25,000, MMR 0.4% → 0.0016 BTC
    mm = maintenance_margin_btc(D("100"), spec.contract_value_usd, D("25000"), D("0.004"))
    assert mm == D("0.0016")


def test_liquidation_estimate_only_exists_above_1x_leverage(spec: ContractSpec) -> None:
    cv = spec.contract_value_usd
    # 1 BTC wallet, 1000 contracts (100% of equity at 100k) → face 1 BTC = wallet → no liquidation
    assert estimate_short_liquidation_price(D("1"), D("1000"), cv, D("100000"), D("0.004")) is None
    # 0.5 BTC wallet with the same short = 2x leverage → finite liquidation price above entry
    liq = estimate_short_liquidation_price(D("0.5"), D("1000"), cv, D("100000"), D("0.004"))
    assert liq is not None
    assert liq > D("100000")
    assert liq.quantize(D("1")) == D("199200")  # 100000·(1−0.004)/(1−0.5)


def test_round_qty_down_and_qty_validation(spec: ContractSpec) -> None:
    assert spec.round_qty_down(D("12.9")) == D("12")
    assert spec.round_qty_down(D("0.4")) == D("0")
    spec.validate_qty(D("1"), market=True)
    with pytest.raises(InverseMathError):
        spec.validate_qty(D("0.5"))
    with pytest.raises(InverseMathError):
        spec.validate_qty(D("60001"), market=True)
    assert spec.round_price(D("12345.67")) == D("12345.7")


def test_decimal_precision_is_exact_for_case_1() -> None:
    # 1000 × 100 × (1/50000 − 1/100000) must be exactly 1 with Decimal, not 0.9999999
    cv = D("100")
    pnl = short_pnl_btc(D("1000"), cv, D("100000"), D("50000"))
    assert pnl == D("1")
