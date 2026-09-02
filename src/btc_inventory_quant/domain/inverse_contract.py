"""COIN-M inverse perpetual contract mathematics (Decimal based).

All money-related computations in this module use :class:`decimal.Decimal`.
Float/NumPy is deliberately *not* used here because contract quantities, fees,
funding and equity feed real orders.

Formulas (see docs/inverse_math.md and docs/exchange_api_verification.md):

* Short PnL (BTC)          = contracts × contract_value_usd × (1/exit − 1/entry)
* Notional (BTC)           = contracts × contract_value_usd / price
* Trading fee (BTC)        = contracts × contract_value_usd / trade_price × fee_rate
                             (official FAQ: "Notional Value = (Number of Contracts * Contract Size) / Trade Price")
* Funding payment (BTC)    = −signed_contracts × contract_value_usd / mark_price × funding_rate
                             (positive rate ⇒ longs pay shorts; a short position receives)
* Target contracts         = floor(equity_btc × mark × target_ratio / contract_value_usd) → step rounding
* Effective hedge ratio    = |short_contracts| × contract_value_usd / (equity_btc × mark)
* Inverse average entry    = Σ qty_i / Σ (qty_i / price_i)   (harmonic, quantity-weighted)

Contract parameters (contract size, lot step, tick) are NEVER hard-coded: they are
read from the exchange ``exchangeInfo`` payload (see :meth:`ContractSpec.from_exchange_info_symbol`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")
SATOSHI = Decimal("0.00000001")


class InverseMathError(ValueError):
    """Raised for invalid inputs to inverse contract math."""


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


def D(value: Any) -> Decimal:  # noqa: N802 - short constructor helper used everywhere
    """Convert ints/strs/Decimals to Decimal exactly; floats go through ``str`` to avoid binary noise."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:  # pragma: no cover - defensive
        raise InverseMathError(f"cannot convert {value!r} to Decimal") from exc


