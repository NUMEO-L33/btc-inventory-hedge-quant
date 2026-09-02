from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from btc_inventory_quant.config.schema import RegimeHedgeParams
from btc_inventory_quant.indicators.trend import ema, slope_is_up
from btc_inventory_quant.indicators.volatility import atr
from btc_inventory_quant.strategy.base import Regime
from btc_inventory_quant.strategy.regime_hedge import RegimeHedgeV1
from btc_inventory_quant.strategy.state_machine import RegimeStateMachine
from tests.helpers import make_dataset, synthetic_prices, trending_prices


def test_indicators_are_causal() -> None:
    prices = pd.Series(synthetic_prices(400))
    full = ema(prices, 50)
    for cut in (120, 250, 399):
        part = ema(prices.iloc[: cut + 1], 50)
        assert np.isclose(part.iloc[-1], full.iloc[cut])
    high, low = prices * 1.01, prices * 0.99
    full_atr = atr(high, low, prices, 20)
    part_atr = atr(high.iloc[:300], low.iloc[:300], prices.iloc[:300], 20)
    assert np.isclose(part_atr.iloc[-1], full_atr.iloc[299])
    assert full.iloc[:49].isna().all()
    assert slope_is_up(pd.Series([1.0, 2.0, 3.0, 2.0]), 1).tolist() == [False, True, True, False]


def test_state_machine_requires_two_bars() -> None:
    sm = RegimeStateMachine(confirm_bars=2)
    seq = [
        Regime.NEUTRAL,
        Regime.BULL,
        Regime.NEUTRAL,
        Regime.BULL,
        Regime.BULL,
        Regime.BEAR,
        Regime.BEAR,
        Regime.BEAR,
    ]
    out = sm.run(seq)
    assert out == [
        Regime.NEUTRAL,
        Regime.NEUTRAL,  # one bull bar is not enough
        Regime.NEUTRAL,
        Regime.NEUTRAL,  # streak restarted
        Regime.BULL,  # second consecutive bull bar → confirmed
        Regime.BULL,
        Regime.BEAR,
        Regime.BEAR,
    ]
    with pytest.raises(ValueError):
        RegimeStateMachine(confirm_bars=0)


def test_regime_rules_on_trends() -> None:
    strat = RegimeHedgeV1(RegimeHedgeParams())
    up = make_dataset(trending_prices(400, daily=0.002))
    sig_up = strat.compute(up.frame)
    assert sig_up["regime"].iloc[-1] == Regime.BULL.value
    assert sig_up["raw_target"].iloc[-1] == pytest.approx(
        0.10
    )  # bull base (no overbought at constant drift)
    down = make_dataset(trending_prices(400, daily=-0.002))
    sig_down = strat.compute(down.frame)
    assert sig_down["regime"].iloc[-1] == Regime.BEAR.value
    assert sig_down["raw_target"].iloc[-1] == pytest.approx(0.80)
    flat = make_dataset(np.full(400, 30_000.0))
    sig_flat = strat.compute(flat.frame)
    assert sig_flat["regime"].iloc[-1] == Regime.NEUTRAL.value
    assert sig_flat["raw_target"].iloc[-1] == pytest.approx(0.45)
    assert sig_up["raw_target"].iloc[: strat.warmup_bars].isna().all()
    valid = sig_up["raw_target"].dropna()
    assert ((valid >= 0) & (valid <= 1)).all()


def test_overbought_and_oversold_adjustments() -> None:
    strat = RegimeHedgeV1(RegimeHedgeParams())
    base = trending_prices(400, daily=0.002)
    spike = base.copy()
    spike[-1] = base[-1] * 1.25  # far above EMA20 + 2·ATR
    sig = strat.compute(make_dataset(spike).frame)
    assert bool(sig["overbought"].iloc[-1])
    assert sig["adjustment"].iloc[-1] == pytest.approx(0.10)
    crash = base.copy()
    crash[-2] = base[-2] * 0.70  # crash below EMA20 − 2·ATR ...
    crash[-1] = (
        crash[-2] * 1.02
    )  # ... then a bounce (close > previous close) while still below the band
    sig2 = strat.compute(make_dataset(crash).frame)
    assert bool(sig2["oversold_bounce"].iloc[-1])
    assert sig2["adjustment"].iloc[-1] == pytest.approx(-0.10)


def test_strategy_is_causal() -> None:
    strat = RegimeHedgeV1(RegimeHedgeParams())
    ds = make_dataset(synthetic_prices(500, seed=3))
    full = strat.compute(ds.frame)
    for cut in (260, 380, 499):
        part = strat.compute(ds.frame.iloc[: cut + 1])
        assert part["regime"].iloc[-1] == full["regime"].iloc[cut]
        assert np.isclose(part["raw_target"].iloc[-1], full["raw_target"].iloc[cut])
