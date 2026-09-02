"""Strategy V1: regime-based dynamic hedge ratio (rule based, no ML).

Signals use completed UTC daily candles only.

Regime (per completed bar):
    bull    : close > EMA200 and EMA50[t] > EMA50[t−5]
    bear    : close < EMA200 and EMA50[t] < EMA50[t−5]
    neutral : otherwise
Confirmed regime changes only after ``regime_confirm_bars`` consecutive bars of the new regime.

Base target by confirmed regime: bull 10% / neutral 45% / bear 80%.
Adjustments (applied on the bar's own values):
    +10pp if close > EMA20 + 2·ATR20              (overheated → hedge more)
    −10pp if close < EMA20 − 2·ATR20 and close > previous close (oversold bounce → hedge less)
Raw target is clipped to [0, 1] here; the risk engine clamps to [min, max]=[0, 0.85], applies the
20pp/day cap and the 10pp minimum difference using the *effective* hedge ratio.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from btc_inventory_quant.config.schema import RegimeHedgeParams
from btc_inventory_quant.indicators.trend import ema, slope_is_down, slope_is_up
from btc_inventory_quant.indicators.volatility import atr
from btc_inventory_quant.strategy.base import Regime, validate_signal_frame
from btc_inventory_quant.strategy.state_machine import RegimeStateMachine


class RegimeHedgeV1:
    name = "regime_hedge_v1"

    def __init__(self, params: RegimeHedgeParams | None = None) -> None:
        self.params = params or RegimeHedgeParams()

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(p.ema_slow, p.ema_mid + p.slope_lookback, p.atr_period) + p.regime_confirm_bars

    @property
    def bypass_max_hedge_cap(self) -> bool:
        return False

    def raw_regimes(self, frame: pd.DataFrame) -> pd.Series:
        p = self.params
        close = frame["close"]
        ema_slow = ema(close, p.ema_slow)
        ema_mid = ema(close, p.ema_mid)
        up = slope_is_up(ema_mid, p.slope_lookback)
        down = slope_is_down(ema_mid, p.slope_lookback)
        bull = (close > ema_slow) & up
        bear = (close < ema_slow) & down
        labels = np.select(
            [bull.to_numpy(), bear.to_numpy()],
            [Regime.BULL.value, Regime.BEAR.value],
            default=Regime.NEUTRAL.value,
        ).astype(object)
        undefined = (ema_slow.isna() | ema_mid.isna()).to_numpy()
        labels[undefined] = np.nan
        regime = pd.Series(labels, index=frame.index, dtype=object)
        return regime

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = frame["close"]
        out = pd.DataFrame(index=frame.index)
        out["ema_fast"] = ema(close, p.ema_fast)
        out["ema_mid"] = ema(close, p.ema_mid)
        out["ema_slow"] = ema(close, p.ema_slow)
        out["atr"] = atr(frame["high"], frame["low"], close, p.atr_period)
        raw = self.raw_regimes(frame)
        out["regime_raw"] = raw

        sm = RegimeStateMachine(confirm_bars=p.regime_confirm_bars)
        confirmed: list[str | float] = []
        for r in raw:
            if isinstance(r, float) and np.isnan(r):
                confirmed.append(np.nan)
                continue
            confirmed.append(sm.update(Regime(r)).value)
        out["regime"] = confirmed

        base_map = {
            Regime.BULL.value: float(p.hedge_bull),
            Regime.NEUTRAL.value: float(p.hedge_neutral),
            Regime.BEAR.value: float(p.hedge_bear),
        }
        out["base_target"] = out["regime"].map(base_map)

        upper = out["ema_fast"] + float(p.overbought_atr_mult) * out["atr"]
        lower = out["ema_fast"] - float(p.oversold_atr_mult) * out["atr"]
        overbought = close > upper
        oversold_bounce = (close < lower) & (close > close.shift(1))
        adj = pd.Series(0.0, index=frame.index)
        adj[overbought] += float(p.overbought_adjustment)
        adj[oversold_bounce] += float(p.oversold_adjustment)
        adj[out["atr"].isna() | out["ema_fast"].isna()] = 0.0
        out["adjustment"] = adj
        out["overbought"] = overbought
        out["oversold_bounce"] = oversold_bounce

        raw_target = (out["base_target"] + adj).clip(lower=0.0, upper=1.0)
        warm = np.arange(len(frame)) < self.warmup_bars
        raw_target[warm] = np.nan
        out["raw_target"] = raw_target
        validate_signal_frame(out, frame)
        return out
