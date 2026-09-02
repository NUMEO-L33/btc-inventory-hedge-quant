"""Sensitivity / stress scenarios (prompt §4): fees ×2, slippage ×3, adverse funding,
signal delayed by one day, one rebalance order missed, partial fills."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from btc_inventory_quant.backtest.runner import LoadedMarket, load_market, run_strategy
from btc_inventory_quant.config.schema import AppConfig, CostModelConfig
from btc_inventory_quant.strategy.benchmarks import build_strategy

SCENARIO_COLUMNS = [
    "scenario",
    "final_btc",
    "net_btc_growth",
    "cagr_btc",
    "final_usd",
    "cagr_usd",
    "excess_vs_hodl_usd_pct",
    "max_drawdown_usd",
    "max_drawdown_btc",
    "total_fees_btc",
    "total_slippage_cost_btc",
    "total_funding_btc",
    "total_trades",
    "missed_rebalances",
    "partial_fills",
    "verdict",
]


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    cost: CostModelConfig


def build_scenarios(base: CostModelConfig) -> list[Scenario]:
    return [
        Scenario("base", base),
        Scenario("fees_x2", replace_cost(base, fee_multiplier=base.fee_multiplier * 2)),
        Scenario(
            "slippage_x3", replace_cost(base, slippage_multiplier=base.slippage_multiplier * 3)
        ),
        Scenario(
            "adverse_funding_-1bp_per_8h",
            replace_cost(
                base,
                funding_adverse_bps_per_period=base.funding_adverse_bps_per_period + Decimal("1"),
            ),
        ),
        Scenario(
            "adverse_funding_-3bp_per_8h",
            replace_cost(
                base,
                funding_adverse_bps_per_period=base.funding_adverse_bps_per_period + Decimal("3"),
            ),
        ),
        Scenario(
            "signal_delay_1d", replace_cost(base, signal_delay_days=base.signal_delay_days + 1)
        ),
        Scenario("missed_rebalance_#1", replace_cost(base, missed_rebalance_indices=(1,))),
        Scenario("missed_rebalance_#5", replace_cost(base, missed_rebalance_indices=(5,))),
        Scenario("missed_every_10th", replace_cost(base, missed_rebalance_every_n=10)),
        Scenario(
            "partial_fills_50pct_p0.5",
            replace_cost(
                base, partial_fill_probability=Decimal("0.5"), partial_fill_ratio=Decimal("0.5")
            ),
        ),
        Scenario(
            "fees_x2_slip_x3_delay_1d",
            replace_cost(
                base,
                fee_multiplier=base.fee_multiplier * 2,
                slippage_multiplier=base.slippage_multiplier * 3,
                signal_delay_days=base.signal_delay_days + 1,
            ),
        ),
        Scenario("maker_fills", replace_cost(base, use_maker=True)),
    ]


def replace_cost(base: CostModelConfig, **changes: Any) -> CostModelConfig:
    return base.model_copy(update=changes)


def run_sensitivity(
    config: AppConfig,
    as_of: datetime | None = None,
    market: LoadedMarket | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    market = market or load_market(config, as_of=as_of)
    rows: list[dict[str, Any]] = []
    for sc in build_scenarios(config.backtest.cost):
        run = run_strategy(
            market, build_strategy(config.strategy), config, cost=sc.cost, start=start, end=end
        )
        v = run.summary.to_dict()
        rows.append(
            {"scenario": sc.name, **{c: v.get(c) for c in SCENARIO_COLUMNS if c != "scenario"}}
        )
    return pd.DataFrame(rows, columns=SCENARIO_COLUMNS)


__all__ = ["Scenario", "build_scenarios", "replace", "run_sensitivity"]
