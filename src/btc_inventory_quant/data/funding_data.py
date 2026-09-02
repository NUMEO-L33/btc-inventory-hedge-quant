"""Funding-rate history (``/dapi/v1/fundingRate``) and the per-day funding calendar.

Rows are objects ``{symbol, fundingTime, fundingRate, markPrice?, rateType?}`` (verified).
``markPrice`` is empty for rows before 2023-11-01 on Binance; the backtest funding model then
falls back to the daily mark-price kline (documented approximation).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

FUNDING_INTERVAL_MS = 8 * 3_600_000


class FundingDataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FundingEvent:
    funding_time: (
        datetime  # normalised to the exact hour (exchange timestamps carry ≤ ~100 ms jitter)
    )
    rate: Decimal
    mark_price: Decimal | None
    symbol: str = "BTCUSD_PERP"
    raw_funding_time_ms: int = 0

    @property
    def day(self) -> date:
        return self.funding_time.date()

    @property
    def hour(self) -> int:
        return self.funding_time.hour


@dataclass(frozen=True, slots=True)
class FundingQualityReport:
    rows: int
    first: datetime | None
    last: datetime | None
    irregular_intervals: tuple[str, ...]
    rows_with_mark_price: int
    min_rate: Decimal | None
    max_rate: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "first": self.first.isoformat() if self.first else None,
            "last": self.last.isoformat() if self.last else None,
            "irregular_intervals": list(self.irregular_intervals),
            "rows_with_mark_price": self.rows_with_mark_price,
            "min_rate": str(self.min_rate) if self.min_rate is not None else None,
            "max_rate": str(self.max_rate) if self.max_rate is not None else None,
        }


def normalise_funding_time(raw_ms: int, max_jitter_ms: int = 60_000) -> datetime:
    """Round to the nearest hour; refuse if the timestamp is further than ``max_jitter_ms`` away."""
    hour_ms = 3_600_000
    rounded = round(raw_ms / hour_ms) * hour_ms
    if abs(rounded - raw_ms) > max_jitter_ms:
        raise FundingDataError(f"funding timestamp {raw_ms} is not near an hour boundary")
    return datetime.fromtimestamp(rounded / 1000, tz=UTC)


def parse_funding_rows(rows: list[dict[str, Any]]) -> list[FundingEvent]:
    events: list[FundingEvent] = []
    for r in rows:
        mp_raw = r.get("markPrice")
        mark = Decimal(str(mp_raw)) if mp_raw not in (None, "", "0") else None
        raw_ms = int(r["fundingTime"])
        events.append(
            FundingEvent(
                funding_time=normalise_funding_time(raw_ms),
                rate=Decimal(str(r["fundingRate"])),
                mark_price=mark,
                symbol=str(r.get("symbol", "BTCUSD_PERP")),
                raw_funding_time_ms=raw_ms,
            )
        )
    events.sort(key=lambda e: e.funding_time)
    dedup: list[FundingEvent] = []
    seen: set[int] = set()
    for e in events:
        key = int(e.funding_time.timestamp())
        if key in seen:
            continue
        seen.add(key)
        dedup.append(e)
    return dedup


def load_funding_events(directory: Path) -> tuple[list[FundingEvent], FundingQualityReport]:
    pages = sorted(directory.glob("page_*.json"))
    if not pages:
        raise FundingDataError(f"no page_*.json files in {directory}")
    rows: list[dict[str, Any]] = []
    for p in pages:
        payload = json.loads(p.read_text(encoding="utf-8"))
        rows.extend(payload)
    events = parse_funding_rows(rows)
    irregular: list[str] = []
    for a, b in pairwise(events):
        delta_ms = int((b.funding_time - a.funding_time).total_seconds() * 1000)
        if delta_ms != FUNDING_INTERVAL_MS:
            irregular.append(
                f"{a.funding_time.isoformat()} -> {b.funding_time.isoformat()} ({delta_ms / 3_600_000:.2f} h)"
            )
    rates = [e.rate for e in events]
    report = FundingQualityReport(
        rows=len(events),
        first=events[0].funding_time if events else None,
        last=events[-1].funding_time if events else None,
        irregular_intervals=tuple(irregular),
        rows_with_mark_price=sum(1 for e in events if e.mark_price is not None),
        min_rate=min(rates) if rates else None,
        max_rate=max(rates) if rates else None,
    )
    return events, report


class FundingCalendar:
    """Funding events grouped by UTC day; the backtest asks ``events_on(day)``."""

    def __init__(self, events: list[FundingEvent]) -> None:
        self._by_day: dict[date, list[FundingEvent]] = defaultdict(list)
        for e in events:
            self._by_day[e.day].append(e)
        self.events = events

    def events_on(self, day: date) -> list[FundingEvent]:
        return sorted(self._by_day.get(day, []), key=lambda e: e.funding_time)

    def __len__(self) -> int:
        return len(self.events)
