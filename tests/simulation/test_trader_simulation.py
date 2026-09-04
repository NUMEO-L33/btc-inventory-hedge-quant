"""End-to-end trader loop on a replayed market with the paper exchange: consistency with the
backtest engine, restart recovery, SAFE_HOLD triggers, live gate."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from btc_inventory_quant.backtest.engine import BacktestEngine
from btc_inventory_quant.config.schema import AppConfig, CostModelConfig, RegimeHedgeParams, RunMode
from btc_inventory_quant.config.settings import Settings
from btc_inventory_quant.domain.inverse_contract import OrderSide
from btc_inventory_quant.exchange.base import ExchangeError
from btc_inventory_quant.exchange.paper import PaperExchange, ReplayMarketData
from btc_inventory_quant.execution.order_manager import OrderManager
from btc_inventory_quant.execution.policies import MarketExecution
from btc_inventory_quant.execution.reconciliation import Reconciler
from btc_inventory_quant.monitoring.alerts import Alerter, LogChannel
from btc_inventory_quant.monitoring.metrics import MetricsRegistry
from btc_inventory_quant.risk.guards import Guards
from btc_inventory_quant.risk.kill_switch import KillSwitch, SystemState
from btc_inventory_quant.runtime.live_gate import RuntimeEvidence, evaluate_live_gate
from btc_inventory_quant.runtime.trader import Trader
from btc_inventory_quant.storage.database import Database
from btc_inventory_quant.strategy.regime_hedge import RegimeHedgeV1
from tests.helpers import make_dataset, make_funding, synthetic_prices

D = Decimal
PARAMS = RegimeHedgeParams(ema_slow=50, ema_mid=20, ema_fast=10, atr_period=10)
WARMUP = 60


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "mode": "shadow",
            "strategy": {"name": "regime_hedge_v1", "regime_hedge": PARAMS.model_dump()},
            "backtest": {
                "warmup_bars": WARMUP,
                "cost": {"taker_fee_rate": "0.0005", "slippage_bps": "5"},
            },
            "exchange": {"name": "paper", "paper_initial_btc": "1"},
            "execution": {"policy": "market"},
        }
    )


class Harness:
    def __init__(
        self, tmp_path: Path, n_days: int = 160, seed: int = 5, db_name: str = "sim.sqlite3"
    ) -> None:
        self.ds = make_dataset(synthetic_prices(n_days, seed=seed, vol=0.04))
        self.config = _config()
        self.db = Database(f"sqlite:///{tmp_path}/{db_name}")
        self.db.create_all()
        self.tmp_path = tmp_path
        self.new_exchange()

    def new_exchange(self) -> None:
        import json

        from btc_inventory_quant.domain.inverse_contract import ContractSpec
        from tests.conftest import FIXTURES

        sym = json.loads((FIXTURES / "exchange_info_btcusd_perp.json").read_text())["symbol"]
        self.spec = ContractSpec.from_exchange_info_symbol(sym, "fixture")
        self.funding = make_funding(self.ds, rate=D("0.0001"), with_mark=True)
        self.market = ReplayMarketData(self.ds, self.spec, funding=self.funding)
        self.exchange = PaperExchange(
            self.market, self.spec, initial_btc=D("1"), taker_fee=D("0.0005"), slippage_bps=D("5")
        )

    def build_trader(self, orders_allowed: bool = True) -> Trader:
        alerter = Alerter([LogChannel()], async_send=False)
        ks = KillSwitch(self.db, alerter)
        om = OrderManager(
            self.exchange,
            self.db,
            self.spec,
            self.config.risk,
            "BTCUSD_PERP",
            "shadow",
            sleep=lambda s: None,
            poll_interval_seconds=0,
        )
        rec = Reconciler(self.exchange, self.db, self.spec, "BTCUSD_PERP", self.config.risk)
        guards = Guards(self.config.risk, self.spec, "BTCUSD_PERP")
        metrics = MetricsRegistry(self.db, file_path=self.tmp_path / "metrics.json")
        trader = Trader(
            self.config,
            self.exchange,
            self.db,
            self.spec,
            RegimeHedgeV1(PARAMS),
            alerter,
            ks,
            om,
            MarketExecution(om),
            rec,
            guards,
            metrics,
            orders_allowed=orders_allowed,
            clock=lambda: self.market.now,
            sleep=lambda s: None,
        )
        self.trader = trader
        self.alerter = alerter
        self.ks = ks
        return trader

    def run_days(self, start_idx: int, n: int) -> list[Any]:
        results = []
        self.market.cursor = start_idx
        for _ in range(n):
            results.append(self.trader.cycle())
            self.market.advance()
        return results


def test_trader_matches_backtest_engine_on_replayed_data(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    start_idx = WARMUP + 1
    n = 60
    results = h.run_days(start_idx, n)
    actions = [r.action for r in results]
    assert "rebalance" in actions
    assert all(a in ("rebalance", "none", "skipped") for a in actions), actions
    # reference: the backtest engine with the SAME funding calendar the paper exchange settles
    eng = BacktestEngine(
        dataset=h.ds,
        funding_calendar=h.funding,
        spec=h.spec,
        strategy=RegimeHedgeV1(PARAMS),
        limits=h.config.risk,
        cost=CostModelConfig(taker_fee_rate=D("0.0005"), slippage_bps=D("5")),
        initial_btc=D("1"),
        warmup_bars=WARMUP,
    )
    bt = eng.run()
    bt_days = bt.daily.iloc[:n]
    live_ratio = np.array([float(r.effective_ratio) for r in results])
    # the trader's ratio is measured at the open (before the day's trade); compare with the engine's
    # current_ratio_at_open column which is the same quantity
    engine_ratio = bt_days["current_ratio_at_open"].astype(float).to_numpy()
    assert np.allclose(live_ratio, engine_ratio, atol=1e-6)
    last = bt_days.iloc[-1]
    assert h.exchange.position.contracts == -Decimal(str(last["short_contracts"]))
    # wallets differ only by the funding events after the last replayed decision (08:00/16:00 of the
    # final day are settled by the engine but not yet by the paper account at 00:05)
    engine_wallet = Decimal(str(last["wallet_btc"]))
    pending_events = [
        e for e in h.funding.events if e.day == bt_days.index[-1].date() and e.hour != 0
    ]
    pending = sum(
        (
            -h.exchange.position.contracts
            * h.spec.contract_value_usd
            / (e.mark_price or D(1))
            * e.rate
            for e in pending_events
        ),
        D("0"),
    )
    assert abs(h.exchange.wallet_balance + pending - engine_wallet) < D("1e-9")
    assert sum(1 for r in h.exchange.income_records if r.income_type == "FUNDING_FEE") > 0


def test_restart_recovers_position_and_continues(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 15)
    assert h.exchange.position.contracts < 0
    contracts_before = h.exchange.position.contracts
    trader.stop()
    # new process: same DB, same exchange account
    trader2 = h.build_trader()
    report = trader2.start()
    assert report["portfolio"] == "reconciled"
    assert not h.ks.engaged
    assert trader2.portfolio is not None
    assert trader2.portfolio.position.contracts == contracts_before
    res = h.run_days(h.market.cursor, 5)
    assert all(r.action != "safe_hold" for r in res)


def test_position_mismatch_engages_safe_hold_and_keeps_position(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 10)
    # someone trades manually on the account: exchange position no longer matches the DB
    manual_contracts = h.exchange.position.contracts - D("50")
    h.exchange.force_position(manual_contracts, D("30000"))
    res = trader.cycle()
    assert res.action == "safe_hold"
    assert h.ks.engaged
    assert h.exchange.position.contracts == manual_contracts  # position kept, not force-closed
    orders_before = len(h.exchange.orders)
    h.market.advance()
    res2 = trader.cycle()
    assert res2.action == "safe_hold"
    assert len(h.exchange.orders) == orders_before  # no new orders while in SAFE_HOLD
    # a fresh trader instance still sees SAFE_HOLD (persisted)
    trader3 = h.build_trader()
    assert trader3.kill_switch.status().state is SystemState.SAFE_HOLD


def test_unknown_order_triggers_safe_hold_and_cancels_open_orders(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 3)
    h.exchange.inject_unknown_order()
    res = trader.cycle()
    assert res.action == "safe_hold"
    assert h.exchange.open_orders("BTCUSD_PERP") == []  # cancelled, position untouched


def test_stale_mark_price_blocks_trading(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 3)
    # clock jumps 1 hour ahead of the (replayed) mark timestamp → stale
    trader.clock = lambda: h.market.now + timedelta(hours=1)
    res = trader.cycle()
    assert res.action == "safe_hold"
    assert any("STALE" in g.code for g in res.guard_failures)


def test_unexpected_deposit_is_flagged_and_not_counted_as_pnl(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 3)
    h.exchange.external_transfer(D("0.5"))
    res = trader.cycle()
    assert res.action == "safe_hold"
    assert trader.portfolio is not None
    totals = trader.portfolio.ledger.totals()
    assert totals.deposits == D("0.5")
    assert (
        totals.trading_pnl
        == totals.realized_pnl
        + totals.commission
        + totals.funding
        + totals.insurance_clear
        + totals.adjustments
    )
    assert trader.portfolio.ledger.net_btc_growth(trader.portfolio.wallet_balance_btc) < D("0.5")


def test_orders_disabled_logs_intent_but_sends_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    trader = h.build_trader(orders_allowed=False)
    trader.start()
    res = h.run_days(WARMUP + 1, 20)
    assert any(r.action == "blocked" for r in res)
    assert len(h.exchange.orders) == 0
    assert h.exchange.position.contracts == 0


def test_never_long_and_cap_respected_over_long_replay(tmp_path: Path) -> None:
    h = Harness(tmp_path, n_days=220, seed=9)
    trader = h.build_trader()
    trader.start()
    res = h.run_days(WARMUP + 1, 150)
    assert all(r.action != "safe_hold" for r in res)
    snaps = [r for r in res if r.effective_ratio is not None]
    assert max(float(r.effective_ratio) for r in snaps) <= 0.85 + 0.03
    assert h.exchange.position.contracts <= 0
    for o in h.exchange.orders.values():
        if o.request.side is OrderSide.BUY:
            assert o.request.reduce_only


def test_live_gate_requires_every_condition() -> None:
    config = AppConfig.model_validate(
        {
            "mode": "live",
            "exchange": {"name": "binance_coinm", "environment": "production"},
            "live": {
                "live_trading_enabled": True,
                "acknowledgement": "I_ACCEPT_COIN_M_TRADING_RISK",
            },
        }
    )
    good = RuntimeEvidence(True, True, True, 100, True, True, True, True, True, True, D("0.01"))
    settings = Settings(
        enable_live_trading=True,
        live_acknowledgement="I_ACCEPT_COIN_M_TRADING_RISK",
        _env_file=None,
    )  # type: ignore[call-arg]
    assert evaluate_live_gate(config, settings, True, good).passed
    assert not evaluate_live_gate(config, settings, False, good).passed  # no --confirm-live
    assert not evaluate_live_gate(
        config,
        Settings(
            enable_live_trading=False,
            live_acknowledgement="I_ACCEPT_COIN_M_TRADING_RISK",
            _env_file=None,
        ),
        True,
        good,
    ).passed  # type: ignore[call-arg]
    assert not evaluate_live_gate(
        config,
        Settings(enable_live_trading=True, live_acknowledgement="wrong", _env_file=None),
        True,
        good,
    ).passed  # type: ignore[call-arg]
    for field_name in (
        "api_key_present",
        "api_permissions_ok",
        "time_synced",
        "position_consistent",
        "no_unconfirmed_orders",
        "hedge_ratio_within_limits",
        "market_data_fresh",
        "contract_spec_verified",
        "position_mode_one_way",
    ):
        bad = replace(good, **{field_name: False})
        assert not evaluate_live_gate(config, settings, True, bad).passed, field_name
    big = replace(good, equity_btc=D("1"))
    assert not evaluate_live_gate(config, settings, True, big).passed
    default_cfg = AppConfig.model_validate(
        {"mode": "live", "exchange": {"name": "binance_coinm", "environment": "production"}}
    )
    assert not evaluate_live_gate(
        default_cfg, settings, True, good
    ).passed  # live_trading_enabled defaults to false


def test_shadow_mode_can_never_place_real_orders() -> None:
    assert RunMode.SHADOW.value == "shadow"
    from btc_inventory_quant.runtime.trader import mode_allows_orders

    assert mode_allows_orders(RunMode.SHADOW)  # paper exchange only
    assert not mode_allows_orders(RunMode.LIVE)  # live requires the gate
    assert not mode_allows_orders(RunMode.BACKTEST)


def test_run_once_smoke(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    trader = h.build_trader()
    h.market.cursor = WARMUP + 1
    assert trader.run(once=True) == 0
    assert (tmp_path / "metrics.json").exists()
    assert h.db.latest_decision() is not None


def test_clock_utc(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.market.now.tzinfo is UTC
    assert isinstance(h.market.now, datetime)


def test_run_acts_on_latest_candle_immediately_at_startup(tmp_path: Path) -> None:
    """A process started after the daily decision time must not skip that day's candle."""
    h = Harness(tmp_path)
    trader = h.build_trader()
    h.market.cursor = WARMUP + 1
    # the replay clock is 00:05 UTC == decision time; run(max_cycles=1) must decide NOW, not tomorrow
    assert trader.run(max_cycles=1) == 0
    decision = h.db.latest_decision()
    assert decision is not None
    assert decision.candle_time == h.ds.frame.index[WARMUP].to_pydatetime()


