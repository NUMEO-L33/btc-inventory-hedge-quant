"""Inverse perpetual position accounting (average-entry method + lot audit trail).

The position is *signed*: negative contracts = SHORT, positive = LONG. This project never
opens longs; :class:`InversePosition` enforces that a BUY can only reduce an existing short
(``allow_long=False``) and raises :class:`PositionFlipError` otherwise.

Average entry price uses the inverse-correct harmonic combination
(:func:`combine_entry_price`); realised PnL on a reduce is computed against that average,
matching the exchange's position-level accounting. A FIFO lot list is maintained purely as an
audit trail (lot-based PnL must equal average-based PnL after a full close).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from btc_inventory_quant.domain.inverse_contract import (
    ZERO,
    ContractSpec,
    InverseMathError,
    OrderSide,
    PositionSide,
    average_entry_price_inverse,
    combine_entry_price,
    position_pnl_btc,
    side_of,
    trading_fee_btc,
    unrealized_pnl_btc,
)


class PositionFlipError(InverseMathError):
    """A fill would flip the position through zero or open a forbidden long."""


@dataclass(slots=True)
class Lot:
    qty: Decimal
    price: Decimal


@dataclass(frozen=True, slots=True)
class FillResult:
    side: OrderSide
    qty: Decimal
    price: Decimal
    opened_qty: Decimal
    closed_qty: Decimal
    realized_pnl_btc: Decimal
    fee_btc: Decimal
    contracts_after: Decimal
    avg_entry_after: Decimal | None


@dataclass(slots=True)
class InversePosition:
    spec: ContractSpec
    contracts: Decimal = ZERO  # signed
    avg_entry_price: Decimal | None = None
    lots: list[Lot] = field(default_factory=list)
    allow_long: bool = False

    # --- derived -----------------------------------------------------------
    @property
    def side(self) -> PositionSide:
        return side_of(self.contracts)

    @property
    def abs_contracts(self) -> Decimal:
        return abs(self.contracts)

    @property
    def short_contracts(self) -> Decimal:
        return -self.contracts if self.contracts < ZERO else ZERO

    def notional_usd(self) -> Decimal:
        return self.abs_contracts * self.spec.contract_value_usd

    def unrealized_pnl_btc(self, mark_price: Decimal) -> Decimal:
        return unrealized_pnl_btc(
            self.contracts, self.spec.contract_value_usd, self.avg_entry_price, mark_price
        )

    def lot_avg_entry(self) -> Decimal | None:
        return average_entry_price_inverse((lot.qty, lot.price) for lot in self.lots)

    # --- validation --------------------------------------------------------
    def validate_reduce_only_buy(self, qty: Decimal) -> None:
        """A BUY must never exceed the current short size (never flip to long)."""
        if qty <= ZERO:
            raise InverseMathError("BUY qty must be positive")
        if qty > self.short_contracts:
            raise PositionFlipError(
                f"BUY {qty} contracts exceeds short position {self.short_contracts}; would flip to LONG"
            )

    # --- fills -------------------------------------------------------------
    def apply_fill(
        self,
        side: OrderSide,
        qty: Decimal,
        price: Decimal,
        fee_rate: Decimal,
        fee_btc: Decimal | None = None,
    ) -> FillResult:
        """Apply a fill and return realised PnL / fee. Quantities are unsigned contract counts.

        ``fee_btc`` overrides the rate-based fee with the exchange-reported commission.
        """
        if qty <= ZERO:
            raise InverseMathError("fill qty must be positive")
        if price <= ZERO:
            raise InverseMathError("fill price must be positive")
        cv = self.spec.contract_value_usd
        fee = trading_fee_btc(qty, cv, price, fee_rate) if fee_btc is None else fee_btc
        signed_delta = -qty if side is OrderSide.SELL else qty

        if not self.allow_long:
            if side is OrderSide.BUY:
                self.validate_reduce_only_buy(qty)
            elif self.contracts > ZERO:  # pragma: no cover - long can never exist here
                raise PositionFlipError("unexpected long position")

        realized = ZERO
        opened = ZERO
        closed = ZERO
        same_direction = self.contracts == ZERO or (self.contracts < ZERO) == (signed_delta < ZERO)

        if same_direction:
            # increase (or open) in the same direction: harmonic average update
            self.avg_entry_price = combine_entry_price(
                self.abs_contracts, self.avg_entry_price, qty, price
            )
            self.contracts += signed_delta
            self.lots.append(Lot(qty, price))
            opened = qty
        else:
            # reduce; possibly through zero (only if allow_long — otherwise validated above)
            reducible = min(qty, self.abs_contracts)
            assert self.avg_entry_price is not None
            signed_reduced = reducible if self.contracts > ZERO else -reducible
            realized = position_pnl_btc(signed_reduced, cv, self.avg_entry_price, price)
            closed = reducible
            self._consume_lots(reducible)
            self.contracts -= signed_reduced  # move toward zero
            remainder = qty - reducible
            if self.contracts == ZERO:
                self.avg_entry_price = None
                self.lots.clear()
            if remainder > ZERO:
                if not self.allow_long:  # pragma: no cover - validated earlier
                    raise PositionFlipError("fill would flip the position")
                self.contracts += remainder if signed_delta > ZERO else -remainder
                self.avg_entry_price = price
                self.lots = [Lot(remainder, price)]
                opened = remainder
        return FillResult(
            side=side,
            qty=qty,
            price=price,
            opened_qty=opened,
            closed_qty=closed,
            realized_pnl_btc=realized,
            fee_btc=fee,
            contracts_after=self.contracts,
            avg_entry_after=self.avg_entry_price,
        )

    def lot_based_close_pnl(self, qty: Decimal, price: Decimal) -> Decimal:
        """FIFO lot PnL for closing ``qty`` at ``price`` (audit only, does not mutate)."""
        remaining = qty
        pnl = ZERO
        cv = self.spec.contract_value_usd
        sign = -1 if self.contracts < ZERO else 1
        for lot in self.lots:
            if remaining <= ZERO:
                break
            take = min(lot.qty, remaining)
            pnl += position_pnl_btc(sign * take, cv, lot.price, price)
            remaining -= take
        if remaining > ZERO:
            raise InverseMathError("lot audit: not enough lots to close requested qty")
        return pnl

    def _consume_lots(self, qty: Decimal) -> None:
        remaining = qty
        new_lots: list[Lot] = []
        for lot in self.lots:
            if remaining <= ZERO:
                new_lots.append(lot)
                continue
            take = min(lot.qty, remaining)
            remaining -= take
            if lot.qty > take:
                new_lots.append(Lot(lot.qty - take, lot.price))
        self.lots = new_lots

    def snapshot(self) -> dict[str, str | None]:
        return {
            "symbol": self.spec.symbol,
            "contracts": str(self.contracts),
            "side": self.side.value,
            "avg_entry_price": None if self.avg_entry_price is None else str(self.avg_entry_price),
        }
