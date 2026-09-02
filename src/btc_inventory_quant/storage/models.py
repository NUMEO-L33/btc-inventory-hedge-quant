"""SQLAlchemy ORM models. Decimals are stored as exact strings (no float storage, SQLite-safe)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class DecimalString(TypeDecorator[Decimal]):
    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Any) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Any) -> Decimal | None:
        return None if value is None else Decimal(value)


class JSONText(TypeDecorator[Any]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        return None if value is None else json.dumps(value, default=str, ensure_ascii=False)

    def process_result_value(self, value: str | None, dialect: Any) -> Any:
        return None if value is None else json.loads(value)


class UTCDateTime(TypeDecorator[datetime]):
    """Always store/return timezone-aware UTC datetimes (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


class AppState(Base):
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONText, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class OrderRecord(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    requested_qty: Mapped[Decimal] = mapped_column(DecimalString)
    filled_qty: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    avg_price: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(64), default="")
    target_ratio: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="shadow")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[Any] = mapped_column(JSONText, nullable=True)


class FillRecord(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(40), index=True)
    client_order_id: Mapped[str] = mapped_column(String(40), index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[Decimal] = mapped_column(DecimalString)
    qty: Mapped[Decimal] = mapped_column(DecimalString)
    base_qty_btc: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    commission: Mapped[Decimal] = mapped_column(DecimalString, default=Decimal("0"))
    maker: Mapped[bool] = mapped_column(Boolean, default=False)
    time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    __table_args__ = (Index("ix_fills_trade_symbol", "trade_id", "symbol", unique=True),)


class LedgerRecord(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    entry_type: Mapped[str] = mapped_column(String(24), index=True)
    amount_btc: Mapped[Decimal] = mapped_column(DecimalString)
    ref_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    price: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    contracts: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    source: Mapped[str] = mapped_column(String(16))  # internal | exchange
    contracts: Mapped[Decimal] = mapped_column(DecimalString)
    avg_entry_price: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    wallet_balance_btc: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    unrealized_pnl_btc: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    equity_btc: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    equity_usd: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    target_ratio: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    effective_ratio: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    raw: Mapped[Any] = mapped_column(JSONText, nullable=True)


class DecisionRecord(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    candle_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    regime: Mapped[str | None] = mapped_column(String(16), nullable=True)
    raw_target: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    decision_target: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    current_ratio: Mapped[Decimal | None] = mapped_column(DecimalString, nullable=True)
    action: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[Any] = mapped_column(JSONText, nullable=True)


class ReconciliationRecord(Base):
    __tablename__ = "reconciliations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    differences: Mapped[Any] = mapped_column(JSONText, nullable=True)
    action: Mapped[str] = mapped_column(String(32), default="")


class EventRecord(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    level: Mapped[str] = mapped_column(String(8))
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSONText, nullable=True)