def test_shadow_funding_is_settled_once_per_event(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 5)
    trader.cycle()  # first cycle of the current replay day settles its events
    n_before = sum(1 for r in h.exchange.income_records if r.income_type == "FUNDING_FEE")
    assert n_before > 0
    # re-running a cycle on the same day must not settle the same events twice
    trader.cycle()
    n_after = sum(1 for r in h.exchange.income_records if r.income_type == "FUNDING_FEE")
    assert n_after == n_before
    assert trader.portfolio is not None
    assert trader.portfolio.ledger.totals().funding > 0  # income sync mirrored it into the ledger


def test_trader_runs_the_funding_guard_with_the_same_features_as_the_backtest(
    tmp_path: Path,
) -> None:
    """Live path parity: the trader must build the same causal funding features the engine uses."""
    from btc_inventory_quant.backtest.engine import BacktestEngine as _Engine
    from btc_inventory_quant.config.schema import FundingGuardParams
    from btc_inventory_quant.data.funding_features import attach_funding_features
    from btc_inventory_quant.strategy.funding_guard import FundingGuardStrategy

    guard = FundingGuardParams(enabled=True, lookback_periods=9, min_periods=9, scale=D("0.5"))
    h = Harness(tmp_path, db_name="guard.sqlite3")
    strategy = FundingGuardStrategy(RegimeHedgeV1(PARAMS), guard)
    trader = h.build_trader()
    trader.strategy = strategy
    trader.start()
    results = h.run_days(WARMUP + 1, 25)
    assert all(r.action in ("rebalance", "none", "skipped") for r in results)
    assert h.exchange.position.contracts <= 0

    # the engine, given the same funding calendar, must see identical guard inputs
    frame = attach_funding_features(h.ds.frame, h.funding.events, (guard.lookback_periods,))
    engine_signals = strategy.compute(frame)
    live_frame = trader.completed_candles(h.market.now)
    assert f"funding_mean_{guard.lookback_periods}" in live_frame.columns
    live_signals = strategy.compute(live_frame)
    common = live_signals.index.intersection(engine_signals.index)
    assert len(common) > WARMUP
    np.testing.assert_allclose(
        live_signals.loc[common, "funding_mean"].to_numpy(dtype=float),
        engine_signals.loc[common, "funding_mean"].to_numpy(dtype=float),
        rtol=1e-12,
    )
    eng = _Engine(
        dataset=h.ds,
        funding_calendar=h.funding,
        spec=h.spec,
        strategy=strategy,
        limits=h.config.risk,
        cost=CostModelConfig(taker_fee_rate=D("0.0005"), slippage_bps=D("5")),
        initial_btc=D("1"),
        warmup_bars=WARMUP,
    ).run()
    live_ratio = np.array([float(r.effective_ratio) for r in results])
    engine_ratio = eng.daily["current_ratio_at_open"].astype(float).to_numpy()[: len(results)]
    np.testing.assert_allclose(live_ratio, engine_ratio, atol=1e-6)


