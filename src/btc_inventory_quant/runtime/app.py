"""Mode wiring: shadow / testnet / live runners and operational commands (status, reconcile, safe-hold)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from btc_inventory_quant.config.loader import load_config
from btc_inventory_quant.config.schema import AppConfig, ExchangeName, ExecutionPolicyName, RunMode
from btc_inventory_quant.config.settings import Settings, load_settings
from btc_inventory_quant.data.repositories import load_contract_spec
from btc_inventory_quant.domain.inverse_contract import ContractSpec
from btc_inventory_quant.exchange.base import ExchangeAdapter, ExchangeError
from btc_inventory_quant.exchange.binance_coinm import BinanceCoinMAdapter
from btc_inventory_quant.exchange.binance_ws import MarkPriceStream, StreamBundle, UserDataStream
from btc_inventory_quant.exchange.paper import PaperExchange
from btc_inventory_quant.execution.order_manager import OrderManager
from btc_inventory_quant.execution.policies import (
    ExecutionPolicy,
    MakerFirstExecution,
    MarketExecution,
    TwapExecution,
)
from btc_inventory_quant.execution.reconciliation import Reconciler
from btc_inventory_quant.monitoring.alerts import Alert, Alerter, build_alerter
from btc_inventory_quant.monitoring.logging import get_logger
from btc_inventory_quant.monitoring.metrics import MetricsRegistry
from btc_inventory_quant.risk.guards import Guards
from btc_inventory_quant.risk.kill_switch import KillSwitch
from btc_inventory_quant.runtime.live_gate import RuntimeEvidence, evaluate_live_gate
from btc_inventory_quant.runtime.trader import Trader
from btc_inventory_quant.storage.database import Database
from btc_inventory_quant.strategy.benchmarks import build_strategy

log = get_logger(__name__)


@dataclass(slots=True)
class Components:
    config: AppConfig
    settings: Settings
    db: Database
    exchange: ExchangeAdapter
    spec: ContractSpec
    alerter: Alerter
    kill_switch: KillSwitch
    order_manager: OrderManager
    policy: ExecutionPolicy
    reconciler: Reconciler
    guards: Guards
    metrics: MetricsRegistry
    streams: StreamBundle | None = None


def build_streams(config: AppConfig, exchange: ExchangeAdapter) -> StreamBundle | None:
    """User-data stream (needs credentials) + mark-price stream (public). REST stays authoritative."""
    if not config.exchange.use_websocket:
        return None
    adapter: BinanceCoinMAdapter | None = None
    if isinstance(exchange, BinanceCoinMAdapter):
        adapter = exchange
    elif isinstance(exchange, PaperExchange) and isinstance(exchange.market, BinanceCoinMAdapter):
        adapter = exchange.market
    if adapter is None:
        return None
    user = UserDataStream(adapter) if adapter.supports_trading else None
    mark = MarkPriceStream(adapter.ws_url, config.exchange.symbol)
    return StreamBundle(user=user, mark=mark)


def build_exchange(config: AppConfig, settings: Settings, mode: RunMode) -> ExchangeAdapter:
    ex = config.exchange
    if mode is RunMode.SHADOW:
        # real public market data (no credentials), simulated account — real orders are impossible
        market = BinanceCoinMAdapter(
            environment="production",
            recv_window_ms=ex.recv_window_ms,
            timeout_seconds=ex.request_timeout_seconds,
            max_retries=ex.max_retries,
            backoff_base_seconds=ex.backoff_base_seconds,
            backoff_max_seconds=ex.backoff_max_seconds,
        )
        spec = load_contract_spec(config.data.exchange_info_path, ex.symbol)
        return PaperExchange(
            market,
            spec,
            initial_btc=ex.paper_initial_btc,
            symbol=ex.symbol,
            taker_fee=config.backtest.cost.taker_fee_rate,
            maker_fee=config.backtest.cost.maker_fee_rate,
            slippage_bps=config.backtest.cost.slippage_bps,
            environment="shadow",
        )
    if ex.name is not ExchangeName.BINANCE_COINM:
        raise ValueError(f"exchange {ex.name} not supported for mode {mode}")
    key, secret = settings.credentials_for(ex.environment)
    return BinanceCoinMAdapter(
        api_key=key.get_secret_value() if key else None,
        api_secret=secret.get_secret_value() if secret else None,
        environment=ex.environment,
        recv_window_ms=ex.recv_window_ms,
        timeout_seconds=ex.request_timeout_seconds,
        max_retries=ex.max_retries,
        backoff_base_seconds=ex.backoff_base_seconds,
        backoff_max_seconds=ex.backoff_max_seconds,
    )


def build_components(
    config: AppConfig, settings: Settings, mode: RunMode, exchange: ExchangeAdapter | None = None
) -> Components:
    db = Database(
        config.storage.database_url or settings.database_url, echo=config.storage.echo_sql
    )
    db.create_all()
    exchange = exchange or build_exchange(config, settings, mode)
    spec = load_contract_spec(config.data.exchange_info_path, config.exchange.symbol)
    alerter = build_alerter(
        settings, tuple(c.value for c in config.monitoring.alert_channels), recorder=_recorder(db)
    )
    kill_switch = KillSwitch(db, alerter)
    streams = build_streams(config, exchange)
    om = OrderManager(
        exchange,
        db,
        spec,
        config.risk,
        config.exchange.symbol,
        mode.value,
        client_id_prefix=config.execution.client_order_id_prefix,
        order_timeout_seconds=config.execution.order_timeout_seconds,
        event_source=streams.user if streams is not None else None,
    )
    policy: ExecutionPolicy
    match config.execution.policy:
        case ExecutionPolicyName.MAKER_FIRST:
            policy = MakerFirstExecution(
                om, exchange, wait_seconds=config.execution.maker_wait_seconds
            )
        case ExecutionPolicyName.TWAP:
            policy = TwapExecution(
                om,
                slices=config.execution.twap_slices,
                interval_seconds=config.execution.twap_interval_seconds,
            )
        case _:
            policy = MarketExecution(om)
    reconciler = Reconciler(exchange, db, spec, config.exchange.symbol, config.risk)
    guards = Guards(
        config.risk,
        spec,
        config.exchange.symbol,
        expected_margin_type=config.exchange.expected_margin_type,
        expected_leverage=config.exchange.expected_leverage,
    )
    metrics = MetricsRegistry(db, file_path=Path("state") / f"metrics_{mode.value}.json")
    return Components(
        config,
        settings,
        db,
        exchange,
        spec,
        alerter,
        kill_switch,
        om,
        policy,
        reconciler,
        guards,
        metrics,
        streams,
    )


def _recorder(db: Database) -> Any:
    def record(alert: Alert) -> None:
        db.add_event(alert.level, alert.event_type, alert.message, alert.payload)

    return record


def gather_live_evidence(c: Components) -> RuntimeEvidence:
    """Collect runtime facts for the live gate (read-only exchange calls)."""
    ex = c.exchange
    key_present = bool(c.settings.binance_api_key and c.settings.binance_api_secret)
    perms_ok = False
    time_ok = False
    offset: int | None = None
    pos_ok = False
    mode_ok = False
    ratio_ok = False
    data_ok = False
    spec_ok = False
    equity: Decimal | None = None
    try:
        sync = getattr(ex, "sync_time", None)
        offset = int(sync()) if callable(sync) else 0
        time_ok = abs(offset) <= c.config.risk.max_clock_skew_ms
        live_spec = ex.contract_spec(c.config.exchange.symbol)
        spec_ok = c.guards.check_contract_spec(live_spec).ok
        info = ex.premium_index(c.config.exchange.symbol)
        data_ok = c.guards.check_mark_freshness(info, datetime.now(tz=UTC)).ok
        if key_present:
            account = ex.account()
            perms_ok = account.can_trade
            mode_ok = c.guards.check_position_mode(ex.position_mode()).ok
            positions = [
                p
                for p in ex.positions(pair=c.spec.pair or None)
                if p.symbol == c.config.exchange.symbol
            ]
            position = positions[0] if positions else None
            equity = account.wallet_balance + account.unrealized_pnl
            ratio_ok = (
                c.guards.check_hedge_cap(account, position, info.mark_price).ok
                and not c.guards.check_position(position)[0].critical
            )
            pos_ok = (
                position is None or position.contracts <= 0
            ) and not c.order_manager.unresolved_orders()
    except ExchangeError as exc:
        log.error("live evidence gathering failed", fields={"error": str(exc)})
    return RuntimeEvidence(
        api_key_present=key_present,
        api_permissions_ok=perms_ok,
        time_synced=time_ok,
        clock_offset_ms=offset,
        position_consistent=pos_ok,
        no_unconfirmed_orders=not c.order_manager.unresolved_orders(),
        hedge_ratio_within_limits=ratio_ok,
        market_data_fresh=data_ok,
        contract_spec_verified=spec_ok,
        position_mode_one_way=mode_ok,
        equity_btc=equity,
    )


def build_trader(c: Components, mode: RunMode, confirm_live: bool = False) -> Trader:
    strategy = build_strategy(c.config.strategy)
    orders_allowed = mode in (RunMode.SHADOW, RunMode.TESTNET)
    if mode is RunMode.LIVE:
        evidence = gather_live_evidence(c)
        gate = evaluate_live_gate(c.config, c.settings, confirm_live, evidence)
        c.db.add_event(
            "INFO" if gate.passed else "WARNING", "live_gate", "live gate evaluated", gate.to_dict()
        )
        if not gate.passed:
            log.error(
                "LIVE GATE FAILED — running read-only, no orders will be sent",
                fields={"failures": gate.failures},
            )
            c.alerter.notify(
                "live_gate_failed", f"live gate failed: {gate.failures}", level="ERROR"
            )
        else:
            log.warning("LIVE GATE PASSED — real orders enabled")
            c.alerter.notify(
                "live_gate_passed", "live gate passed; real orders enabled", level="WARNING"
            )
        orders_allowed = gate.passed
    return Trader(
        c.config,
        c.exchange,
        c.db,
        c.spec,
        strategy,
        c.alerter,
        c.kill_switch,
        c.order_manager,
        c.policy,
        c.reconciler,
        c.guards,
        c.metrics,
        orders_allowed=orders_allowed,
        streams=c.streams,
    )


def run_mode(
    mode: RunMode,
    config_path: str,
    confirm_live: bool = False,
    once: bool = False,
    max_cycles: int | None = None,
) -> int:
    config = load_config(config_path)
    if config.mode is not mode:
        raise SystemExit(
            f"config mode is {config.mode.value}, command is {mode.value}: refusing to run (use the matching config)"
        )
    settings = load_settings()
    components = build_components(config, settings, mode)
    trader = build_trader(components, mode, confirm_live=confirm_live)
    try:
        return trader.run(once=once, max_cycles=max_cycles)
    except ExchangeError as exc:
        log.error(
            "cannot run: exchange unreachable or rejected the request", fields={"error": str(exc)}
        )
        components.alerter.notify("system_start_failed", str(exc), level="ERROR")
        return 1


def ops_command(command: str, config_path: str, release: bool = False, ack: str = "") -> int:
    config = load_config(config_path)
    settings = load_settings()
    db = Database(config.storage.database_url or settings.database_url)
    db.create_all()
    alerter = build_alerter(
        settings, tuple(c.value for c in config.monitoring.alert_channels), recorder=_recorder(db)
    )
    ks = KillSwitch(db, alerter)
    if command == "status":
        status = ks.status()
        out = {
            "system_state": status.state.value,
            "safe_hold_reason": status.reason,
            "safe_hold_since": status.since.isoformat() if status.since else None,
            "metrics": MetricsRegistry.load(db),
            "latest_decision": _decision_dict(db.latest_decision()),
            "latest_reconciliation": _recon_dict(db.latest_reconciliation()),
            "unresolved_orders": [
                o.client_order_id
                for o in db.orders_with_status(
                    ["PENDING_SUBMIT", "SUBMITTED", "UNCERTAIN", "NEW", "PARTIALLY_FILLED"]
                )
            ],
            "recent_events": [
                {"ts": e.ts.isoformat(), "level": e.level, "type": e.event_type, "msg": e.message}
                for e in db.recent_events(10)
            ],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0
    if command == "safe-hold":
        if release:
            if ack != config.live.expected_acknowledgement:
                print("release refused: acknowledgement string does not match")
                return 2
            ks.release(ack, operator="cli")
            print("SAFE_HOLD released")
            return 0
        ks.engage("manual safe-hold from CLI")
        components = build_components(config, settings, config.mode)
        try:
            n = components.order_manager.cancel_all()
            print(f"SAFE_HOLD engaged; cancelled/refreshed {n} order(s); position kept")
        except ExchangeError as exc:
            print(f"SAFE_HOLD engaged; cancel failed: {exc}")
        return 0
    if command == "reconcile":
        components = build_components(config, settings, config.mode)
        trader = build_trader(components, config.mode)
        trader.portfolio = trader._load_portfolio(datetime.now(tz=UTC))
        snap = components.reconciler.snapshot()
        mark = components.exchange.premium_index(config.exchange.symbol).mark_price
        rec = components.reconciler.compare(
            trader.portfolio, snap, components.order_manager.known_client_ids(), mark
        )
        print(json.dumps(rec.to_dict(), indent=2, ensure_ascii=False))
        return 0 if rec.ok else 1
    return 2


def _decision_dict(d: Any) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "ts": d.ts.isoformat(),
        "candle_time": d.candle_time.isoformat() if d.candle_time else None,
        "regime": d.regime,
        "raw_target": str(d.raw_target),
        "decision_target": str(d.decision_target),
        "current_ratio": str(d.current_ratio),
        "action": d.action,
        "reason": d.reason,
    }


def _recon_dict(r: Any) -> dict[str, Any] | None:
    if r is None:
        return None
    return {"ts": r.ts.isoformat(), "ok": r.ok, "differences": r.differences, "action": r.action}
