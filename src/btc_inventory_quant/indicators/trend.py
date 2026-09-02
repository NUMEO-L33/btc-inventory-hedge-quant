"""Trend indicators (float/pandas is acceptable here: indicators never touch orders directly).

All indicators are *causal*: the value at bar t depends only on bars ≤ t. This is asserted by
``tests/unit/test_indicators_strategy.py::test_indicators_are_causal``.
"""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, alpha = 2/(period+1), seeded with the first value (adjust=False)."""
    if period < 1:
        raise ValueError("period must be >= 1")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def slope_is_up(series: pd.Series, lookback: int) -> pd.Series:
    """True where value[t] > value[t-lookback]. NaN-safe (False where undefined)."""
    prev = series.shift(lookback)
    return (series > prev) & series.notna() & prev.notna()


def slope_is_down(series: pd.Series, lookback: int) -> pd.Series:
    prev = series.shift(lookback)
    return (series < prev) & series.notna() & prev.notna()
