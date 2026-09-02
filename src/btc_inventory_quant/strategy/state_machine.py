"""Regime confirmation state machine.

A newly observed regime must persist for ``confirm_bars`` consecutive *completed* bars before
the confirmed regime changes. Sequential by construction (no look-ahead).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from btc_inventory_quant.strategy.base import Regime


@dataclass(slots=True)
class RegimeStateMachine:
    confirm_bars: int = 2
    initial: Regime = Regime.NEUTRAL
    confirmed: Regime = field(init=False)
    _candidate: Regime | None = field(init=False, default=None)
    _streak: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if self.confirm_bars < 1:
            raise ValueError("confirm_bars must be >= 1")
        self.confirmed = self.initial

    def update(self, observed: Regime) -> Regime:
        """Feed one completed bar's raw regime; return the confirmed regime after this bar."""
        if observed == self.confirmed:
            self._candidate = None
            self._streak = 0
            return self.confirmed
        if observed == self._candidate:
            self._streak += 1
        else:
            self._candidate = observed
            self._streak = 1
        if self._streak >= self.confirm_bars:
            self.confirmed = observed
            self._candidate = None
            self._streak = 0
        return self.confirmed

    def run(self, observations: Iterable[Regime]) -> list[Regime]:
        return [self.update(o) for o in observations]

    @property
    def pending(self) -> tuple[Regime | None, int]:
        return self._candidate, self._streak
