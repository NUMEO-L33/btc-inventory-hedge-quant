"""Database access (SQLite for development, PostgreSQL for operations) and repositories."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from btc_inventory_quant.domain.ledger import WALLET_AFFECTING_TYPES, LedgerEntry, LedgerEntryType
from btc_inventory_quant.storage.models import (
    AppState,
    Base,
    DecisionRecord,
    EventRecord,
    FillRecord,
    LedgerRecord,
    OrderRecord,
    PositionSnapshot,
    ReconciliationRecord,
    utcnow,
)


class Database:
    def __init__(self, url: str, echo: bool = False) -> None:
        if url.startswith("sqlite:///"):
            path = url.removeprefix("sqlite:///")
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.url = url
        self.engine: Engine = create_engine(url, echo=echo, future=True)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # --- app state -------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            row = s.get(AppState, key)
            return default if row is None else row.value

    def set_state(self, key: str, value: Any) -> None:
        with self.session() as s:
            row = s.get(AppState, key)
            if row is None:
                s.add(AppState(key=key, value=value))
            else:
                row.value = value
                row.updated_at = utcnow()

    # --- orders ----------------------------------------------------------
    def upsert_order(self, **fields: Any) -> OrderRecord:
        with self.session() as s:
            row = s.scalar(
                select(OrderRecord).where(OrderRecord.client_order_id == fields["client_order_id"])
            )
            if row is None:
                row = OrderRecord(**fields)
                s.add(row)
            else:
                for k, v in fields.items():
                    setattr(row, k, v)
                row.updated_at = utcnow()
            s.flush()
            return row

    def get_order(self, client_order_id: str) -> OrderRecord | None:
        with self.session() as s:
            return s.scalar(
                select(OrderRecord).where(OrderRecord.client_order_id == client_order_id)
            )

    def orders_with_status(
        self, statuses: list[str], symbol: str | None = None
    ) -> list[OrderRecord]:
        with self.session() as s:
            stmt = select(OrderRecord).where(OrderRecord.status.in_(statuses))
            if symbol:
                stmt = stmt.where(OrderRecord.symbol == symbol)
            return list(s.scalars(stmt.order_by(OrderRecord.id)))

    def all_orders(self, limit: int = 1000) -> list[OrderRecord]:
        with self.session() as s:
            return list(s.scalars(select(OrderRecord).order_by(OrderRecord.id.desc()).limit(limit)))

    # --- fills -----------------------------------------------------------
    def add_fill_if_new(self, **fields: Any) -> bool:
        with self.session() as s:
            exists = s.scalar(
                select(FillRecord).where(
                    FillRecord.trade_id == fields["trade_id"], FillRecord.symbol == fields["symbol"]
                )
            )
            if exists:
                return False
            s.add(FillRecord(**fields))
            return True

    def fills_for_order(self, client_order_id: str) -> list[FillRecord]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(FillRecord)
                    .where(FillRecord.client_order_id == client_order_id)
                    .order_by(FillRecord.id)
                )
            )

    # --- ledger ----------------------------------------------------------
    def add_ledger_entry(self, entry: LedgerEntry) -> None:
        with self.session() as s:
            s.add(
                LedgerRecord(
                    ts=entry.ts,
                    entry_type=entry.entry_type.value,
                    amount_btc=entry.amount_btc,
                    ref_id=entry.ref_id,
                    price=entry.price,
                    contracts=entry.contracts,
                    note=entry.note,
                )
            )

    def ledger_entries(self, since: datetime | None = None) -> list[LedgerRecord]:
        with self.session() as s:
            stmt = select(LedgerRecord).order_by(LedgerRecord.id)
            if since is not None:
                stmt = stmt.where(LedgerRecord.ts >= since)
            return list(s.scalars(stmt))

    def ledger_wallet_balance(self) -> tuple[Decimal, int] | None:
        """The wallet balance the ledger accounts for, and how many entries make it up.

        Summed from the most recent OPENING_BALANCE onwards: re-adopting a portfolio from the
        exchange (the runbook's recovery procedure) books a fresh opening balance, and summing
        across it would count the capital twice. ``None`` when there is no opening balance to
        start from - then the ledger cannot vouch for any wallet value.
        """
        with self.session() as s:
            start = s.scalar(
                select(LedgerRecord.id)
                .where(LedgerRecord.entry_type == LedgerEntryType.OPENING_BALANCE.value)
                .order_by(LedgerRecord.id.desc())
                .limit(1)
            )
            if start is None:
                return None
            kinds = sorted(t.value for t in WALLET_AFFECTING_TYPES)
            amounts = list(
                s.scalars(
                    select(LedgerRecord.amount_btc).where(
                        LedgerRecord.id >= start, LedgerRecord.entry_type.in_(kinds)
                    )
                )
            )
        return sum((Decimal(str(a)) for a in amounts), Decimal(0)), len(amounts)

    def ledger_has_ref(self, ref_id: str, entry_type: str) -> bool:
        with self.session() as s:
            return (
                s.scalar(
                    select(LedgerRecord).where(
                        LedgerRecord.ref_id == ref_id, LedgerRecord.entry_type == entry_type
                    )
                )
                is not None
            )

    # --- snapshots / decisions / events ----------------------------------
    def add_snapshot(self, **fields: Any) -> None:
        with self.session() as s:
            s.add(PositionSnapshot(**fields))

    def latest_snapshot(self, source: str = "internal") -> PositionSnapshot | None:
        with self.session() as s:
            return s.scalar(
                select(PositionSnapshot)
                .where(PositionSnapshot.source == source)
                .order_by(PositionSnapshot.id.desc())
                .limit(1)
            )

    def add_decision(self, **fields: Any) -> None:
        with self.session() as s:
            s.add(DecisionRecord(**fields))

    def latest_decision(self) -> DecisionRecord | None:
        with self.session() as s:
            return s.scalar(select(DecisionRecord).order_by(DecisionRecord.id.desc()).limit(1))

    def add_reconciliation(
        self, ok: bool, differences: list[dict[str, Any]], action: str = ""
    ) -> None:
        with self.session() as s:
            s.add(ReconciliationRecord(ts=utcnow(), ok=ok, differences=differences, action=action))

    def latest_reconciliation(self) -> ReconciliationRecord | None:
        with self.session() as s:
            return s.scalar(
                select(ReconciliationRecord).order_by(ReconciliationRecord.id.desc()).limit(1)
            )

    def add_event(
        self, level: str, event_type: str, message: str, payload: dict[str, Any] | None = None
    ) -> None:
        with self.session() as s:
            s.add(
                EventRecord(
                    ts=utcnow(),
                    level=level,
                    event_type=event_type,
                    message=message,
                    payload=payload,
                )
            )

    def recent_events(self, limit: int = 50) -> list[EventRecord]:
        with self.session() as s:
            return list(s.scalars(select(EventRecord).order_by(EventRecord.id.desc()).limit(limit)))


def decimal_or_none(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))
