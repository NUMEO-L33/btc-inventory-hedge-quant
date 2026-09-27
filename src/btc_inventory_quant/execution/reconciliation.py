"""Reconciliation between the exchange (source of truth for position/balance) and the internal DB.

* ``compare`` → list of differences (empty = consistent). Any difference is a SAFE_HOLD trigger.
* ``sync_income`` → applies exchange income records (FUNDING_FEE, COMMISSION, REALIZED_PNL,
  TRANSFER, INSURANCE_CLEAR…) to the internal ledger exactly once (dedupe by tranId), keeping
  external transfers separate from trading PnL and flagging *unexpected* transfers.
* ``adopt`` → rebuild the internal position/wallet from the exchange (restart recovery).
* ``verify_funding_formula`` → compare our funding formula with actual FUNDING_FEE amounts
  (the "÷ Mark Price" form is PARTIAL in the official docs — see docs/inverse_math.md §6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from btc_inventory_quant.config.schema import RiskLimits
from btc_inventory_quant.domain.inverse_contract import (
    ZERO,
    ContractSpec,
    OrderSide,
    funding_payment_btc,
)
from btc_inventory_quant.domain.ledger import LedgerEntry, LedgerEntryType
from btc_inventory_quant.domain.portfolio import Portfolio
from btc_inventory_quant.exchange.base import ExchangeAdapter
from btc_inventory_quant.exchange.models import (
    AccountState,
    ExchangeOrder,
    ExchangePosition,
    IncomeRecord,
    PositionMode,
)
from btc_inventory_quant.monitoring.logging import get_logger
from btc_inventory_quant.storage.database import Database

log = get_logger(__name__)

INCOME_TO_LEDGER = {
    "FUNDING_FEE": LedgerEntryType.FUNDING_FEE,
    "COMMISSION": LedgerEntryType.COMMISSION,
    "REALIZED_PNL": LedgerEntryType.REALIZED_PNL,
    "INSURANCE_CLEAR": LedgerEntryType.INSURANCE_CLEAR,
    "TRANSFER": LedgerEntryType.DEPOSIT,  # sign decides deposit/withdrawal
    "WELCOME_BONUS": LedgerEntryType.ADJUSTMENT,
    "DELIVERED_SETTELMENT": LedgerEntryType.ADJUSTMENT,
}
# Income is fetched one type at a time. The documented behaviour of GET /dapi/v1/income is "if
# incomeType is not sent, all kinds of flow will be returned", but the demo host returns an EMPTY
# list for exactly that call (measured 2026-09-27: 0 records unfiltered, 22 with
# incomeType=FUNDING_FEE, same account, same window). Seven days of demo funding never reached
# the ledger, and the wallet tolerance hid it. Asking per type is what the docs define, works on
# both hosts, and costs a few extra weight-20 calls once a day. COMMISSION and REALIZED_PNL are
# not fetched: they enter the ledger through fills, and sync_income only skips them anyway.
INCOME_TYPES_SYNCED = (
    "FUNDING_FEE",
    "TRANSFER",
    "INSURANCE_CLEAR",
    "WELCOME_BONUS",
    "DELIVERED_SETTELMENT",
)


@dataclass(frozen=True, slots=True)
class Difference:
    code: str
    message: str
    internal: str = ""
    exchange: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "internal": self.internal,
            "exchange": self.exchange,
        }


@dataclass(slots=True)
class ExchangeSnapshot:
    account: AccountState
    position: ExchangePosition | None
    open_orders: list[ExchangeOrder]
    mode: PositionMode
    taken_at: datetime


@dataclass(slots=True)
class ReconciliationReport:
    ok: bool
    differences: list[Difference] = field(default_factory=list)
    snapshot: ExchangeSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "differences": [d.to_dict() for d in self.differences]}


class Reconciler:
    def __init__(
        self,
        exchange: ExchangeAdapter,
        db: Database,
        spec: ContractSpec,
        symbol: str,
        limits: RiskLimits,
        *,
        price_tolerance: Decimal = Decimal("0.001"),
        balance_tolerance_btc: Decimal | None = None,
    ) -> None:
        self.exchange = exchange
        self.db = db
        self.spec = spec
        self.symbol = symbol
        self.limits = limits
        self.price_tolerance = price_tolerance
        self.balance_tolerance = (
            balance_tolerance_btc
            if balance_tolerance_btc is not None
            else limits.max_unexpected_balance_diff_btc
        )

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> ExchangeSnapshot:
        account = self.exchange.account()
        positions = [
            p
            for p in self.exchange.positions(pair=self.spec.pair or None)
            if p.symbol == self.symbol
        ]
        position = positions[0] if positions else None
        return ExchangeSnapshot(
            account=account,
            position=position,
            open_orders=self.exchange.open_orders(self.symbol),
            mode=self.exchange.position_mode(),
            taken_at=datetime.now(tz=UTC),
        )

    # --------------------------------------------------------------- compare
    def compare(
        self,
        portfolio: Portfolio,
        snap: ExchangeSnapshot,
        known_client_ids: set[str],
        mark_price: Decimal,
    ) -> ReconciliationReport:
        diffs: list[Difference] = []
        ex_contracts = snap.position.contracts if snap.position else ZERO
        if snap.mode is not PositionMode.ONE_WAY:
            diffs.append(
                Difference(
                    "POSITION_MODE",
                    "exchange position mode is not ONE_WAY",
                    "ONE_WAY",
                    snap.mode.value,
                )
            )
        if ex_contracts > ZERO:
            diffs.append(
                Difference(
                    "LONG_POSITION",
                    "exchange reports a LONG position",
                    str(portfolio.position.contracts),
                    str(ex_contracts),
                )
            )
        if ex_contracts != portfolio.position.contracts:
            diffs.append(
                Difference(
                    "POSITION_CONTRACTS",
                    "position size differs",
                    str(portfolio.position.contracts),
                    str(ex_contracts),
                )
            )
        if (
            snap.position
            and snap.position.entry_price
            and portfolio.position.avg_entry_price
            and ex_contracts != ZERO
        ):
            rel = (
                abs(snap.position.entry_price - portfolio.position.avg_entry_price)
                / snap.position.entry_price
            )
            if rel > self.price_tolerance:
                diffs.append(
                    Difference(
                        "ENTRY_PRICE",
                        f"entry price differs by {rel:.4%}",
                        str(portfolio.position.avg_entry_price),
                        str(snap.position.entry_price),
                    )
                )
        wallet_diff = snap.account.wallet_balance - portfolio.wallet_balance_btc
        if abs(wallet_diff) > self.balance_tolerance:
            diffs.append(
                Difference(
                    "WALLET_BALANCE",
                    f"wallet balance differs by {wallet_diff} BTC",
                    str(portfolio.wallet_balance_btc),
                    str(snap.account.wallet_balance),
                )
            )
        for o in snap.open_orders:
            if o.client_order_id not in known_client_ids:
                diffs.append(
                    Difference(
                        "UNKNOWN_OPEN_ORDER",
                        "open order not created by this system",
                        "",
                        o.client_order_id,
                    )
                )
        equity = snap.account.wallet_balance + snap.account.unrealized_pnl
        if equity > ZERO and ex_contracts < ZERO:
            ratio = -ex_contracts * self.spec.contract_value_usd / (equity * mark_price)
            if ratio > self.limits.max_hedge_ratio + self.limits.max_hedge_ratio_tolerance:
                diffs.append(
                    Difference(
                        "HEDGE_CAP",
                        f"effective hedge ratio {ratio:.4f} above cap",
                        str(self.limits.max_hedge_ratio),
                        f"{ratio:.4f}",
                    )
                )
        report = ReconciliationReport(ok=not diffs, differences=diffs, snapshot=snap)
        self.db.add_reconciliation(
            report.ok, [d.to_dict() for d in diffs], action="" if report.ok else "SAFE_HOLD"
        )
        return report

    # ---------------------------------------------------------------- income
    def sync_income(
        self, portfolio: Portfolio, since_ms: int | None, *, expect_transfers: bool = False
    ) -> dict[str, Any]:
        records: list[IncomeRecord] = []
        for income_type in INCOME_TYPES_SYNCED:
            records.extend(
                self.exchange.income(symbol=None, income_type=income_type, start_ms=since_ms)
            )
        applied = 0
        unexpected_transfers: list[IncomeRecord] = []
        for r in sorted(records, key=lambda x: (x.time.timestamp() if x.time else 0, x.tran_id)):
            ref = f"income:{r.income_type}:{r.tran_id}"
            if r.amount == ZERO or self.db.ledger_has_ref(ref, _ledger_type_for(r).value):
                continue
            ts = r.time or datetime.now(tz=portfolio.ledger.entries[-1].ts.tzinfo)
            if portfolio.ledger.entries and ts < portfolio.ledger.entries[-1].ts:
                ts = portfolio.ledger.entries[-1].ts
            entry_type = _ledger_type_for(r)
            if r.income_type == "TRANSFER":
                entry_type = (
                    LedgerEntryType.DEPOSIT if r.amount > ZERO else LedgerEntryType.WITHDRAWAL
                )
                if not expect_transfers:
                    unexpected_transfers.append(r)
            if entry_type in (LedgerEntryType.COMMISSION, LedgerEntryType.REALIZED_PNL):
                # already applied through fills; keep as informational check only
                continue
            entry = LedgerEntry(
                ts, entry_type, r.amount, ref_id=ref, note=f"{r.income_type} {r.info}".strip()
            )
            portfolio.wallet_balance_btc += r.amount
            portfolio.ledger.append(entry)
            self.db.add_ledger_entry(entry)
            applied += 1
        return {
            "applied": applied,
            "unexpected_transfers": [t.tran_id for t in unexpected_transfers],
            "unexpected_amount_btc": str(sum((t.amount for t in unexpected_transfers), ZERO)),
        }

    # ----------------------------------------------------------------- adopt
    def adopt(
        self,
        portfolio: Portfolio,
        snap: ExchangeSnapshot,
        ts: datetime,
        *,
        as_opening: bool = False,
    ) -> None:
        """Rebuild internal state from the exchange (used on first start and after SAFE_HOLD review).

        ``as_opening=True`` books the wallet difference as OPENING_BALANCE (first start: the
        exchange balance is the starting capital, not a trading result); otherwise as ADJUSTMENT.
        """
        contracts = snap.position.contracts if snap.position else ZERO
        entry = snap.position.entry_price if snap.position else None
        portfolio.position.contracts = ZERO
        portfolio.position.avg_entry_price = None
        portfolio.position.lots.clear()
        if contracts < ZERO and entry:
            portfolio.position.apply_fill(OrderSide.SELL, -contracts, entry, ZERO)
        elif contracts > ZERO:
            raise RuntimeError("cannot adopt a LONG position: this system never holds longs")
        diff = snap.account.wallet_balance - portfolio.wallet_balance_btc
        if diff != ZERO:
            portfolio.wallet_balance_btc = snap.account.wallet_balance
            kind = LedgerEntryType.OPENING_BALANCE if as_opening else LedgerEntryType.ADJUSTMENT
            entry_rec = LedgerEntry(
                ts,
                kind,
                diff,
                ref_id=f"adopt:{ts.isoformat()}",
                note="adopted exchange wallet balance",
            )
            portfolio.ledger.append(entry_rec)
            self.db.add_ledger_entry(entry_rec)
        self.db.add_snapshot(
            ts=ts,
            source="exchange",
            contracts=contracts,
            avg_entry_price=entry,
            mark_price=snap.position.mark_price if snap.position else None,
            wallet_balance_btc=snap.account.wallet_balance,
            unrealized_pnl_btc=snap.account.unrealized_pnl,
            equity_btc=snap.account.wallet_balance + snap.account.unrealized_pnl,
            raw={"adopted": True},
        )

    # ------------------------------------------------------- funding check
    def verify_funding_formula(
        self,
        records: list[IncomeRecord],
        position_contracts: Decimal,
        mark_prices: dict[str, Decimal],
        rates: dict[str, Decimal],
    ) -> dict[str, Any]:
        """Compare computed funding (contracts × CV / mark × rate) with actual FUNDING_FEE income.

        ``mark_prices``/``rates`` are keyed by the funding time ISO string. Returns ratio stats;
        a mean ratio far from 1 means the formula (or the mark source) is wrong.
        """
        ratios: list[Decimal] = []
        for r in records:
            if r.income_type != "FUNDING_FEE" or r.time is None:
                continue
            key = r.time.replace(minute=0, second=0, microsecond=0).isoformat()
            if key not in mark_prices or key not in rates:
                continue
            computed = funding_payment_btc(
                position_contracts, self.spec.contract_value_usd, mark_prices[key], rates[key]
            )
            if computed != ZERO:
                ratios.append(r.amount / computed)
        if not ratios:
            return {"samples": 0}
        mean = sum(ratios, ZERO) / len(ratios)
        return {
            "samples": len(ratios),
            "mean_ratio": str(mean),
            "min_ratio": str(min(ratios)),
            "max_ratio": str(max(ratios)),
            "ok": abs(mean - 1) < Decimal("0.05"),
        }


def _ledger_type_for(r: IncomeRecord) -> LedgerEntryType:
    return INCOME_TO_LEDGER.get(r.income_type, LedgerEntryType.ADJUSTMENT)
