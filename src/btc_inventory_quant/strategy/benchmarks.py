"""Benchmark strategies: HODL, fixed hedge (50% / 100%), EMA200 simple hedge."""

from __future__ import annotations

import numpy as np
import pandas as pd

from btc_inventory_quant.config.schema import (
    Ema200SimpleParams,
    FixedHedgeParams,
    StrategyConfig,
    StrategyName,
)
from btc_inventory_quant.indicators.trend import ema
from btc_inventory_quant.strategy.base import Regime, Strategy, validate_signal_frame
from btc_inventory_quant.strategy.regime_hedge import RegimeHedgeV1


class HodlStrategy:
    name = "hodl"

    @property
    def warmup_bars(self) -> int:
        return 0

    @property
    def bypass_max_hedge_cap(self) -> bool:
        return False

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=frame.index)
        out["raw_target"] = 0.0
        out["regime"] = Regime.NEUTRAL.value
        validate_signal_frame(out, frame)
        return out


class FixedHedgeStrategy:
    def __init__(self, params: FixedHedgeParams) -> None:
        self.params = params
        self.name = f"fixed_hedge_{round(float(params.ratio) * 100)}"

    @property
    def warmup_bars(self) -> int:
        return 0

    @property
    def bypass_max_hedge_cap(self) -> bool:
        return self.params.bypass_max_hedge_cap

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=frame.index)
        out["raw_target"] = float(self.params.ratio)
        out["regime"] = Regime.NEUTRAL.value
        validate_signal_frame(out, frame)
        return out


class Ema200SimpleStrategy:
    name = "ema200_simple"

    def __init__(self, params: Ema200SimpleParams) -> None:
        self.params = params

    @property
    def warmup_bars(self) -> int:
        return self.params.ema_period

    @property
    def bypass_max_hedge_cap(self) -> bool:
        return False

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        close = frame["close"]
        e = ema(close, self.params.ema_period)
        out = pd.DataFrame(index=frame.index)
        below = close < e
        target = pd.Series(float(self.params.above_ratio), index=frame.index)
        target[below] = float(self.params.below_ratio)
        target[e.isna()] = np.nan
        warm = np.arange(len(frame)) < self.warmup_bars
        target[warm] = np.nan
        out["raw_target"] = target
        out["regime"] = np.where(below, Regime.BEAR.value, Regime.BULL.value)
        out["ema_slow"] = e
        validate_signal_frame(out, frame)
        return out


def build_strategy(cfg: StrategyConfig) -> Strategy:
    match cfg.name:
        case StrategyName.REGIME_HEDGE_V1:
            return RegimeHedgeV1(cfg.regime_hedge)
        case StrategyName.HODL:
            return HodlStrategy()
        case StrategyName.FIXED_HEDGE:
            return FixedHedgeStrategy(cfg.fixed_hedge)
        case StrategyName.EMA200_SIMPLE:
            return Ema200SimpleStrategy(cfg.ema200_simple)
    raise ValueError(f"unknown strategy {cfg.name}")  # pragma: no cover