def test_startup_report_states_live_fees_against_the_backtest_assumption(tmp_path: Path) -> None:
    """Fees drove 92% of the V4 'edge', so a drift from the modelled rate must be visible at start."""
    from btc_inventory_quant.exchange.models import CommissionRates

    harness = Harness(tmp_path)
    trader = harness.build_trader()
    exchange = harness.exchange

    # the account charges 20% less than the backtest assumed
    exchange.commission_rates = lambda symbol: CommissionRates(  # type: ignore[assignment,method-assign]
        symbol=symbol, maker=D("0.00016"), taker=D("0.0004")
    )
    report = trader._commission_report()
    assert report["assumed_taker"] == "0.0005"
    assert report["actual_taker"] == "0.0004"
    assert report["taker_drift_pct"] == "-20.00"
    assert report["backtest_assumption_ok"] is False
    assert report["direction"] == "conservative"

    # matching rates pass
    exchange.commission_rates = lambda symbol: CommissionRates(  # type: ignore[assignment,method-assign]
        symbol=symbol, maker=D("0.0002"), taker=D("0.0005")
    )
    assert trader._commission_report()["backtest_assumption_ok"] is True

    # an exchange that cannot report them must not break startup
    def _boom(symbol: str) -> CommissionRates:
        raise ExchangeError("not supported here")

    exchange.commission_rates = _boom  # type: ignore[assignment,method-assign]
    degraded = trader._commission_report()
    assert degraded["actual"] is None
    assert "not supported" in degraded["error"]


