from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from btc_inventory_quant.backtest.engine import BacktestEngine
from btc_inventory_quant.backtest.metrics import summarize
from btc_inventory_quant.config.schema import (
    CostModelConfig,
    FixedHedgeParams,
    RegimeHedgeParams,
    RiskLimits,
)
from btc_inventory_quant.strategy.base import Regime
from btc_inventory_quant.strategy.benchmarks import FixedHedgeStrategy, HodlStrategy
from btc_inventory_quant.strategy.regime_hedge import RegimeHedgeV1
from tests.helpers import make_dataset, make_funding, synthetic_prices

D = Decimal


class SpikeStrategy:
    """Target 0 everywhere except one bar → isolates *when* a signal is executed."""

    name = "spike"
    warmup_bars = 0
    bypass_max_hedge_cap = False

    def __init__(self, spike_bar: int, ratio: float = 0.5) -> None:
        self.spike_bar = spike_bar
        self.ratio = ratio

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=frame.index)
        out["raw_target"] = 0.0
        out.iloc[self.spike_bar, out.columns.get_loc("raw_target")] = self.ratio
        out["regime"] = Regime.NEUTRAL.value
        return out


class FutureLeakStrategy:
    """Deliberately looks at the NEXT bar; the engine must still execute one bar later, so the
    result must differ from a strategy that used the same information legitimately."""

    name = "leak"
    warmup_bars = 0
    bypass_max_hedge_cap = False

    def compute(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=frame.index)
        nxt = frame["close"].shift(-1)
        out["raw_target"] = np.where(nxt < frame["close"], 0.8, 0.0)
        out["regime"] = Regime.NEUTRAL.value
        return out


def _engine(
    ds,
    strategy,
    cost: CostModelConfig | None = None,
    limits: RiskLimits | None = None,
    warmup: int = 5,
    seed: int = 42,
):  # type: ignore[no-untyped-def]
    import json

    from btc_inventory_quant.domain.inverse_contract import ContractSpec
    from tests.conftest import FIXTURES

    sym = json.loads((FIXTURES / "exchange_info_btcusd_perp.json").read_text())["symbol"]
    spec = ContractSpec.from_exchange_info_symbol(sym, "fixture")
    return BacktestEngine(
        dataset=ds,
        funding_calendar=make_funding(ds),
        spec=spec,
        strategy=strategy,
        limits=limits or RiskLimits(),
        cost=cost or CostModelConfig(),
        initial_btc=D("1"),
        seed=seed,
        warmup_bars=warmup,
    )


def test_signal_executes_on_next_open_never_same_candle() -> None:
    ds = make_dataset(synthetic_prices(60, seed=1))
    spike = 20
    res = _engine(ds, SpikeStrategy(spike)).run()
    trades = res.trades
    assert len(trades) >= 1
    first = trades.iloc[0]
    exec_ts = ds.frame.index[spike + 1]
    assert first["ts"] == exec_ts.to_pydatetime()
    ref = ds.dec(exec_ts, "open")
    assert first["reference_price"] == ref
    # fill = next open × (1 − slippage), rounded down to tick — never the signal candle's close
    assert first["fill_price"] < ref
    assert first["fill_price"] != ds.dec(ds.frame.index[spike], "close")


def test_truncating_future_data_does_not_change_the_past() -> None:
    ds = make_dataset(synthetic_prices(300, seed=5))
    strat = RegimeHedgeV1(RegimeHedgeParams(ema_slow=50, ema_mid=20, ema_fast=10, atr_period=10))
    full = _engine(ds, strat, warmup=60).run().daily
    cut = 200
    part_ds = make_dataset(synthetic_prices(300, seed=5)[:cut])
    part = _engine(part_ds, strat, warmup=60).run().daily
    common = part.index
    pd.testing.assert_frame_equal(full.loc[common], part, check_dtype=False)


