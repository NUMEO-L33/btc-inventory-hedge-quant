"""Daily market data (trade klines, mark-price klines, index-price klines) for COIN-M BTCUSD_PERP.

Raw inputs are the verbatim JSON pages returned by the public REST endpoints
(``/dapi/v1/klines``, ``/dapi/v1/markPriceKlines``, ``/dapi/v1/indexPriceKlines``) stored under
``data/raw/<dataset>/page_NNN.json``. Kline arrays follow the official layout (verified):

    [open_time, open, high, low, close, volume, close_time, base_asset_volume,
     number_of_trades, taker_buy_volume, taker_buy_base_asset_volume, ignore]

Only *completed* candles are used: a candle is complete when ``close_time < as_of``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

DAY_MS = 86_400_000


class MarketDataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    dataset: str
    rows: int
    first_open_time: datetime | None
    last_open_time: datetime | None
    gaps: tuple[str, ...]
    duplicates: int
    ohlc_violations: int
    dropped_incomplete: int
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "rows": self.rows,
            "first_open_time": self.first_open_time.isoformat() if self.first_open_time else None,
            "last_open_time": self.last_open_time.isoformat() if self.last_open_time else None,
            "gaps": list(self.gaps),
            "duplicates": self.duplicates,
            "ohlc_violations": self.ohlc_violations,
            "dropped_incomplete": self.dropped_incomplete,
            "notes": list(self.notes),
        }


def _read_pages(directory: Path) -> list[list[Any]]:
    pages = sorted(directory.glob("page_*.json"))
    if not pages:
        raise MarketDataError(f"no page_*.json files in {directory}")
    rows: list[list[Any]] = []
    for p in pages:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise MarketDataError(f"{p} is not a JSON array")
        rows.extend(payload)
    return rows


def klines_frame(rows: list[list[Any]], prefix: str = "") -> pd.DataFrame:
    """Kline rows → DataFrame indexed by UTC open_time. Prices kept as strings→float64 for indicators;
    exact Decimal copies are added for execution-critical columns (open/close)."""
    if not rows:
        raise MarketDataError("empty kline rows")
    records = []
    for r in rows:
        if len(r) < 7:
            raise MarketDataError(f"malformed kline row: {r}")
        records.append(
            {
                "open_time": int(r[0]),
                f"{prefix}open": float(r[1]),
                f"{prefix}high": float(r[2]),
                f"{prefix}low": float(r[3]),
                f"{prefix}close": float(r[4]),
                f"{prefix}volume": float(r[5]),
                "close_time": int(r[6]),
                f"{prefix}open_dec": str(r[1]),
                f"{prefix}close_dec": str(r[4]),
            }
        )
    df = pd.DataFrame.from_records(records)
    df = df.drop_duplicates(subset="open_time", keep="first").sort_values("open_time")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df.set_index("open_time")


def _quality(
    df: pd.DataFrame, dataset: str, prefix: str, dropped: int, raw_len: int
) -> DataQualityReport:
    idx = df.index
    gaps: list[str] = []
    if len(idx) > 1:
        ms = np.asarray(idx.values).astype("datetime64[ms]").astype(np.int64)
        for i in range(len(ms) - 1):
            d = int(ms[i + 1] - ms[i])
            if d != DAY_MS:
                gaps.append(
                    f"{idx[i].isoformat()} -> {idx[i + 1].isoformat()} ({d / DAY_MS:.2f} days)"
                )
    o, h, low, c = (df[f"{prefix}{k}"] for k in ("open", "high", "low", "close"))
    violations = int(((low > o) | (low > c) | (h < o) | (h < c) | (low <= 0)).sum())
    return DataQualityReport(
        dataset=dataset,
        rows=len(df),
        first_open_time=idx[0].to_pydatetime() if len(idx) else None,
        last_open_time=idx[-1].to_pydatetime() if len(idx) else None,
        gaps=tuple(gaps),
        duplicates=raw_len - len(df) - dropped,
        ohlc_violations=violations,
        dropped_incomplete=dropped,
        notes=(),
    )


def load_daily_klines(
    directory: Path, dataset: str, prefix: str, as_of: datetime
) -> tuple[pd.DataFrame, DataQualityReport]:
    if as_of.tzinfo is None:
        raise MarketDataError("as_of must be timezone-aware")
    rows = _read_pages(directory)
    df = klines_frame(rows, prefix=prefix)
    complete = df["close_time"] < pd.Timestamp(as_of)
    dropped = int((~complete).sum())
    df = df[complete]
    return df, _quality(df, dataset, prefix, dropped, len(rows))


@dataclass(slots=True)
class DailyDataset:
    """Aligned daily frame with trade, mark and index prices (all UTC, completed candles only)."""

    frame: pd.DataFrame
    quality: dict[str, DataQualityReport]
    symbol: str
    as_of: datetime

    @property
    def start(self) -> datetime:
        return self.frame.index[0].to_pydatetime()  # type: ignore[no-any-return]

    @property
    def end(self) -> datetime:
        return self.frame.index[-1].to_pydatetime()  # type: ignore[no-any-return]

    def __len__(self) -> int:
        return len(self.frame)

    def slice(self, start: datetime | None = None, end: datetime | None = None) -> DailyDataset:
        f = self.frame
        if start is not None:
            f = f[f.index >= pd.Timestamp(start)]
        if end is not None:
            f = f[f.index <= pd.Timestamp(end)]
        return DailyDataset(frame=f, quality=self.quality, symbol=self.symbol, as_of=self.as_of)

    def dec(self, ts: pd.Timestamp, column: str) -> Decimal:
        """Exact Decimal price for execution-critical use (open/close, mark_open/mark_close)."""
        return Decimal(str(self.frame.at[ts, f"{column}_dec"]))


def build_daily_dataset(
    raw_dir: Path,
    symbol: str = "BTCUSD_PERP",
    as_of: datetime | None = None,
    max_mark_index_divergence: float = 0.02,
    require_index: bool = True,
) -> DailyDataset:
    """Load + align trade/mark/index daily klines, drop incomplete candles, validate.

    Index price is used purely for validation (mark vs index divergence) — never for PnL.
    """
    as_of = as_of or datetime.now(tz=UTC)
    trade, q_trade = load_daily_klines(raw_dir / "klines_1d", "klines_1d", "", as_of)
    mark, q_mark = load_daily_klines(
        raw_dir / "mark_price_klines_1d", "mark_price_klines_1d", "mark_", as_of
    )
    frame = trade.join(mark.drop(columns=["close_time"]), how="inner")
    quality = {"klines_1d": q_trade, "mark_price_klines_1d": q_mark}
    notes: list[str] = []
    index_dir = raw_dir / "index_price_klines_1d"
    if index_dir.exists():
        index, q_index = load_daily_klines(index_dir, "index_price_klines_1d", "index_", as_of)
        frame = frame.join(index.drop(columns=["close_time"]), how="left")
        quality["index_price_klines_1d"] = q_index
        div = (frame["mark_close"] / frame["index_close"] - 1.0).abs()
        bad = frame.index[div > max_mark_index_divergence]
        if len(bad):
            notes.append(
                f"{len(bad)} days with |mark/index − 1| > {max_mark_index_divergence}: {[d.date().isoformat() for d in bad[:10]]}"
            )
        frame["mark_index_divergence"] = div
    elif require_index:
        raise MarketDataError(
            "index price klines are required for validation (data.require_index_validation)"
        )
    if frame.empty:
        raise MarketDataError("no aligned complete candles")
    missing_mark = int(frame["mark_close"].isna().sum())
    if missing_mark:
        raise MarketDataError(f"{missing_mark} days without mark price")
    # sanity: trade close vs mark close should be within a few percent
    tm = (frame["close"] / frame["mark_close"] - 1.0).abs()
    if (tm > 0.05).any():
        notes.append(f"{int((tm > 0.05).sum())} days with |close/mark − 1| > 5%")
    frame.attrs["notes"] = notes
    frame.attrs["symbol"] = symbol
    return DailyDataset(frame=frame, quality=quality, symbol=symbol, as_of=as_of)


def previous_day(ts: datetime) -> datetime:
    return ts - timedelta(days=1)
