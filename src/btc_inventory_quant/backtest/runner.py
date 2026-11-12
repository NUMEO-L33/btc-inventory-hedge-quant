"""Backtest orchestration: data → strategy + benchmarks → metrics → report bundle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from btc_inventory_quant.backtest.engine import BacktestEngine, BacktestResult
from btc_inventory_quant.backtest.metrics import summarize
from btc_inventory_quant.backtest.reports import StrategyRun, write_bundle
from btc_inventory_quant.config.loader import RunMetadata
from btc_inventory_quant.config.schema import (
    AppConfig,
    BenchmarkSpec,
    CostModelConfig,
    Ema200SimpleParams,
    FixedHedgeParams,
    StrategyConfig,
    StrategyName,
)
from btc_inventory_quant.data.funding_data import FundingCalendar
from btc_inventory_quant.data.market_data import DailyDataset
from btc_inventory_quant.data.repositories import MarketDataRepository
from btc_inventory_quant.domain.inverse_contract import ContractSpec
from btc_inventory_quant.strategy.base import Strategy
from btc_inventory_quant.strategy.benchmarks import build_strategy


def default_benchmarks() -> tuple[BenchmarkSpec, ...]:
    return (
        BenchmarkSpec(name="hodl", strategy=StrategyConfig(name=StrategyName.HODL)),
        BenchmarkSpec(
            name="fixed_hedge_100",
            strategy=StrategyConfig(
                name=StrategyName.FIXED_HEDGE,
                fixed_hedge=FixedHedgeParams(ratio=Decimal("1"), bypass_max_hedge_cap=True),
            ),
        ),
        BenchmarkSpec(
            name="fixed_hedge_50",
            strategy=StrategyConfig(
                name=StrategyName.FIXED_HEDGE, fixed_hedge=FixedHedgeParams(ratio=Decimal("0.5"))
            ),
        ),
        BenchmarkSpec(
            name="ema200_simple",
            strategy=StrategyConfig(
                name=StrategyName.EMA200_SIMPLE, ema200_simple=Ema200SimpleParams()
            ),
        ),
    )


@dataclass(slots=True)
class LoadedMarket:
    dataset: DailyDataset
    funding: FundingCalendar
    spec: ContractSpec
    quality: dict[str, Any]
    settlement_marks: dict[datetime, Decimal] = field(default_factory=dict)


def load_market(config: AppConfig, as_of: datetime | None = None) -> LoadedMarket:
    repo = MarketDataRepository(config.data, as_of=as_of)
    spec = repo.contract_spec()
    dataset = repo.daily()
    funding, funding_report = repo.funding()
    settlement_marks = repo.settlement_marks()
    covered = sum(1 for e in funding.events if e.funding_time in settlement_marks)
    quality = {
        "market": {k: v.to_dict() for k, v in dataset.quality.items()},
        "funding": funding_report.to_dict(),
        "settlement_marks": {
            "available": len(settlement_marks),
            "funding_events_covered": covered,
            "source": "mark_price_klines_8h" if settlement_marks else "daily_kline_approximation",
        },
        "notes": dataset.frame.attrs.get("notes", []),
        "contract_spec_source": spec.source,
    }
    return LoadedMarket(
        dataset=dataset,
        funding=funding,
        spec=spec,
        quality=quality,
        settlement_marks=settlement_marks,
    )


def _to_dt(d: Any) -> datetime | None:
    if d is None:
        return None
    return datetime.combine(d, time(0, 0), tzinfo=UTC)


def run_strategy(
    market: LoadedMarket,
    strategy: Strategy,
    config: AppConfig,
    cost: CostModelConfig | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    initial_btc: Decimal | None = None,
    seed: int | None = None,
) -> StrategyRun:
    bt = config.backtest
    engine = BacktestEngine(
        dataset=market.dataset,
        funding_calendar=market.funding,
        spec=market.spec,
        strategy=strategy,
        limits=config.risk,
        cost=cost or bt.cost,
        initial_btc=initial_btc or bt.initial_btc,
        seed=bt.seed if seed is None else seed,
        start=start if start is not None else _to_dt(bt.start),
        end=end if end is not None else _to_dt(bt.end),
        warmup_bars=bt.warmup_bars,
        settlement_marks=market.settlement_marks,
    )
    result: BacktestResult = engine.run()
    summary, frames = summarize(result, initial_btc or bt.initial_btc)
    return StrategyRun(result=result, summary=summary, frames=frames)


def run_backtest(
    config: AppConfig,
    metadata: RunMetadata,
    out_dir: Path | None = None,
    sensitivity: pd.DataFrame | None = None,
    as_of: datetime | None = None,
    market: LoadedMarket | None = None,
) -> tuple[Path, StrategyRun, list[StrategyRun]]:
    market = market or load_market(config, as_of=as_of)
    main = run_strategy(market, build_strategy(config.strategy), config)
    benchmark_specs = config.backtest.benchmarks or default_benchmarks()
    benchmarks: list[StrategyRun] = []
    for b in benchmark_specs:
        if b.strategy.name == config.strategy.name and b.strategy == config.strategy:
            continue
        benchmarks.append(run_strategy(market, build_strategy(b.strategy), config))
    out = out_dir or (config.backtest.report_dir / metadata.run_id)
    write_bundle(
        out,
        main,
        benchmarks,
        metadata.to_dict(),
        config.model_dump(mode="json"),
        market.quality,
        sensitivity=sensitivity,
        notes=[
            "The funding notional formula (contracts × contract value ÷ mark price) is PARTIAL because no COIN-M-specific source text for it was found in the official documentation; it is to be back-verified against measured live Shadow/Testnet data.",
            "Slippage and partial fills are modelled values; actual execution quality needs to be recalibrated with live trading data.",
        ],
        make_charts=config.backtest.make_charts,
    )
    return out, main, benchmarks
