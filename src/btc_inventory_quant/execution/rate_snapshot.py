"""Capture the funding rate an environment will actually apply, BEFORE it settles.

Why this exists (measured 2026-09-03): the demo host settles funding but publishes no
``fundingRate`` history, so a retrospective check has two unknowns (rate and mark) and one
equation. Substituting production's rate produced a 2.93x MISMATCH — not because the formula is
wrong (backing the rate out of the real settlement gives 0.00927%, a perfectly ordinary funding
rate, whereas a wrong denominator would imply 1.2e-9 or 1.6e-14) but because **the demo applies
its own rate**, roughly 2.9x production's on that settlement.

The ``environments_agree`` check in :mod:`funding_verification` was too weak to catch this: it
compares MARK PRICE, and two venues can quote the same mark while running different funding rates.
Mark agreement is not rate agreement.

So capture the rate prospectively instead. ``GET /dapi/v1/premiumIndex`` exposes the rate for the
UPCOMING settlement along with ``nextFundingTime``; sampled shortly before that time it is the
rate about to be applied. Snapshots are appended to a JSON file and consumed by
``verify-funding``, which then has a rate from the same environment that produced the settlement.

The rate can still move until the boundary, so every snapshot records ``captured_at`` and the
seconds remaining — the report shows both and the operator judges whether the sample was close
enough.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from btc_inventory_quant.exchange.base import ExchangeAdapter
from btc_inventory_quant.monitoring.logging import get_logger

log = get_logger(__name__)

DEFAULT_PATH = Path("state") / "funding_rate_snapshots.json"


@dataclass(frozen=True, slots=True)
class RateSnapshot:
    symbol: str
    environment: str
    funding_time: datetime
    rate: Decimal
    mark_price: Decimal
    captured_at: datetime

    @property
    def seconds_before_settlement(self) -> int:
        return int((self.funding_time - self.captured_at).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "environment": self.environment,
            "funding_time": self.funding_time.isoformat(),
            "rate": str(self.rate),
            "mark_price": str(self.mark_price),
            "captured_at": self.captured_at.isoformat(),
            "seconds_before_settlement": self.seconds_before_settlement,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RateSnapshot:
        return cls(
            symbol=str(d["symbol"]),
            environment=str(d["environment"]),
            funding_time=datetime.fromisoformat(str(d["funding_time"])),
            rate=Decimal(str(d["rate"])),
            mark_price=Decimal(str(d["mark_price"])),
            captured_at=datetime.fromisoformat(str(d["captured_at"])),
        )


class SnapshotStoreError(ValueError):
    pass


def capture(exchange: ExchangeAdapter, symbol: str, now: datetime | None = None) -> RateSnapshot:
    """Read the upcoming funding rate from the configured environment."""
    info = exchange.premium_index(symbol)
    if info.next_funding_time is None:
        raise SnapshotStoreError(
            f"{exchange.environment} premiumIndex did not report nextFundingTime for {symbol}; "
            "cannot tell which settlement this rate belongs to"
        )
    if info.last_funding_rate is None:
        raise SnapshotStoreError(
            f"{exchange.environment} premiumIndex reported no funding rate for {symbol}; "
            "there is nothing to capture, and guessing one would defeat the verification"
        )
    return RateSnapshot(
        symbol=symbol,
        environment=exchange.environment,
        funding_time=info.next_funding_time,
        rate=info.last_funding_rate,
        mark_price=info.mark_price,
        captured_at=now or datetime.now(tz=UTC),
    )


def load(path: Path = DEFAULT_PATH) -> list[RateSnapshot]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SnapshotStoreError(f"{path} is not a JSON array")
    return [RateSnapshot.from_dict(d) for d in payload]


def append(snapshot: RateSnapshot, path: Path = DEFAULT_PATH) -> list[RateSnapshot]:
    """Append, keeping only the LATEST capture per (symbol, environment, funding_time).

    A later sample sits closer to the settlement boundary, so it is the better estimate of the
    rate that will actually be applied.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {
        (s.symbol, s.environment, s.funding_time): s
        for s in load(path)
        if not (
            s.symbol == snapshot.symbol
            and s.environment == snapshot.environment
            and s.funding_time == snapshot.funding_time
            and s.captured_at <= snapshot.captured_at
        )
    }
    kept[(snapshot.symbol, snapshot.environment, snapshot.funding_time)] = kept.get(
        (snapshot.symbol, snapshot.environment, snapshot.funding_time), snapshot
    )
    out = sorted(kept.values(), key=lambda s: (s.funding_time, s.captured_at))
    path.write_text(
        json.dumps([s.to_dict() for s in out], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return out


def rates_for(
    snapshots: list[RateSnapshot], symbol: str, environment: str
) -> dict[datetime, tuple[Decimal, Decimal | None]]:
    """Snapshots → the ``{funding_time: (rate, mark)}`` shape the verifier consumes."""
    return {
        s.funding_time: (s.rate, s.mark_price)
        for s in sorted(snapshots, key=lambda s: s.captured_at)
        if s.symbol == symbol and s.environment == environment
    }


__all__ = [
    "DEFAULT_PATH",
    "RateSnapshot",
    "SnapshotStoreError",
    "append",
    "capture",
    "load",
    "rates_for",
]