def test_metrics_report_the_hedge_ratio_after_the_fill_not_before(tmp_path: Path) -> None:
    """Observed 2026-09-04 in shadow: 162 contracts held, effective_hedge_ratio reported as 0.

    The cycle computes current_ratio BEFORE deciding (the decision record wants it that way), and
    that pre-trade value was being handed straight to the metrics. A rebalance from flat therefore
    published ratio 0 and error -target while fully hedged — on a dashboard watched for weeks that
    reads as a permanent failure.
    """
    harness = Harness(tmp_path)
    trader = harness.build_trader()
    trader.start()

    # exactly ONE cycle, from flat: this is the only cycle where the bug shows, because from the
    # second cycle onward the pre-trade ratio is already non-zero and hides the staleness
    (result,) = harness.run_days(WARMUP + 1, 1)
    assert result.action == "rebalance"

    m = trader.metrics.current
    assert m.short_contracts is not None and m.effective_hedge_ratio is not None
    contracts = D(str(m.short_contracts))
    ratio = D(str(m.effective_hedge_ratio))
    assert contracts > 0  # we are holding a short
    assert ratio > D("0.01")  # NOT the pre-trade zero

    # and it must agree with the position it just took
    expected = (
        abs(contracts)
        * harness.spec.contract_value_usd
        / (D(str(m.equity_btc)) * D(str(m.mark_price)))
    )
    assert abs(ratio - expected) < D("1e-9")
    assert m.hedge_ratio_error is not None
    # within the min-rebalance band, not the -target a stale pre-trade ratio would produce
    assert abs(D(str(m.hedge_ratio_error))) < D("0.11")


