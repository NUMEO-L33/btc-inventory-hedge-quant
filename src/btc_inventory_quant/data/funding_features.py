"""Causal daily funding features built from the real settlement history.

The feature at bar *t* uses only settlements that had already occurred when bar *t* closed
(i.e. ``funding_time <= bar_close``), so a strategy reading it at bar *t* and executing at the
open of bar *t+1* has no look-ahead. This is asserted in
``tests/unit/test_funding_features.py::test_features_are_causal``.

Columns produced (``n`` = ``lookback_periods``, default 21 settlements ≈ 7 days):

* ``funding_rate_last``      — most recent settled rate at/ before the bar close
* ``funding_mean_<n>``       — mean settled rate over the last n settlements
* ``funding_sum_<n>``        — sum of the last n settled rates (the carry actually banked)
* ``funding_annual_<n>``     — the same mean expressed per year (× 3 × 365)
* ``funding_periods_<n>``    — how many settlements the window actually contains (< n during warm-up)

A short position *receives* when the rate is positive, so a negative mean means the short is
paying to stay on — that is what the funding guard reacts to.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd

from btc_inventory_quant.data.funding_data import FundingEvent, normalise_funding_time

PERIODS_PER_YEAR = 3 * 365  # 8-hour settlements
DEFAULT_LOOKBACK = 21


def _events_to_series(events: Iterable[FundingEvent]) -> pd.Series:
    times: list[pd.Timestamp] = []
    rates: list[float] = []
    for e in events:
        times.append(pd.Timestamp(e.funding_time))
        rates.append(float(e.rate))
    if not times:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    s = pd.Series(rates, index=pd.DatetimeIndex(times))
    return s[~s.index.duplicated(keep="first")].sort_index()


def rows_to_events(rows: Iterable[dict[str, object]]) -> list[FundingEvent]:
    """Adapter rows (``fundingTime``/``fundingRate``) → :class:`FundingEvent` list (live path)."""
    from decimal import Decimal

    out: list[FundingEvent] = []
    for r in rows:
        raw_ms = int(str(r["fundingTime"]))
        mark = r.get("markPrice")
        out.append(
            FundingEvent(
                funding_time=normalise_funding_time(raw_ms),
                rate=Decimal(str(r["fundingRate"])),
                mark_price=None if mark in (None, "", "0") else Decimal(str(mark)),
                symbol=str(r.get("symbol", "BTCUSD_PERP")),
                raw_funding_time_ms=raw_ms,
            )
        )
    out.sort(key=lambda e: e.funding_time)
    return out


def build_funding_features(
    events: Iterable[FundingEvent],
    index: pd.DatetimeIndex,
    lookback_periods: int | Sequence[int] = DEFAULT_LOOKBACK,
    bar_close: pd.Series | None = None,
) -> pd.DataFrame:
    """Daily funding features aligned to ``index`` (bar open times).

    ``bar_close`` (optional) gives each bar's close time; by default the bar is assumed to close
    just before the next bar opens (daily bars), i.e. settlements up to ``open + 1 day − 1 ms``.
    """
    lookbacks = (
        (lookback_periods,) if isinstance(lookback_periods, int) else tuple(lookback_periods)
    )
    if any(n < 1 for n in lookbacks):
        raise ValueError("lookback_periods must be >= 1")
    series = _events_to_series(events)
    closes = (
        pd.DatetimeIndex(bar_close)
        if bar_close is not None
        else index + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    )
    out = pd.DataFrame(index=index)
    if series.empty:
        out["funding_rate_last"] = np.nan
        for n in lookbacks:
            out[f"funding_mean_{n}"] = np.nan
            out[f"funding_sum_{n}"] = np.nan
            out[f"funding_annual_{n}"] = np.nan
            out[f"funding_periods_{n}"] = 0
        return out

    values = series.to_numpy(dtype=float)
    cumsum = np.concatenate([[0.0], np.cumsum(values)])
    # number of settlements strictly at or before each bar close (searchsorted 'right')
    counts = series.index.searchsorted(closes, side="right").astype(int)

    out["funding_rate_last"] = np.where(counts > 0, values[np.clip(counts - 1, 0, None)], np.nan)
    for n in lookbacks:
        start = np.clip(counts - n, 0, None)
        periods = counts - start
        total = cumsum[counts] - cumsum[start]
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(periods > 0, total / np.maximum(periods, 1), np.nan)
        out[f"funding_mean_{n}"] = mean
        out[f"funding_sum_{n}"] = np.where(periods > 0, total, np.nan)
        out[f"funding_annual_{n}"] = mean * PERIODS_PER_YEAR
        out[f"funding_periods_{n}"] = periods
    return out


def attach_funding_features(
    frame: pd.DataFrame,
    events: Iterable[FundingEvent],
    lookback_periods: int | Sequence[int] = DEFAULT_LOOKBACK,
) -> pd.DataFrame:
    """Return a copy of ``frame`` with the funding feature columns joined (never mutates input)."""
    index = pd.DatetimeIndex(frame.index)
    close = frame["close_time"] if "close_time" in frame.columns else None
    features = build_funding_features(events, index, lookback_periods, bar_close=close)
    merged = frame.copy()
    for col in features.columns:
        merged[col] = features[col].to_numpy()
    merged.attrs = dict(frame.attrs)
    return merged
