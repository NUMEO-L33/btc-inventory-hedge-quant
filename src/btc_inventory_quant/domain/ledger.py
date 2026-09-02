"""Append-only BTC ledger separating trading results from external flows.

Invariant (checked by :meth:`Ledger.check_invariant`)::

    wallet_balance = opening_balance + Σ deposits − Σ withdrawals
                     + Σ realized_pnl − Σ commissions + Σ funding ± Σ other

External deposits/withdrawals are *never* counted as trading performance:
``trading_pnl_btc`` = realized + funding − commission (+ other exchange adjustments), and
``net_btc_growth`` = ending wallet − opening − net external flows.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from btc_inventory_quant.domain.inverse_contract import ZERO


class LedgerEntryType(StrEnum):
    OPENING_BALANCE = "OPENING_BALANCE"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    REALIZED_PNL = "REALIZED_PNL"
    COMMISSION = "COMMISSION"
    FUNDING_FEE = "FUNDING_FEE"
    SLIPPAGE = (
        "SLIPPAGE"  # informational: modelled slippage cost vs reference price (backtest only)
    )
    INSURANCE_CLEAR = "INSURANCE_CLEAR"
    ADJUSTMENT = "ADJUSTMENT"  # exchange-side unexplained difference discovered by reconciliation


EXTERNAL_FLOW_TYPES = frozenset({LedgerEntryType.DEPOSIT, LedgerEntryType.WITHDRAWAL})
TRADING_TYPES = frozenset(
    {
        LedgerEntryType.REALIZED_PNL,
        LedgerEntryType.COMMISSION,
        LedgerEntryType.FUNDING_FEE,
        LedgerEntryType.INSURANCE_CLEAR,
        LedgerEntryType.ADJUSTMENT,
    }
)
WALLET_AFFECTING_TYPES = frozenset(
    {LedgerEntryType.OPENING_BALANCE, *EXTERNAL_FLOW_TYPES, *TRADING_TYPES}
)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    ts: datetime
    entry_type: LedgerEntryType
    amount_btc: (
        Decimal  # signed wallet change (SLIPPAGE entries are informational, negative = cost)
    )
    ref_id: str = ""
    note: str = ""
    price: Decimal | None = None
    contracts: Decimal | None = None
    asset: str = "BTC"

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("ledger timestamps must be timezone-aware (UTC)")


@dataclass(slots=True)
class LedgerTotals:
    opening_balance: Decimal = ZERO
    deposits: Decimal = ZERO
    withdrawals: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    commission: Decimal = ZERO
    funding: Decimal = ZERO
    slippage_cost: Decimal = ZERO
    insurance_clear: Decimal = ZERO
    adjustments: Decimal = ZERO

    @property
    def net_external_flows(self) -> Decimal:
        return self.deposits - self.withdrawals

    @property
    def trading_pnl(self) -> Decimal:
        """Realised trading result after fees and funding (external flows excluded)."""
        return (
            self.realized_pnl
            + self.commission
            + self.funding
            + self.insurance_clear
            + self.adjustments
        )

    @property
    def wallet_balance(self) -> Decimal:
        return self.opening_balance + self.net_external_flows + self.trading_pnl


@dataclass(slots=True)
class Ledger:
    entries: list[LedgerEntry] = field(default_factory=list)

    def append(self, entry: LedgerEntry) -> None:
        if self.entries and entry.ts < self.entries[-1].ts:
            raise ValueError("ledger entries must be appended in non-decreasing time order")
        self.entries.append(entry)

    def extend(self, entries: Iterable[LedgerEntry]) -> None:
        for e in entries:
            self.append(e)

    def totals(self) -> LedgerTotals:
        t = LedgerTotals()
        for e in self.entries:
            match e.entry_type:
                case LedgerEntryType.OPENING_BALANCE:
                    t.opening_balance += e.amount_btc
                case LedgerEntryType.DEPOSIT:
                    t.deposits += e.amount_btc
                case LedgerEntryType.WITHDRAWAL:
                    t.withdrawals += -e.amount_btc  # stored as negative wallet change
                case LedgerEntryType.REALIZED_PNL:
                    t.realized_pnl += e.amount_btc
                case LedgerEntryType.COMMISSION:
                    t.commission += e.amount_btc  # negative
                case LedgerEntryType.FUNDING_FEE:
                    t.funding += e.amount_btc
                case LedgerEntryType.SLIPPAGE:
                    t.slippage_cost += -e.amount_btc
                case LedgerEntryType.INSURANCE_CLEAR:
                    t.insurance_clear += e.amount_btc
                case LedgerEntryType.ADJUSTMENT:
                    t.adjustments += e.amount_btc
        return t

    def wallet_balance(self) -> Decimal:
        return sum(
            (e.amount_btc for e in self.entries if e.entry_type in WALLET_AFFECTING_TYPES), ZERO
        )

    def check_invariant(
        self, wallet_balance_btc: Decimal, tolerance: Decimal = Decimal("1e-12")
    ) -> None:
        expected = self.wallet_balance()
        if abs(expected - wallet_balance_btc) > tolerance:
            raise AssertionError(
                f"ledger invariant violated: ledger={expected} wallet={wallet_balance_btc}"
            )

    def net_btc_growth(self, ending_wallet_btc: Decimal) -> Decimal:
        """BTC gained from trading only: ending − opening − net external flows."""
        t = self.totals()
        return ending_wallet_btc - t.opening_balance - t.net_external_flows


def utc_now() -> datetime:
    return datetime.now(tz=UTC)