def test_paper_account_survives_a_restart_instead_of_engaging_safe_hold(tmp_path: Path) -> None:
    """Observed 2026-09-04, 7 minutes into the first shadow run.

    The paper exchange lives in memory. On restart it came up flat while the ledger still held 162
    contracts, reconciliation disagreed, and the trader engaged SAFE_HOLD — with KeepAlive on the
    launchd agent that would end a multi-week observation at the first restart.
    """
    h = Harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    (first,) = h.run_days(WARMUP + 1, 1)
    assert first.action == "rebalance"
    held = h.exchange.position.contracts
    assert held < 0  # a real short is open

    # restart: fresh in-memory exchange and a fresh trader against the SAME database
    h.new_exchange()
    assert h.exchange.position.contracts == D("0")  # the simulated account really did reset
    restarted = h.build_trader()
    report = restarted.start()

    assert report["portfolio"] == "reconciled"
    assert not restarted.kill_switch.engaged
    assert h.exchange.position.contracts == held  # restored, not re-adopted at zero


def test_a_long_paper_state_is_refused_rather_than_loaded(tmp_path: Path) -> None:
    """This account is short-only; a saved long means the state is not ours."""
    h = Harness(tmp_path)
    h.build_trader()
    with pytest.raises(ExchangeError, match="short-only"):
        h.exchange.restore({"wallet_balance": "1", "contracts": "5", "avg_entry_price": "30000"})


def test_corrupt_paper_state_is_refused(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.build_trader()
    with pytest.raises(ExchangeError, match="cannot restore"):
        h.exchange.restore({"wallet_balance": "not-a-number", "contracts": "-1"})
