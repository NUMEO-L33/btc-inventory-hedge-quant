"""Interval estimates, empirical p-values, and the control-arm strategies."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from btc_inventory_quant.backtest.statistics import (
    empirical_p_value,
    percentile_rank,
    sidak_alpha,
    wilson_interval,
)
from btc_inventory_quant.config.schema import FixedHedgeParams, RegimeHedgeParams
from btc_inventory_quant.strategy.benchmarks import FixedHedgeStrategy
from btc_inventory_quant.strategy.controls import (
    BlockShuffleControl,
    CoinFlipControl,
    ShuffledTimingControl,
    UniformRandomControl,
)
from btc_inventory_quant.strategy.regime_hedge import RegimeHedgeV1
from tests.helpers import make_dataset

D = Decimal


# --------------------------------------------------------------------------- Wilson
def test_wilson_matches_published_values() -> None:
    """2/7 is the win rate this project reported for months; its interval contains 0.5."""
    i = wilson_interval(2, 7)
    assert i.point == pytest.approx(2 / 7)
    assert i.low == pytest.approx(0.0824, abs=1e-3)
    assert i.high == pytest.approx(0.6410, abs=1e-3)
    assert i.contains_half  # -> not distinguishable from a coin flip


def test_wilson_edges_do_not_escape_zero_one() -> None:
    lo = wilson_interval(0, 7)
    hi = wilson_interval(7, 7)
    # at 0 successes the exact lower bound is 0; floating point leaves ~1e-17 behind
    assert lo.low == pytest.approx(0.0, abs=1e-12) and 0.0 < lo.high < 1.0
    assert 0.0 < hi.low < 1.0 and hi.high == pytest.approx(1.0, abs=1e-12)
    assert not hi.contains_half  # 7/7 does exclude a coin flip


def test_wilson_narrows_with_n() -> None:
    small = wilson_interval(20, 40)
    large = wilson_interval(200, 400)
    assert (large.high - large.low) < (small.high - small.low)
    assert small.contains_half and large.contains_half  # both centred on 0.5


def test_wilson_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        wilson_interval(8, 7)
    with pytest.raises(ValueError):
        wilson_interval(-1, 7)
    assert wilson_interval(0, 0).n == 0


# --------------------------------------------------------------------------- p-values
def test_empirical_p_value_has_a_floor_of_one_over_n_plus_one() -> None:
    null = list(np.linspace(0.0, 1.0, 100))
    assert empirical_p_value(999.0, null) == pytest.approx(1 / 101)
    assert empirical_p_value(-999.0, null) == pytest.approx(1.0)


def test_empirical_p_value_direction() -> None:
    null = [0.0, 1.0, 2.0, 3.0]
    assert empirical_p_value(2.5, null, higher_is_better=True) == pytest.approx(2 / 5)
    assert empirical_p_value(0.5, null, higher_is_better=False) == pytest.approx(2 / 5)


def test_percentile_rank_and_nan_handling() -> None:
    assert percentile_rank(1.5, [0.0, 1.0, 2.0, 3.0]) == pytest.approx(50.0)
    assert empirical_p_value(1.0, [float("nan")]) != empirical_p_value(1.0, [1.0])
    assert np.isnan(percentile_rank(1.0, [float("nan")]))


def test_sidak_alpha_tightens_with_family_size() -> None:
    assert sidak_alpha(0.05, 1) == pytest.approx(0.05)
    assert sidak_alpha(0.05, 18) == pytest.approx(0.002846, abs=1e-5)
    assert sidak_alpha(0.05, 18) < sidak_alpha(0.05, 5) < 0.05
    with pytest.raises(ValueError):
        sidak_alpha(0.05, 0)


# --------------------------------------------------------------------------- controls
def _prices(n: int = 400) -> np.ndarray:
    rng = np.random.default_rng(7)
    return 30_000.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))


def test_shuffled_control_preserves_the_target_distribution_but_not_the_order() -> None:
    ds = make_dataset(_prices())
    base = RegimeHedgeV1(RegimeHedgeParams())
    ctrl = ShuffledTimingControl(base, seed=1)
    base_out = base.compute(ds.frame)["raw_target"].dropna()
    ctrl_out = ctrl.compute(ds.frame)["raw_target"].dropna()
    assert sorted(base_out.tolist()) == pytest.approx(sorted(ctrl_out.tolist()))  # same multiset
    assert base_out.tolist() != ctrl_out.tolist()  # different order
    assert ctrl.warmup_bars == base.warmup_bars


def test_shuffled_control_is_deterministic_per_seed() -> None:
    ds = make_dataset(_prices())
    base = RegimeHedgeV1(RegimeHedgeParams())
    a = ShuffledTimingControl(base, seed=3).compute(ds.frame)["raw_target"]
    b = ShuffledTimingControl(base, seed=3).compute(ds.frame)["raw_target"]
    c = ShuffledTimingControl(base, seed=4).compute(ds.frame)["raw_target"]
    assert a.equals(b)
    assert not a.equals(c)


def test_shuffling_a_static_strategy_is_the_identity() -> None:
    """Why the study skips the shuffled control for static arms instead of reporting p=1."""
    ds = make_dataset(_prices())
    base = FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.2")))
    shuffled = ShuffledTimingControl(base, seed=1).compute(ds.frame)["raw_target"]
    assert shuffled.dropna().nunique() == 1


def test_random_controls_respect_the_cap_and_hit_the_requested_mean() -> None:
    ds = make_dataset(_prices(2000))
    for ctrl in (
        UniformRandomControl(0.3, seed=2, warmup=10),
        CoinFlipControl(0.3, seed=2, warmup=10),
    ):
        t = ctrl.compute(ds.frame)["raw_target"].dropna()
        assert t.min() >= 0.0 and t.max() <= 0.85
        assert t.mean() == pytest.approx(0.3, abs=0.03)


def test_random_controls_blank_the_warmup_and_are_seed_deterministic() -> None:
    ds = make_dataset(_prices(300))
    out = UniformRandomControl(0.4, seed=5, warmup=50).compute(ds.frame)
    assert out["raw_target"].iloc[:50].isna().all()
    assert out["raw_target"].iloc[50:].notna().all()
    again = UniformRandomControl(0.4, seed=5, warmup=50).compute(ds.frame)
    assert out["raw_target"].equals(again["raw_target"])


def test_coin_flip_control_caps_a_high_requested_mean() -> None:
    ds = make_dataset(_prices(500))
    t = CoinFlipControl(0.8, seed=1, warmup=10).compute(ds.frame)["raw_target"].dropna()
    assert set(np.round(t.unique(), 6)) <= {0.0, 0.85}  # 2*0.8 clipped to the cap


def test_block_shuffle_preserves_run_structure_and_trade_count() -> None:
    """The bar-wise shuffle destroys persistence and so inflates trades; the block shuffle must not."""
    ds = make_dataset(_prices(600))
    base = RegimeHedgeV1(RegimeHedgeParams())
    raw = base.compute(ds.frame)["raw_target"].dropna().to_numpy()

    block = BlockShuffleControl(base, seed=1).compute(ds.frame)["raw_target"].dropna().to_numpy()
    bar = ShuffledTimingControl(base, seed=1).compute(ds.frame)["raw_target"].dropna().to_numpy()

    def transitions(a: np.ndarray) -> int:
        return int(np.count_nonzero(np.diff(a)))

    # same multiset of values as the original
    assert sorted(block.tolist()) == pytest.approx(sorted(raw.tolist()))
    # Transitions are NOT preserved exactly: permuting runs can place two runs of the same value
    # next to each other, which merges them and removes one transition. The property that matters
    # is that the count stays close, so the control pays comparable trading costs.
    assert transitions(block) <= transitions(raw)
    assert transitions(block) >= transitions(raw) * 0.6
    # the bar-wise shuffle does NOT stay close — this is the confound it introduces
    assert transitions(bar) > transitions(raw) * 2
    # and it is a real reordering, not the identity
    assert block.tolist() != raw.tolist()


def test_block_shuffle_keeps_runs_long_unlike_the_bar_shuffle() -> None:
    """Mean run length is the quantity that drives trade count, and it must survive the shuffle."""
    ds = make_dataset(_prices(600))
    base = RegimeHedgeV1(RegimeHedgeParams())

    def mean_run(a: np.ndarray) -> float:
        if len(a) == 0:
            return 0.0
        cuts = np.flatnonzero(np.diff(a) != 0) + 1
        return float(np.mean([len(r) for r in np.split(a, cuts)]))

    raw = base.compute(ds.frame)["raw_target"].dropna().to_numpy()
    block_means = [
        mean_run(BlockShuffleControl(base, s).compute(ds.frame)["raw_target"].dropna().to_numpy())
        for s in (0, 1, 2)
    ]
    bar_mean = mean_run(
        ShuffledTimingControl(base, 0).compute(ds.frame)["raw_target"].dropna().to_numpy()
    )
    assert all(m >= mean_run(raw) for m in block_means)  # merges can only lengthen runs
    assert bar_mean < mean_run(raw) / 2  # the bar shuffle roughly halves them or worse


def test_block_shuffle_is_seed_deterministic_and_static_safe() -> None:
    ds = make_dataset(_prices(300))
    base = RegimeHedgeV1(RegimeHedgeParams())
    a = BlockShuffleControl(base, seed=9).compute(ds.frame)["raw_target"]
    b = BlockShuffleControl(base, seed=9).compute(ds.frame)["raw_target"]
    assert a.equals(b)
    static = FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.2")))
    out = BlockShuffleControl(static, seed=1).compute(ds.frame)["raw_target"].dropna()
    assert out.nunique() == 1  # a single run permutes to itself
