"""Strategy interface.

A strategy converts *completed* daily candles into a **raw target hedge ratio** per bar.
It knows nothing about orders, exchanges or the current position: clamping to risk limits,
the daily change cap and the min-rebalance filter are applied downstream by the risk engine
(:mod:`btc_inventory_quant.risk.limits`) using the live effective hedge ratio.

Contract: ``compute(frame)`` returns a DataFrame aligned to ``frame.index`` with at least the
columns ``raw_target`` (float in [0, 1], NaN during warm-up) and ``regime`` (str). Values at bar
t must depend only on bars ≤ t (causality is unit-tested).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

import pandas as pd


class Regime(StrEnum):
    BULL = "bull"
    NEUTRAL = "neutral"
    BEAR = "bear"


class Strategy(Protocol):
    name: str

    @property
    def warmup_bars(self) -> int: ...

    @property
    def bypass_max_hedge_cap(self) -> bool: ...

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame: ...


REQUIRED_COLUMNS = ("raw_target", "regime")


def validate_signal_frame(signals: pd.DataFrame, frame: pd.DataFrame) -> None:
    for c in REQUIRED_COLUMNS:
        if c not in signals.columns:
            raise ValueError(f"strategy output lacks column {c!r}")
    if not signals.index.equals(frame.index):
        raise ValueError("strategy output index must equal the input index")
    valid = signals["raw_target"].dropna()
    if ((valid < 0) | (valid > 1)).any():
        raise ValueError("raw_target must lie within [0, 1]")
