"""Funding guard (V2 step 1): reduce the short when carrying it has become expensive.

Motivation (docs/strategy_v2.md): in the V1 backtest the entire net BTC gain came from positive
funding received by the short, and the *only* scenario that flipped the strategy to FAIL was
adverse funding. The guard is therefore not a market-direction forecast — it is a **cost rule**:

    if mean settled funding rate over the last ``lookback_periods`` settlements < ``threshold``
        target_hedge_ratio ×= ``scale``        (and is additionally capped at ``max_ratio_when_adverse``)

It wraps ANY base strategy (fixed hedge, regime hedge, EMA200 …) so the guard's contribution can
be measured independently of the base signal — that is the whole point of running the 2×2
comparison base × guard.

Inputs are the causal funding features from :mod:`btc_inventory_quant.data.funding_features`;
the feature at bar t only contains settlements that had happened by the close of bar t, and the
engine executes at the open of bar t+1, so there is no look-ahead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from btc_inventory_quant.config.schema import FundingGuardParams
from btc_inventory_quant.data.funding_features import DEFAULT_LOOKBACK
from btc_inventory_quant.strategy.base import Strategy, validate_signal_frame


class FundingGuardStrategy:
    """Decorator strategy: base target × guard factor."""

    def __init__(self, base: Strategy, params: FundingGuardParams) -> None:
        self.base = base
        self.params = params
        self.name = f"{base.name}+funding_guard"

    @property
    def warmup_bars(self) -> int:
        # the guard needs its own window of settled funding as well (3 settlements per day)
        guard_days = -(-self.params.lookback_periods // 3)
        return max(self.base.warmup_bars, guard_days)

    @property
    def bypass_max_hedge_cap(self) -> bool:
        return self.base.bypass_max_hedge_cap

    @property
    def requires_funding(self) -> bool:
        return True

    @property
    def funding_lookbacks(self) -> tuple[int, ...]:
        base_lookbacks = getattr(self.base, "funding_lookbacks", ())
        return tuple({self.params.lookback_periods, *base_lookbacks})

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = self.base.compute(frame)
        col_mean = f"funding_mean_{p.lookback_periods}"
        col_periods = f"funding_periods_{p.lookback_periods}"
        if col_mean not in frame.columns:
            raise ValueError(
                f"funding guard needs column {col_mean!r}; attach funding features to the frame "
                "(data.funding_features.attach_funding_features) before calling compute()"
            )
        mean = frame[col_mean].astype(float)
        periods = (
            frame[col_periods].astype(float)
            if col_periods in frame.columns
            else pd.Series(np.inf, index=frame.index)
        )
        ready = periods >= p.min_periods
        adverse = ready & (mean < float(p.threshold_rate))

        factor = pd.Series(1.0, index=frame.index)
        factor[adverse] = float(p.scale)
        guarded = out["raw_target"].astype(float) * factor
        cap = float(p.max_ratio_when_adverse)
        guarded[adverse] = guarded[adverse].clip(upper=cap)

        out = out.copy()
        out["base_target"] = out["raw_target"]
        out["funding_mean"] = mean
        out["funding_adverse"] = adverse
        out["funding_guard_factor"] = factor
        out["raw_target"] = guarded.clip(lower=0.0, upper=1.0)
        # warm-up: the guard cannot be evaluated before enough settlements exist
        warm = np.arange(len(frame)) < self.warmup_bars
        out.loc[warm, "raw_target"] = np.nan
        validate_signal_frame(out, frame)
        return out


def guard_summary(signals: pd.DataFrame) -> dict[str, float]:
    """How often the guard fired and how much it changed the target (for reports)."""
    if "funding_adverse" not in signals.columns:
        return {}
    valid = signals["raw_target"].notna()
    adverse = signals.loc[valid, "funding_adverse"].astype(bool)
    base = signals.loc[valid, "base_target"].astype(float)
    guarded = signals.loc[valid, "raw_target"].astype(float)
    n = int(valid.sum())
    return {
        "days_evaluated": n,
        "days_guard_active": int(adverse.sum()),
        "share_guard_active": float(adverse.mean()) if n else float("nan"),
        "mean_base_target": float(base.mean()) if n else float("nan"),
        "mean_guarded_target": float(guarded.mean()) if n else float("nan"),
        "mean_reduction_when_active": float((base[adverse] - guarded[adverse]).mean())
        if adverse.any()
        else 0.0,
    }


__all__ = ["DEFAULT_LOOKBACK", "FundingGuardStrategy", "guard_summary"]
