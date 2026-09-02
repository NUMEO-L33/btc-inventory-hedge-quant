"""Exact mark price at each funding settlement instant, from the 8-hour mark-price klines.

Binance only populates ``markPrice`` on ``/dapi/v1/fundingRate`` rows from **2023-11-01**; before
that the backtest had to approximate the settlement mark by interpolating the *daily* mark kline
(open for 00:00, open→close linear for 08:00/16:00). That approximation covers 53.2% of the
settlement history (3,533 of 6,640 events on the 2026-09-01 snapshot).

``GET /dapi/v1/markPriceKlines?interval=8h`` gives a bar whose **open time is exactly a settlement
instant** (00:00 / 08:00 / 16:00 UTC) and whose **open price is the mark price at that instant**,
so its open replaces the interpolation with a real observation for the whole history.

Download it with ``python -m btc_inventory_quant data --raw-dir <dir> --include-8h-mark download``.
The dataset is optional: when the directory is absent the funding model falls back to the previous
behaviour, and nothing else changes.

Measured impact (see ``docs/backtest_methodology.md`` §7): on the 2023-11→2026-09 era, where the
true mark *is* known, the daily interpolation is off by 0.52% on average (p95 1.89%, max 7.77%)
per settlement, but the errors are symmetric and the **cumulative funding error is +0.0096%**.
This dataset therefore buys per-settlement precision, not a change in any conclusion.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

SETTLEMENT_HOURS = (0, 8, 16)
DIRECTORY_NAME = "mark_price_klines_8h"


class SettlementMarkError(ValueError):
    pass


def load_settlement_marks(directory: Path) -> dict[datetime, Decimal]:
    """``{settlement instant → mark price}`` from 8h mark klines (open time → open price).

    Bars whose open time is not a settlement hour are ignored rather than rejected, so a snapshot
    taken with a different interval cannot silently corrupt the funding model.
    """
    pages = sorted(directory.glob("page_*.json"))
    if not pages:
        raise SettlementMarkError(f"no page_*.json files in {directory}")
    marks: dict[datetime, Decimal] = {}
    for p in pages:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise SettlementMarkError(f"{p} is not a JSON array")
        for row in payload:
            ts = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
            if ts.hour not in SETTLEMENT_HOURS or ts.minute or ts.second:
                continue
            price = Decimal(str(row[1]))
            if price <= 0:
                raise SettlementMarkError(f"{p}: non-positive mark price at {ts.isoformat()}")
            marks.setdefault(ts, price)
    if not marks:
        raise SettlementMarkError(f"{directory}: no bars opening on a settlement hour")
    return marks


def load_settlement_marks_if_present(raw_dir: Path) -> dict[datetime, Decimal]:
    """Same, but returns ``{}`` when the optional dataset was not downloaded."""
    directory = raw_dir / DIRECTORY_NAME
    if not directory.is_dir():
        return {}
    return load_settlement_marks(directory)


__all__ = [
    "DIRECTORY_NAME",
    "SETTLEMENT_HOURS",
    "SettlementMarkError",
    "load_settlement_marks",
    "load_settlement_marks_if_present",
]
