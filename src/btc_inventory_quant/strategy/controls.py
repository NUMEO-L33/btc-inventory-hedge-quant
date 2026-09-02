"""Control arms — what score does a WORTHLESS strategy get on this data?

Every result in this project so far has been compared against HODL and against other real
strategies. None was compared against a null. Without a null there is no way to say whether a
walk-forward score reflects an edge or just the exposure level plus luck, which is exactly the
question left open by ``docs/strategy_v3_static_ratio.md``.

Three controls, in increasing strength:

``ShuffledTimingControl``  Takes a REAL strategy's target series and permutes it in time. The
                           marginal distribution of targets is identical — same mean exposure,
                           same variance, same values — and only the temporal alignment with the
                           market is destroyed. If a strategy cannot beat its own shuffled self,
                           its timing carries no information. This is the decisive control.
``UniformRandomControl``   Targets drawn i.i.d. from U(0, 2·mean), clipped to the cap.
``CoinFlipControl``        Targets drawn i.i.d. from {0, 2·mean}, p = 0.5.

All three emit targets into the SAME risk pipeline as a real strategy (daily change cap, minimum
rebalance distance, hard hedge cap), so the comparison is like-for-like rather than an idealised
random allocation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from btc_inventory_quant.strategy.base import Regime, Strategy, validate_signal_frame

DEFAULT_CAP = 0.85


def _frame_out(index: pd.Index, targets: np.ndarray, warmup: int) -> pd.DataFrame:
    out = pd.DataFrame(index=index)
    values = targets.astype(float).copy()
    values[:warmup] = np.nan
    out["raw_target"] = values
    out["regime"] = str(Regime.NEUTRAL)
    return out


class ShuffledTimingControl:
    """A real strategy's own targets, permuted in time. Destroys timing, preserves everything else."""

    def __init__(self, base: Strategy, seed: int) -> None:
        self.base = base
        self.seed = seed
        self.name = f"{base.name}+shuffled(seed={seed})"

    @property
    def warmup_bars(self) -> int:
        return self.base.warmup_bars

    @property
    def bypass_max_hedge_cap(self) -> bool:
        return self.base.bypass_max_hedge_cap

    @property
    def requires_funding(self) -> bool:
        return bool(getattr(self.base, "requires_funding", False))

    @property
    def funding_lookbacks(self) -> tuple[int, ...]:
        return tuple(getattr(self.base, "funding_lookbacks", ()))

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        base_out = self.base.compute(frame)
        raw = base_out["raw_target"].to_numpy(dtype=float)
        valid = ~np.isnan(raw)
        shuffled = raw.copy()
        rng = np.random.default_rng(self.seed)
        shuffled[valid] = rng.permutation(raw[valid])
        out = base_out.copy()
        out["raw_target"] = shuffled
        out["base_target"] = raw
        validate_signal_frame(out, frame)
        return out


class BlockShuffleControl:
    """A real strategy's targets with its constant-value RUNS permuted, not its individual bars.

    ``ShuffledTimingControl`` destroys persistence as well as timing: permuting bar by bar turns a
    strategy that holds one target for weeks into one that changes almost daily, so the control
    trades far more than the arm it is compared against (measured: 1,609 vs 243 trades for
    regime_hedge_v1, with 69% of the resulting BTC gap explained by fees alone). Any "significance"
    against it is partly transaction-cost avoidance rather than predictive skill.

    Permuting whole runs instead keeps the multiset of run lengths and values intact, so the number
    of target transitions — and therefore the trade count and cost profile — is preserved, and only
    the alignment between the runs and the market is destroyed. That isolates timing.
    """

    def __init__(self, base: Strategy, seed: int) -> None:
        self.base = base
        self.seed = seed
        self.name = f"{base.name}+block_shuffled(seed={seed})"

    @property
    def warmup_bars(self) -> int:
        return self.base.warmup_bars

    @property
    def bypass_max_hedge_cap(self) -> bool:
        return self.base.bypass_max_hedge_cap

    @property
    def requires_funding(self) -> bool:
        return bool(getattr(self.base, "requires_funding", False))

    @property
    def funding_lookbacks(self) -> tuple[int, ...]:
        return tuple(getattr(self.base, "funding_lookbacks", ()))

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        base_out = self.base.compute(frame)
        raw = base_out["raw_target"].to_numpy(dtype=float)
        valid_mask = ~np.isnan(raw)
        values = raw[valid_mask]

        # split into runs of equal consecutive value, then permute the runs
        if len(values) == 0:
            runs: list[np.ndarray] = []
        else:
            boundaries = np.flatnonzero(np.diff(values) != 0) + 1
            runs = np.split(values, boundaries)
        rng = np.random.default_rng(self.seed)
        order = rng.permutation(len(runs))
        rebuilt = np.concatenate([runs[i] for i in order]) if runs else values

        shuffled = raw.copy()
        shuffled[valid_mask] = rebuilt
        out = base_out.copy()
        out["raw_target"] = shuffled
        out["base_target"] = raw
        validate_signal_frame(out, frame)
        return out


class UniformRandomControl:
    """Targets ~ U(0, 2*mean_ratio), clipped to ``cap``."""

    def __init__(
        self, mean_ratio: float, seed: int, warmup: int = 210, cap: float = DEFAULT_CAP
    ) -> None:
        self.mean_ratio = float(mean_ratio)
        self.seed = seed
        self._warmup = warmup
        self.cap = cap
        self.name = f"uniform_random(mean={mean_ratio:.2f},seed={seed})"

    @property
    def warmup_bars(self) -> int:
        return self._warmup

    @property
    def bypass_max_hedge_cap(self) -> bool:
        return False

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        rng = np.random.default_rng(self.seed)
        draws = rng.uniform(0.0, 2.0 * self.mean_ratio, size=len(frame))
        out = _frame_out(frame.index, np.clip(draws, 0.0, self.cap), self.warmup_bars)
        validate_signal_frame(out, frame)
        return out


class CoinFlipControl:
    """Targets ~ {0, 2*mean_ratio} with p = 0.5, clipped to ``cap``."""

    def __init__(
        self, mean_ratio: float, seed: int, warmup: int = 210, cap: float = DEFAULT_CAP
    ) -> None:
        self.mean_ratio = float(mean_ratio)
        self.seed = seed
        self._warmup = warmup
        self.cap = cap
        self.name = f"coin_flip(mean={mean_ratio:.2f},seed={seed})"

    @property
    def warmup_bars(self) -> int:
        return self._warmup

    @property
    def bypass_max_hedge_cap(self) -> bool:
        return False

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        rng = np.random.default_rng(self.seed)
        high = min(2.0 * self.mean_ratio, self.cap)
        draws = np.where(rng.random(len(frame)) < 0.5, 0.0, high)
        out = _frame_out(frame.index, draws, self.warmup_bars)
        validate_signal_frame(out, frame)
        return out


__all__ = [
    "BlockShuffleControl",
    "CoinFlipControl",
    "ShuffledTimingControl",
    "UniformRandomControl",
]
