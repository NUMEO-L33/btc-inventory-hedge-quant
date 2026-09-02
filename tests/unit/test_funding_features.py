"""Funding features + funding guard: causality, correctness, and guard behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from btc_inventory_quant.config.schema import FixedHedgeParams, FundingGuardParams, StrategyConfig
from btc_inventory_quant.data.funding_data import FundingEvent
from btc_inventory_quant.data.funding_features import (
    attach_funding_features,
    build_funding_features,
    rows_to_events,
)
from btc_inventory_quant.strategy.benchmarks import FixedHedgeStrategy, build_strategy
from btc_inventory_quant.strategy.funding_guard import FundingGuardStrategy, guard_summary
from tests.helpers import make_dataset

D = Decimal
START = datetime(2021, 1, 1, tzinfo=UTC)


def _events(rates: list[float], start: datetime = START) -> list[FundingEvent]:
    """3 settlements per day (00/08/16 UTC) with the given rates, in order."""
    out = []
    for i, r in enumerate(rates):
        t = start + timedelta(hours=8 * i)
        out.append(
            FundingEvent(
                funding_time=t,
                rate=D(str(r)),
                mark_price=None,
                raw_funding_time_ms=int(t.timestamp() * 1000),
            )
        )
    return out


def _index(days: int, start: datetime = START) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=days, freq="D", tz="UTC")


def test_window_math_matches_a_manual_computation() -> None:
    rates = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]  # 2 days × 3 settlements
    f = build_funding_features(_events(rates), _index(2), lookback_periods=3)
    # day 0 closes after settlements 0..2 → last 3 = [0.01, 0.02, 0.03]
    assert f["funding_rate_last"].iloc[0] == pytest.approx(0.03)
    assert f["funding_mean_3"].iloc[0] == pytest.approx(0.02)
    assert f["funding_sum_3"].iloc[0] == pytest.approx(0.06)
    assert f["funding_periods_3"].iloc[0] == 3
    # day 1 closes after settlements 0..5 → last 3 = [0.04, 0.05, 0.06]
    assert f["funding_mean_3"].iloc[1] == pytest.approx(0.05)
    assert f["funding_annual_3"].iloc[1] == pytest.approx(0.05 * 3 * 365)


def test_warmup_reports_partial_window() -> None:
    f = build_funding_features(_events([0.01, 0.02]), _index(2), lookback_periods=6)
    assert f["funding_periods_6"].iloc[0] == 2  # only two settlements exist
    assert f["funding_mean_6"].iloc[0] == pytest.approx(0.015)
    assert f["funding_periods_6"].iloc[1] == 2


def test_features_are_causal() -> None:
    """A feature at bar t must not change when future settlements/bars are appended."""
    rates = [0.001 * (i % 7) - 0.002 for i in range(90)]
    idx = _index(30)
    full = build_funding_features(_events(rates), idx, lookback_periods=9)
    for cut_days in (5, 12, 29):
        partial_events = _events(rates)[: (cut_days + 1) * 3]  # only settlements up to that day
        partial = build_funding_features(partial_events, idx[: cut_days + 1], lookback_periods=9)
        assert partial["funding_mean_9"].iloc[-1] == pytest.approx(
            full["funding_mean_9"].iloc[cut_days]
        )
        assert partial["funding_rate_last"].iloc[-1] == pytest.approx(
            full["funding_rate_last"].iloc[cut_days]
        )


def test_settlement_after_bar_close_is_not_visible() -> None:
    """The 00:00 settlement of day t+1 must not leak into bar t."""
    idx = _index(2)
    events = [
        FundingEvent(funding_time=START + timedelta(hours=16), rate=D("0.01"), mark_price=None),
        FundingEvent(funding_time=START + timedelta(days=1), rate=D("-0.99"), mark_price=None),
    ]
    f = build_funding_features(events, idx, lookback_periods=1)
    assert f["funding_rate_last"].iloc[0] == pytest.approx(0.01)  # not −0.99
    assert f["funding_rate_last"].iloc[1] == pytest.approx(-0.99)


def test_empty_history_yields_nan_columns() -> None:
    f = build_funding_features([], _index(3), lookback_periods=(3, 21))
    assert f["funding_mean_3"].isna().all()
    assert (f["funding_periods_21"] == 0).all()


def test_rows_to_events_parses_adapter_payload() -> None:
    rows = [
        {
            "symbol": "BTCUSD_PERP",
            "fundingTime": int(START.timestamp() * 1000) + 7,
            "fundingRate": "0.00010000",
            "markPrice": "",
        },
        {
            "symbol": "BTCUSD_PERP",
            "fundingTime": int((START + timedelta(hours=8)).timestamp() * 1000),
            "fundingRate": "-0.00020000",
            "markPrice": "50000",
        },
    ]
    events = rows_to_events(rows)
    assert [str(e.rate) for e in events] == ["0.00010000", "-0.00020000"]
    assert events[0].funding_time.minute == 0  # jitter normalised
    assert events[0].mark_price is None and events[1].mark_price == D("50000")


def test_attach_features_does_not_mutate_the_input_frame() -> None:
    ds = make_dataset(np.full(10, 30_000.0))
    before = set(ds.frame.columns)
    merged = attach_funding_features(ds.frame, _events([0.0001] * 30), lookback_periods=3)
    assert set(ds.frame.columns) == before
    assert "funding_mean_3" in merged.columns
    assert len(merged) == len(ds.frame)


# --------------------------------------------------------------------------- guard
def _guarded(rates: list[float], days: int, params: FundingGuardParams) -> pd.DataFrame:
    ds = make_dataset(np.full(days, 30_000.0))
    frame = attach_funding_features(
        ds.frame, _events(rates), lookback_periods=params.lookback_periods
    )
    base = FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.8")))
    return FundingGuardStrategy(base, params).compute(frame)


def test_guard_scales_and_caps_only_while_funding_is_adverse() -> None:
    params = FundingGuardParams(
        enabled=True,
        lookback_periods=3,
        min_periods=3,
        threshold_rate=D("0"),
        scale=D("0.5"),
        max_ratio_when_adverse=D("0.45"),
    )
    # day 0 = warm-up (1 day), day 1: positive funding, day 2: negative funding
    out = _guarded([0.001] * 6 + [-0.001] * 3, days=3, params=params)
    assert np.isnan(out["raw_target"].iloc[0])  # warm-up
    assert not bool(out["funding_adverse"].iloc[1])
    assert out["raw_target"].iloc[1] == pytest.approx(0.8)  # untouched
    assert bool(out["funding_adverse"].iloc[2])
    assert out["raw_target"].iloc[2] == pytest.approx(0.4)  # 0.8 × 0.5, below the 0.45 cap
    assert out["base_target"].iloc[2] == pytest.approx(0.8)


def test_guard_cap_binds_when_scaling_is_not_enough() -> None:
    params = FundingGuardParams(
        enabled=True,
        lookback_periods=3,
        min_periods=3,
        scale=D("0.9"),
        max_ratio_when_adverse=D("0.45"),
    )
    out = _guarded([-0.001] * 9, days=3, params=params)
    assert out["raw_target"].iloc[2] == pytest.approx(0.45)  # 0.8 × 0.9 = 0.72 → capped


def test_guard_stays_inactive_before_the_window_is_full() -> None:
    params = FundingGuardParams(enabled=True, lookback_periods=6, min_periods=6, scale=D("0.5"))
    out = _guarded([-0.001] * 3, days=3, params=params)  # only 3 settlements exist
    assert not out["funding_adverse"].any()


def test_guard_warmup_hides_early_targets_and_summary_reports_activity() -> None:
    params = FundingGuardParams(enabled=True, lookback_periods=9, min_periods=9, scale=D("0.5"))
    out = _guarded([-0.001] * 30, days=10, params=params)
    strat = FundingGuardStrategy(FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.8"))), params)
    assert strat.warmup_bars == 3  # ceil(9 settlements / 3 per day)
    assert out["raw_target"].iloc[: strat.warmup_bars].isna().all()
    summary = guard_summary(out)
    assert summary["days_evaluated"] == 10 - strat.warmup_bars
    assert summary["days_guard_active"] == summary["days_evaluated"]
    assert summary["mean_reduction_when_active"] == pytest.approx(0.4)


def test_guard_raises_when_features_are_missing() -> None:
    ds = make_dataset(np.full(5, 30_000.0))
    strat = FundingGuardStrategy(
        FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.5"))), FundingGuardParams(enabled=True)
    )
    with pytest.raises(ValueError, match="funding_mean_21"):
        strat.compute(ds.frame)


def test_build_strategy_wraps_only_when_enabled() -> None:
    off = build_strategy(
        StrategyConfig(name="fixed_hedge", fixed_hedge=FixedHedgeParams(ratio=D("0.5")))
    )  # type: ignore[arg-type]
    assert not isinstance(off, FundingGuardStrategy)
    on = build_strategy(
        StrategyConfig(
            name="fixed_hedge",
            fixed_hedge=FixedHedgeParams(ratio=D("0.5")),
            funding_guard=FundingGuardParams(enabled=True),
        )
    )  # type: ignore[arg-type]
    assert isinstance(on, FundingGuardStrategy)
    assert on.name == "fixed_hedge_50+funding_guard"
    assert on.requires_funding and on.funding_lookbacks == (21,)


def test_guard_params_validation() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FundingGuardParams(lookback_periods=3, min_periods=5)
    with pytest.raises(ValidationError):
        FundingGuardParams(scale=D("1.5"))
