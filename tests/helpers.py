"""Synthetic, deterministic market data for hermetic tests (no network, no files)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from btc_inventory_quant.data.funding_data import FundingCalendar, FundingEvent
from btc_inventory_quant.data.market_data import DailyDataset


def synthetic_prices(
    n: int, seed: int = 7, start_price: float = 30_000.0, drift: float = 0.0004, vol: float = 0.03
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, size=n)
    return start_price * np.exp(np.cumsum(rets))


def make_dataset(
    closes: np.ndarray | list[float], start: datetime | None = None, intraday_range: float = 0.02
) -> DailyDataset:
    start = start or datetime(2021, 1, 1, tzinfo=UTC)
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    opens = np.concatenate([[closes[0]], closes[:-1]])  # open = previous close (gap-free)
    highs = np.maximum(opens, closes) * (1 + intraday_range)
    lows = np.minimum(opens, closes) * (1 - intraday_range)
    f = pd.DataFrame(index=idx)
    f.index.name = "open_time"
    f["open"], f["high"], f["low"], f["close"] = opens, highs, lows, closes
    f["volume"] = 1000.0
    f["close_time"] = idx + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    f["open_dec"] = [f"{x:.1f}" for x in opens]
    f["close_dec"] = [f"{x:.1f}" for x in closes]
    # mark = trade price (synthetic); index slightly different
    f["mark_open"], f["mark_high"], f["mark_low"], f["mark_close"] = opens, highs, lows, closes
    f["mark_open_dec"] = f["open_dec"]
    f["mark_close_dec"] = f["close_dec"]
    f["index_open"], f["index_close"] = opens * 1.0001, closes * 1.0001
    f["index_open_dec"] = [f"{x:.1f}" for x in opens * 1.0001]
    f["index_close_dec"] = [f"{x:.1f}" for x in closes * 1.0001]
    f["mark_index_divergence"] = 0.0001
    f.attrs["notes"] = []
    return DailyDataset(
        frame=f, quality={}, symbol="BTCUSD_PERP", as_of=idx[-1].to_pydatetime() + timedelta(days=2)
    )


def make_funding(dataset: DailyDataset, rate: Decimal = Decimal("0.0001")) -> FundingCalendar:
    events: list[FundingEvent] = []
    for ts in dataset.frame.index:
        base = ts.to_pydatetime()
        for h in (0, 8, 16):
            t = base + timedelta(hours=h)
            events.append(
                FundingEvent(
                    funding_time=t,
                    rate=rate,
                    mark_price=None,
                    raw_funding_time_ms=int(t.timestamp() * 1000),
                )
            )
    return FundingCalendar(events)


def trending_prices(n: int, start_price: float = 30_000.0, daily: float = 0.01) -> np.ndarray:
    return np.array([start_price * math.exp(daily * i) for i in range(n)])
