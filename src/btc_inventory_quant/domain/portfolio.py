"""Portfolio engine: wallet balance + inverse position + ledger → equity, hedge ratio.

* wallet_balance_btc: realised BTC (opening + external flows + realised pnl − fees + funding)
* equity_btc  = wallet_balance_btc + unrealised_pnl(mark)
* equity_usd  = equity_btc × mark
* effective_hedge_ratio = |short| × CV / (equity_btc × mark)

Mark price is used for every valuation; trade prices only for fills and fees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from btc_inventory_quant.domain.inverse_contract import (
    ZERO,
    ContractSpec,
    InverseMathError,
    OrderSide,
    effective_hedge_ratio,
    estimate_short_liquidation_price,
    funding_payment_btc,
    target_contracts,
)
from btc_inventory_quant.domain.ledger import Ledger, LedgerEntry, LedgerEntryType
from btc_inventory_quant.domain.position import FillResult, InversePosition


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    ts: datetime
    mark_price: Decimal
    wallet_balance_btc: Decimal
    unrealized_pnl_btc: Decimal
    equity_btc: Decimal
    equity_usd: Decimal
    short_contracts: Decimal
    short_notional_usd: Decimal
    avg_entry_price: Decimal | None
    effective_hedge_ratio: Decimal
    liquidation_price_estimate: Decimal | None


@dataclass(slots=True)
class Portfolio:
    spec: ContractSpec
    ledger: Ledger = field(default_factory=Ledger)
    position: InversePosition = field(init=False)
    wallet_balance_btc: Decimal = ZERO
    maint_margin_rate: Decimal = Decimal(
        "0.004"
    )  # bracket-1 estimate; runtime uses leverageBracket

    def __post_init__(self) -> None:
        self.position = InversePosition(spec=self.spec, allow_long=False)

    # --- construction ------------------------------------------------------
    @classmethod
    def open(cls, spec: ContractSpec, opening_balance_btc: Decimal, ts: datetime) -> Portfolio:
        p = cls(spec=spec)
        if opening_balance_btc < ZERO:
            raise InverseMathError("opening balance must be non-negative")
        p.wallet_balance_btc = opening_balance_btc
        p.ledger.append(
            LedgerEntry(
                ts, LedgerEntryType.OPENING_BALANCE, opening_balance_btc, note="opening balance"
            )
        )
        return p

    # --- external flows (never performance) --------------------------------
    def deposit(self, amount_btc: Decimal, ts: datetime, ref_id: str = "") -> None:
        if amount_btc <= ZERO:
            raise InverseMathError("deposit must be positive")
        self.wallet_balance_btc += amount_btc
        self.ledger.append(
            LedgerEntry(
                ts, LedgerEntryType.DEPOSIT, amount_btc, ref_id=ref_id, note="external deposit"
            )
        )

    def withdraw(self, amount_btc: Decimal, ts: datetime, ref_id: str = "") -> None:
        if amount_btc <= ZERO:
            raise InverseMathError("withdrawal must be positive")
        if amount_btc > self.wallet_balance_btc:
            raise InverseMathError("withdrawal exceeds wallet balance")
        self.wallet_balance_btc -= amount_btc
        self.ledger.append(
            LedgerEntry(
                ts,
                LedgerEntryType.WITHDRAWAL,
                -amount_btc,
                ref_id=ref_id,
                note="external withdrawal",
            )
        )

    # --- trading events ----------------------------------------------------
    def apply_fill(
        self,
        side: OrderSide,
        qty: Decimal,
        price: Decimal,
        fee_rate: Decimal,
        ts: datetime,
        ref_id: str = "",
        reference_price: Decimal | None = None,
        fee_btc: Decimal | None = None,
    ) -> FillResult:
        result = self.position.apply_fill(side, qty, price, fee_rate, fee_btc=fee_btc)
        if result.realized_pnl_btc != ZERO:
            self.wallet_balance_btc += result.realized_pnl_btc
            self.ledger.append(
                LedgerEntry(
                    ts,
                    LedgerEntryType.REALIZED_PNL,
                    result.realized_pnl_btc,
                    ref_id=ref_id,
                    price=price,
                    contracts=result.closed_qty,
                    note=f"{side.value} close {result.closed_qty}",
                )
            )
        if result.fee_btc != ZERO:
            self.wallet_balance_btc -= result.fee_btc
            self.ledger.append(
                LedgerEntry(
                    ts,
                    LedgerEntryType.COMMISSION,
                    -result.fee_btc,
                    ref_id=ref_id,
                    price=price,
                    contracts=qty,
                    note=f"fee rate {fee_rate}",
                )
            )
        if reference_price is not None and reference_price != price:
            # Informational slippage cost (BTC) already embedded in the fill price:
            #   SELL filled at P_f instead of P_r changes eventual short PnL by qty·CV·(1/P_r − 1/P_f)
            #   BUY  filled at P_f instead of P_r changes eventual short PnL by qty·CV·(1/P_f − 1/P_r)
            # A negative amount is a cost (fill worse than reference); positive is price improvement.
            cv = self.spec.contract_value_usd
            one = Decimal(1)
            if side is OrderSide.SELL:
                pnl_change = qty * cv * (one / reference_price - one / price)
            else:
                pnl_change = qty * cv * (one / price - one / reference_price)
            self.ledger.append(
                LedgerEntry(
                    ts,
                    LedgerEntryType.SLIPPAGE,
                    pnl_change,
                    ref_id=ref_id,
                    price=price,
                    contracts=qty,
                    note=f"reference {reference_price}",
                )
            )
        return result

    def apply_funding(
        self, funding_rate: Decimal, mark_price: Decimal, ts: datetime, ref_id: str = ""
    ) -> Decimal:
        payment = funding_payment_btc(
            self.position.contracts, self.spec.contract_value_usd, mark_price, funding_rate
        )
        if payment != ZERO:
            self.wallet_balance_btc += payment
            self.ledger.append(
                LedgerEntry(
                    ts,
                    LedgerEntryType.FUNDING_FEE,
                    payment,
                    ref_id=ref_id,
                    price=mark_price,
                    contracts=self.position.abs_contracts,
                    note=f"rate {funding_rate}",
                )
            )
        return payment

    # --- valuation ---------------------------------------------------------
    def unrealized_pnl_btc(self, mark_price: Decimal) -> Decimal:
        return self.position.unrealized_pnl_btc(mark_price)

    def equity_btc(self, mark_price: Decimal) -> Decimal:
        return self.wallet_balance_btc + self.unrealized_pnl_btc(mark_price)

    def equity_usd(self, mark_price: Decimal) -> Decimal:
        return self.equity_btc(mark_price) * mark_price

    def effective_hedge_ratio(self, mark_price: Decimal) -> Decimal:
        return effective_hedge_ratio(
            self.position.short_contracts,
            self.spec.contract_value_usd,
            self.equity_btc(mark_price),
            mark_price,
        )

    def target_contracts(self, mark_price: Decimal, target_ratio: Decimal) -> Decimal:
        return target_contracts(self.equity_btc(mark_price), mark_price, target_ratio, self.spec)

    def snapshot(self, ts: datetime, mark_price: Decimal) -> PortfolioSnapshot:
        eq = self.equity_btc(mark_price)
        return PortfolioSnapshot(
            ts=ts,
            mark_price=mark_price,
            wallet_balance_btc=self.wallet_balance_btc,
            unrealized_pnl_btc=self.unrealized_pnl_btc(mark_price),
            equity_btc=eq,
            equity_usd=eq * mark_price,
            short_contracts=self.position.short_contracts,
            short_notional_usd=self.position.notional_usd(),
            avg_entry_price=self.position.avg_entry_price,
            effective_hedge_ratio=(
                self.effective_hedge_ratio(mark_price) if eq > ZERO else Decimal("Infinity")
            ),
            liquidation_price_estimate=estimate_short_liquidation_price(
                self.wallet_balance_btc,
                self.position.short_contracts,
                self.spec.contract_value_usd,
                self.position.avg_entry_price or mark_price,
                self.maint_margin_rate,
            ),
        )

    def check_ledger_invariant(self) -> None:
        self.ledger.check_invariant(self.wallet_balance_btc)
