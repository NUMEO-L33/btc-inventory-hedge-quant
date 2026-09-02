"""Interval estimates and empirical p-values — so a win rate is never reported as a bare fraction.

Motivation: every walk-forward win rate reported in this project before 2026-09-02 was a point
estimate over 7 folds ("2/7 folds positive"). A 95% Wilson interval on 2/7 is [0.082, 0.641] —
it contains 0.5, so that statistic cannot distinguish the strategy from a coin flip, and a
comparison against another arm at 1/7 (CI [0.026, 0.513]) is not evidence of anything. The
``docs/strategy_v2.md`` claim that static hedging beat the dynamic regime cited exactly such a
comparison as one of its four axes; that axis has been withdrawn.

Wilson is used rather than the normal (Wald) interval because Wald is badly wrong for small n and
for p near 0 or 1 — at n = 7 it is unusable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Z95 = 1.959963984540054


@dataclass(frozen=True, slots=True)
class Interval:
    point: float
    low: float
    high: float
    n: int

    @property
    def contains_half(self) -> bool:
        """True when the interval cannot rule out a coin flip."""
        return self.low <= 0.5 <= self.high

    def __str__(self) -> str:
        return f"{self.point:.3f} [{self.low:.3f}, {self.high:.3f}] (n={self.n})"

    def to_dict(self) -> dict[str, float | int | bool]:
        return {
            "point": self.point,
            "ci_low": self.low,
            "ci_high": self.high,
            "n": self.n,
            "indistinguishable_from_coin_flip": self.contains_half,
        }


def wilson_interval(successes: int, n: int, z: float = Z95) -> Interval:
    """Wilson score interval for a binomial proportion (correct at small n, unlike Wald)."""
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"invalid counts: {successes}/{n}")
    if n == 0:
        return Interval(point=float("nan"), low=0.0, high=1.0, n=0)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z / denom * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return Interval(point=p, low=max(0.0, centre - half), high=min(1.0, centre + half), n=n)


def empirical_p_value(
    observed: float, null_samples: Sequence[float], higher_is_better: bool = True
) -> float:
    """One-sided p-value against a null distribution, with the +1 correction.

    ``p = (1 + #{null at least as extreme as observed}) / (1 + N)`` — the +1 keeps p strictly
    positive, which is the honest floor given a finite number of control draws (with N samples
    the smallest reportable p is 1/(N+1)).
    """
    samples = [s for s in null_samples if not math.isnan(s)]
    if not samples:
        return float("nan")
    if higher_is_better:
        at_least_as_extreme = sum(1 for s in samples if s >= observed)
    else:
        at_least_as_extreme = sum(1 for s in samples if s <= observed)
    return (1 + at_least_as_extreme) / (1 + len(samples))


def percentile_rank(observed: float, null_samples: Sequence[float]) -> float:
    """Share of the null distribution the observed value beats, in percent."""
    samples = [s for s in null_samples if not math.isnan(s)]
    if not samples:
        return float("nan")
    return 100.0 * sum(1 for s in samples if s < observed) / len(samples)


def sidak_alpha(alpha: float, n_tests: int) -> float:
    """Family-wise corrected significance level for ``n_tests`` correlated-but-treated-as-independent tests."""
    if n_tests < 1:
        raise ValueError("n_tests must be >= 1")
    return float(1.0 - (1.0 - alpha) ** (1.0 / n_tests))


__all__ = [
    "Z95",
    "Interval",
    "empirical_p_value",
    "percentile_rank",
    "sidak_alpha",
    "wilson_interval",
]