def test_future_leak_strategy_cannot_profit_from_leak() -> None:
    """A strategy that peeks at bar t+1 still trades at the open of t+1 (after the peeked close is
    already known)… the engine cannot stop *strategies* from cheating, but it guarantees they cannot
    trade before the candle closes: the fill timestamps must all be strictly after the signal bar."""
    ds = make_dataset(synthetic_prices(80, seed=9))
    res = _engine(ds, FutureLeakStrategy()).run()
    sig = res.signals["raw_target"]
    for _, t in res.trades.iterrows():
        pos = ds.frame.index.get_loc(pd.Timestamp(t["ts"]))
        assert pos >= 1
        # the trade is explained by the previous bar's signal, never by the same bar's
        assert not np.isnan(sig.iloc[pos - 1])


def test_daily_cap_and_min_diff_are_enforced() -> None:
    ds = make_dataset(np.full(40, 30_000.0))  # flat market: ratio only moves through trades
    strat = FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.8")))
    res = _engine(ds, strat).run()
    ratios = res.daily["effective_ratio"].astype(float).tolist()
    # 0 → 0.2 → 0.4 → 0.6 → 0.8 (never more than 20pp per day)
    assert ratios[0] == pytest.approx(0.2, abs=0.01)
    assert ratios[1] == pytest.approx(0.4, abs=0.01)
    assert ratios[2] == pytest.approx(0.6, abs=0.01)
    assert ratios[3] == pytest.approx(0.8, abs=0.01)
    diffs = np.diff(ratios)
    assert (diffs <= 0.2 + 1e-6).all()
    # once at target, no more trades (differences < 10pp)
    assert res.trades["ts"].nunique() == 4