def quantize_btc(amount: Decimal, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    """Quantize a BTC amount to 8 decimals (baseAssetPrecision = 8)."""
    return amount.quantize(SATOSHI, rounding=rounding)


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """Contract specification read from the exchange (never hard-coded).

    ``contract_value_usd`` is Binance's ``contractSize`` (100 USD for BTCUSD_PERP at the
    time of the verified snapshot); quantities are whole contracts (``stepSize`` = 1).
    """

    symbol: str
    pair: str
    contract_value_usd: Decimal
    qty_step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_max_qty: Decimal
    price_tick: Decimal
    price_precision: int
    quantity_precision: int
    margin_asset: str = "BTC"
    contract_type: str = "PERPETUAL"
    source: str = ""
    onboard_date_ms: int | None = None
    maint_margin_percent: Decimal | None = None
    liquidation_fee_rate: Decimal | None = None

    def __post_init__(self) -> None:
        if self.contract_value_usd <= ZERO:
            raise InverseMathError("contract_value_usd must be positive")
        if self.qty_step <= ZERO or self.min_qty <= ZERO or self.price_tick <= ZERO:
            raise InverseMathError("qty_step/min_qty/price_tick must be positive")

    @classmethod
    def from_exchange_info_symbol(
        cls, sym: Mapping[str, Any], source: str = "exchangeInfo"
    ) -> ContractSpec:
        """Build from one element of ``exchangeInfo.symbols`` (Binance COIN-M schema).

        Field names follow the official documentation (verified 2026-09-02):
        ``contractSize``, ``filters[PRICE_FILTER].tickSize``, ``filters[LOT_SIZE]``,
        ``filters[MARKET_LOT_SIZE]``, ``pricePrecision``, ``quantityPrecision``, ``marginAsset``.
        """
        filters = {f["filterType"]: f for f in sym.get("filters", [])}
        try:
            price_filter = filters["PRICE_FILTER"]
            lot = filters["LOT_SIZE"]
        except KeyError as exc:
            raise InverseMathError(
                f"exchangeInfo symbol {sym.get('symbol')} lacks filter {exc}"
            ) from exc
        market_lot = filters.get("MARKET_LOT_SIZE", lot)
        return cls(
            symbol=str(sym["symbol"]),
            pair=str(sym.get("pair", "")),
            contract_value_usd=D(sym["contractSize"]),
            qty_step=D(lot["stepSize"]),
            min_qty=D(lot["minQty"]),
            max_qty=D(lot["maxQty"]),
            market_max_qty=D(market_lot["maxQty"]),
            price_tick=D(price_filter["tickSize"]),
            price_precision=int(sym["pricePrecision"]),
            quantity_precision=int(sym["quantityPrecision"]),
            margin_asset=str(sym.get("marginAsset", "BTC")),
            contract_type=str(sym.get("contractType", "PERPETUAL")),
            source=source,
            onboard_date_ms=int(sym["onboardDate"]) if "onboardDate" in sym else None,
            maint_margin_percent=D(sym["maintMarginPercent"]) / D(100)
            if "maintMarginPercent" in sym
            else None,
            liquidation_fee_rate=D(sym["liquidationFee"]) if "liquidationFee" in sym else None,
        )

    # --- rounding helpers -------------------------------------------------
    def round_qty_down(self, qty: Decimal) -> Decimal:
        """Round a contract quantity DOWN to the lot step (never rounds up: conservative)."""
        if qty < ZERO:
            raise InverseMathError("qty must be non-negative")
        steps = (qty / self.qty_step).to_integral_value(rounding=ROUND_DOWN)
        return (steps * self.qty_step).quantize(self.qty_step)

    def round_price(self, price: Decimal, rounding: str = ROUND_HALF_EVEN) -> Decimal:
        ticks = (price / self.price_tick).to_integral_value(rounding=rounding)
        return (ticks * self.price_tick).quantize(self.price_tick)

    def validate_qty(self, qty: Decimal, *, market: bool = False) -> None:
        if qty <= ZERO:
            raise InverseMathError("order quantity must be positive")
        if qty < self.min_qty:
            raise InverseMathError(f"qty {qty} below minQty {self.min_qty}")
        if (qty / self.qty_step) != (qty / self.qty_step).to_integral_value():
            raise InverseMathError(f"qty {qty} not a multiple of stepSize {self.qty_step}")
        cap = self.market_max_qty if market else self.max_qty
        if qty > cap:
            raise InverseMathError(f"qty {qty} above max {cap} ({'market' if market else 'limit'})")


# ---------------------------------------------------------------------------
# Core formulas
# ---------------------------------------------------------------------------
def _check_price(price: Decimal, name: str = "price") -> None:
    if price <= ZERO:
        raise InverseMathError(f"{name} must be positive, got {price}")


def notional_usd(contracts: Decimal, contract_value_usd: Decimal) -> Decimal:
    """USD face value of ``contracts`` (unsigned)."""
    return abs(contracts) * contract_value_usd


def notional_btc(contracts: Decimal, contract_value_usd: Decimal, price: Decimal) -> Decimal:
    """BTC value of ``contracts`` at ``price`` (unsigned): contracts × CV / price."""
    _check_price(price)
    return abs(contracts) * contract_value_usd / price


def short_pnl_btc(
    contracts: Decimal, contract_value_usd: Decimal, entry_price: Decimal, exit_price: Decimal
) -> Decimal:
    """Realised/unrealised PnL in BTC of a SHORT of ``contracts`` opened at ``entry_price``.

    PnL_BTC = contracts × CV × (1/exit − 1/entry). Positive when price falls.
    """
    _check_price(entry_price, "entry_price")
    _check_price(exit_price, "exit_price")
    if contracts < ZERO:
        raise InverseMathError("contracts must be unsigned (non-negative)")
    return contracts * contract_value_usd * (ONE / exit_price - ONE / entry_price)


def long_pnl_btc(
    contracts: Decimal, contract_value_usd: Decimal, entry_price: Decimal, exit_price: Decimal
) -> Decimal:
    """PnL in BTC of a LONG (only used to *detect* forbidden long exposure)."""
    return -short_pnl_btc(contracts, contract_value_usd, entry_price, exit_price)


def position_pnl_btc(
    signed_contracts: Decimal,
    contract_value_usd: Decimal,
    entry_price: Decimal,
    exit_price: Decimal,
) -> Decimal:
    """PnL for a signed position (negative contracts = short)."""
    if signed_contracts == ZERO:
        return ZERO
    if signed_contracts < ZERO:
        return short_pnl_btc(-signed_contracts, contract_value_usd, entry_price, exit_price)
    return long_pnl_btc(signed_contracts, contract_value_usd, entry_price, exit_price)


def unrealized_pnl_btc(
    signed_contracts: Decimal,
    contract_value_usd: Decimal,
    entry_price: Decimal | None,
    mark_price: Decimal,
) -> Decimal:
    """Unrealised PnL at ``mark_price`` (mark price is used for equity/risk, never the trade price)."""
    if signed_contracts == ZERO or entry_price is None:
        return ZERO
    return position_pnl_btc(signed_contracts, contract_value_usd, entry_price, mark_price)


def trading_fee_btc(
    contracts: Decimal, contract_value_usd: Decimal, trade_price: Decimal, fee_rate: Decimal
) -> Decimal:
    """Commission in BTC = notional_btc(at trade price) × fee_rate (official COIN-M fee FAQ)."""
    if fee_rate < ZERO:
        # negative maker rebates exist for high VIP tiers; they are allowed but must be explicit
        pass
    return notional_btc(contracts, contract_value_usd, trade_price) * fee_rate


def funding_payment_btc(
    signed_contracts: Decimal,
    contract_value_usd: Decimal,
    mark_price: Decimal,
    funding_rate: Decimal,
) -> Decimal:
    """Wallet change (BTC) at a funding settlement.

    Nominal value (BTC) = |contracts| × CV / mark_price. Positive funding rate: longs pay shorts,
    so a short position (negative signed contracts) *receives* a positive amount.
    """
    if signed_contracts == ZERO:
        return ZERO
    nominal = notional_btc(signed_contracts, contract_value_usd, mark_price)
    payment = nominal * funding_rate
    return payment if signed_contracts < ZERO else -payment


def target_short_notional_usd(
    equity_btc: Decimal, mark_price: Decimal, target_hedge_ratio: Decimal
) -> Decimal:
    _check_price(mark_price, "mark_price")
    if equity_btc < ZERO:
        raise InverseMathError("equity_btc must be non-negative")
    if not (ZERO <= target_hedge_ratio <= ONE):
        raise InverseMathError(
            f"target_hedge_ratio must be within [0, 1], got {target_hedge_ratio}"
        )
    return equity_btc * mark_price * target_hedge_ratio


def target_contracts(
    equity_btc: Decimal, mark_price: Decimal, target_hedge_ratio: Decimal, spec: ContractSpec
) -> Decimal:
    """Whole-contract target: floor(notional / CV) then rounded down to the lot step."""
    notional = target_short_notional_usd(equity_btc, mark_price, target_hedge_ratio)
    raw = (notional / spec.contract_value_usd).to_integral_value(rounding=ROUND_DOWN)
    return spec.round_qty_down(raw)


def effective_hedge_ratio(
    short_contracts: Decimal, contract_value_usd: Decimal, equity_btc: Decimal, mark_price: Decimal
) -> Decimal:
    """|short| × CV / (equity_btc × mark). Raises if equity is not positive (a critical state)."""
    _check_price(mark_price, "mark_price")
    if equity_btc <= ZERO:
        raise InverseMathError("equity_btc must be positive to compute a hedge ratio")
    return abs(short_contracts) * contract_value_usd / (equity_btc * mark_price)


def hedge_ratio_error(target_ratio: Decimal, effective_ratio: Decimal) -> Decimal:
    return effective_ratio - target_ratio


def average_entry_price_inverse(lots: Iterable[tuple[Decimal, Decimal]]) -> Decimal | None:
    """Quantity-weighted harmonic mean: Σ qty / Σ (qty / price).

    For inverse contracts the BTC-denominated exposure of a lot is qty × CV / price, so the
    break-even price of the combined position is the harmonic mean, *not* the arithmetic mean.
    Returns ``None`` for an empty position.
    """
    total_qty = ZERO
    inv_sum = ZERO
    for qty, price in lots:
        if qty <= ZERO:
            raise InverseMathError("lot qty must be positive")
        _check_price(price)
        total_qty += qty
        inv_sum += qty / price
    if total_qty == ZERO:
        return None
    return total_qty / inv_sum


def combine_entry_price(
    existing_qty: Decimal, existing_avg: Decimal | None, add_qty: Decimal, add_price: Decimal
) -> Decimal:
    """New average entry after adding ``add_qty`` at ``add_price`` (inverse-correct)."""
    if existing_qty == ZERO or existing_avg is None:
        return add_price
    result = average_entry_price_inverse([(existing_qty, existing_avg), (add_qty, add_price)])
    assert result is not None
    return result


def maintenance_margin_btc(
    contracts: Decimal,
    contract_value_usd: Decimal,
    price: Decimal,
    maint_margin_rate: Decimal,
    maint_amount_btc: Decimal = ZERO,
) -> Decimal:
    """Maintenance margin (BTC) = contracts × CV / price × MMR − cum (per official FAQ example)."""
    return notional_btc(contracts, contract_value_usd, price) * maint_margin_rate - maint_amount_btc


def estimate_short_liquidation_price(
    wallet_balance_btc: Decimal,
    short_contracts: Decimal,
    contract_value_usd: Decimal,
    entry_price: Decimal,
    maint_margin_rate: Decimal,
    maint_amount_btc: Decimal = ZERO,
) -> Decimal | None:
    """Estimated liquidation price of a lone SHORT under cross margin (no other positions).

    Solves  wallet + c·CV·(1/P − 1/entry) = c·CV/P·MMR − cum  for P:
        P = c·CV·(1 − MMR) / (c·CV/entry − wallet − cum)
    Returns ``None`` when no finite liquidation price exists (denominator ≤ 0), which is the
    case whenever wallet ≥ c·CV/entry, i.e. effective leverage ≤ 1. The exchange's
    ``positionRisk.liquidationPrice`` is authoritative; this is a monitoring estimate only.
    """
    if short_contracts <= ZERO:
        return None
    _check_price(entry_price, "entry_price")
    face_btc = short_contracts * contract_value_usd / entry_price
    denominator = face_btc - wallet_balance_btc - maint_amount_btc
    if denominator <= ZERO:
        return None
    return short_contracts * contract_value_usd * (ONE - maint_margin_rate) / denominator


def side_of(signed_contracts: Decimal) -> PositionSide:
    if signed_contracts < ZERO:
        return PositionSide.SHORT
    if signed_contracts > ZERO:
        return PositionSide.LONG
    return PositionSide.FLAT