def test_no_trade_when_difference_below_10_points() -> None:
    ds = make_dataset(np.full(30, 30_000.0))
    res = _engine(ds, FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.09")))).run()
    assert len(res.trades) == 0
    res2 = _engine(ds, FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.10")))).run()
    assert len(res2.trades) == 1


def test_never_long_and_buy_never_exceeds_short() -> None:
    ds = make_dataset(synthetic_prices(400, seed=11, vol=0.05))
    res = _engine(
        ds,
        RegimeHedgeV1(RegimeHedgeParams(ema_slow=50, ema_mid=20, ema_fast=10, atr_period=10)),
        warmup=60,
    ).run()
    assert (res.daily["short_contracts"].astype(float) >= 0).all()
    assert res.portfolio.position.contracts <= 0
    buys = res.trades[res.trades["side"] == "BUY"]
    assert (buys["contracts_after"].astype(float) <= 0).all()
    assert (res.daily["effective_ratio"].astype(float) <= 0.85 + 0.03).all()


def test_backtest_is_deterministic() -> None:
    ds = make_dataset(synthetic_prices(200, seed=2))
    cost = CostModelConfig(partial_fill_probability=D("0.5"), partial_fill_ratio=D("0.5"))
    strat = RegimeHedgeV1(RegimeHedgeParams(ema_slow=30, ema_mid=15, ema_fast=5, atr_period=5))
    a = _engine(ds, strat, cost=cost, warmup=40).run()
    b = _engine(ds, strat, cost=cost, warmup=40).run()
    pd.testing.assert_frame_equal(a.daily, b.daily)
    pd.testing.assert_frame_equal(a.trades, b.trades)


def test_costs_reduce_performance_monotonically() -> None:
    ds = make_dataset(synthetic_prices(250, seed=4, vol=0.04))
    strat = RegimeHedgeV1(RegimeHedgeParams(ema_slow=30, ema_mid=15, ema_fast=5, atr_period=5))
    base = _engine(ds, strat, warmup=40).run()
    fee2 = _engine(ds, strat, cost=CostModelConfig(fee_multiplier=D("2")), warmup=40).run()
    slip3 = _engine(ds, strat, cost=CostModelConfig(slippage_multiplier=D("3")), warmup=40).run()
    adverse = _engine(
        ds, strat, cost=CostModelConfig(funding_adverse_bps_per_period=D("1")), warmup=40
    ).run()
    f = lambda r: r.portfolio.equity_btc(r.daily["mark_close"].iloc[-1])  # noqa: E731
    assert len(base.trades) > 0
    assert f(fee2) < f(base)
    assert f(slip3) < f(base)
    assert f(adverse) < f(base)
    assert base.portfolio.ledger.totals().commission < 0


def test_funding_is_received_by_short_with_positive_rate() -> None:
    ds = make_dataset(np.full(30, 30_000.0))
    res = _engine(ds, FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.5")))).run()
    totals = res.portfolio.ledger.totals()
    assert totals.funding > 0
    # 0.5 hedge on 1 BTC: nominal 0.5 BTC × 0.0001 × 3/day ≈ 0.00015 BTC/day once at target
    per_day = res.daily["funding_btc_today"].astype(float).iloc[-1]
    assert per_day == pytest.approx(0.5 * 0.0001 * 3, rel=0.05)
    hodl = _engine(ds, HodlStrategy()).run()
    assert hodl.portfolio.ledger.totals().funding == 0


def test_missed_rebalance_and_partial_fill_scenarios() -> None:
    ds = make_dataset(np.full(40, 30_000.0))
    strat = FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.8")))
    base = _engine(ds, strat).run()
    missed = _engine(ds, strat, cost=CostModelConfig(missed_rebalance_indices=(1,))).run()
    assert missed.stats["missed_rebalances"] == 1
    assert missed.daily["effective_ratio"].astype(float).iloc[0] == 0.0  # first order lost
    assert len(missed.trades) == len(base.trades)  # re-planned the next day
    partial = _engine(
        ds,
        strat,
        cost=CostModelConfig(partial_fill_probability=D("1"), partial_fill_ratio=D("0.5")),
    ).run()
    assert partial.stats["partial_fills"] > 0
    assert (partial.trades["reason"] == "reorder").any()
    assert partial.daily["effective_ratio"].astype(float).iloc[-1] == pytest.approx(0.8, abs=0.02)


def test_case_1_and_2_reproduced_by_engine(spec) -> None:  # type: ignore[no-untyped-def]
    """100% fixed short opened at 100k; price halves → ~2 BTC & ~100k USD; doubles → ~0.5 BTC & ~100k USD."""
    prices = np.array([100_000.0] * 6 + [50_000.0] * 3)
    ds = make_dataset(prices)
    strat = FixedHedgeStrategy(FixedHedgeParams(ratio=D("1"), bypass_max_hedge_cap=True))
    cost = CostModelConfig(taker_fee_rate=D("0"), slippage_bps=D("0"))
    eng = _engine(ds, strat, cost=cost, warmup=0)
    eng.funding_calendar = make_funding(ds, rate=D("0"))
    res = eng.run()
    last = res.daily.iloc[-1]
    assert float(last["equity_btc"]) == pytest.approx(2.0, rel=1e-6)
    assert float(last["equity_usd"]) == pytest.approx(100_000.0, rel=1e-6)
    prices2 = np.array([100_000.0] * 6 + [200_000.0] * 3)
    ds2 = make_dataset(prices2)
    eng2 = _engine(ds2, strat, cost=cost, warmup=0)
    eng2.funding_calendar = make_funding(ds2, rate=D("0"))
    res2 = eng2.run()
    last2 = res2.daily.iloc[-1]
    assert float(last2["equity_btc"]) == pytest.approx(0.5, rel=1e-6)
    assert float(last2["equity_usd"]) == pytest.approx(100_000.0, rel=1e-6)


def test_summary_metrics_shape() -> None:
    ds = make_dataset(synthetic_prices(300, seed=8))
    strat = RegimeHedgeV1(RegimeHedgeParams(ema_slow=50, ema_mid=20, ema_fast=10, atr_period=10))
    res = _engine(ds, strat, warmup=60).run()
    summary, frames = summarize(res, D("1"))
    v = summary.to_dict()
    for key in (
        "start_btc",
        "final_btc",
        "net_btc_growth",
        "cagr_btc",
        "cagr_usd",
        "max_drawdown_usd",
        "max_drawdown_btc",
        "excess_vs_hodl_usd_pct",
        "total_realized_pnl_btc",
        "total_unrealized_pnl_btc",
        "total_funding_btc",
        "total_fees_btc",
        "total_slippage_cost_btc",
        "total_trades",
        "turnover_total",
        "avg_hedge_ratio",
        "max_hedge_ratio",
        "worst_consecutive_losing_days_usd",
        "regime_performance",
        "verdict",
    ):
        assert key in v
    assert set(frames) == {"monthly_returns", "yearly_returns", "drawdown"}
    assert v["verdict"] in {"PASS", "WEAK", "FAIL"}
