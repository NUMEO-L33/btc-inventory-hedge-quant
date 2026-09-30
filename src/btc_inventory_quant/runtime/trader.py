"""The trading loop shared by shadow / testnet / live modes.

One *cycle* = one daily decision (after the UTC daily candle closes):

    health (ping, time sync) → completed candles → signal → exchange snapshot → income sync →
    reconciliation → guards → rebalance plan → (orders allowed?) execute → post-trade verification →
    persist decision / snapshot / metrics → alerts

Any inconsistency, stale data, unknown order or unexpected error engages SAFE_HOLD:
no new orders, open orders cancelled, the position is KEPT (never force-closed).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from btc_inventory_quant.config.schema import AppConfig, RunMode
from btc_inventory_quant.data.funding_features import attach_funding_features, rows_to_events
from btc_inventory_quant.data.market_data import klines_frame
from btc_inventory_quant.domain.inverse_contract import (
    ZERO,
    ContractSpec,
    OrderSide,
    effective_hedge_ratio,
)
from btc_inventory_quant.domain.portfolio import Portfolio
from btc_inventory_quant.exchange.base import (
    ExchangeAdapter,
    ExchangeError,
    ExchangeTimeout,
    ExchangeUnavailable,
)
from btc_inventory_quant.exchange.binance_ws import StreamBundle
from btc_inventory_quant.execution.order_manager import (
    DuplicateRiskError,
    OrderManager,
    UnresolvedOrderError,
)
from btc_inventory_quant.execution.policies import (
    ExecutionContext,
    ExecutionPolicy,
    ExecutionReport,
)
from btc_inventory_quant.execution.rebalancer import OrderIntent, RebalancePlan, Rebalancer
from btc_inventory_quant.execution.reconciliation import Reconciler
from btc_inventory_quant.monitoring.alerts import Alerter
from btc_inventory_quant.monitoring.logging import get_logger
from btc_inventory_quant.monitoring.metrics import MetricsRegistry
from btc_inventory_quant.risk.guards import GuardResult, Guards, critical_failures, failures
from btc_inventory_quant.risk.kill_switch import KillSwitch
from btc_inventory_quant.storage.database import Database
from btc_inventory_quant.strategy.base import (
    Strategy,
    strategy_funding_lookbacks,
    strategy_requires_funding,
)

log = get_logger(__name__)
STATE_PORTFOLIO = "portfolio"
STATE_LAST_INCOME_SYNC_MS = "last_income_sync_ms"
STATE_SAFE_HOLD_CANCELLED = "safe_hold_orders_cancelled"
STATE_LAST_FUNDING_SETTLE_MS = "last_paper_funding_settle_ms"
STATE_PREV_EQUITY = "prev_cycle_equity"
STATE_PAPER_ACCOUNT = "paper_account"
STATE_STALE_CYCLES = "stale_data_cycles"
STATE_CLOCK_SKEW_CYCLES = "clock_skew_cycles"
# Soft guard failures that the next few minutes may fix; a decision blocked only by these is
# retried within the day (see Trader.retryable) and latches only after several bad slots.
TRANSIENT_BLOCK_CODES = frozenset({"WS_SILENT", "CLOCK_SKEW"})
# Awake time an alive stream may go without a single connect attempt or message before the
# supervisor concludes its loop is stuck. A healthy loop attempts at least every ~76 s: 30 s
# backoff + 10 s open timeout when the network is down, 60 s silence + 5 s recv timeout + the
# connect when a socket is up but dead. Three minutes is a stuck loop, not a slow network.
STREAM_STALL_SECONDS = 180.0


@dataclass(slots=True)
class CycleResult:
    ts: datetime
    action: str  # none | rebalance | blocked | safe_hold | skipped | error
    candle_time: datetime | None = None
    regime: str | None = None
    raw_target: Decimal | None = None
    plan: RebalancePlan | None = None
    intent: OrderIntent | None = None
    report: ExecutionReport | None = None
    guard_failures: list[GuardResult] = field(default_factory=list)
    reconciliation_ok: bool | None = None
    message: str = ""
    equity_btc: Decimal | None = None
    effective_ratio: Decimal | None = None


class Trader:
    def __init__(
        self,
        config: AppConfig,
        exchange: ExchangeAdapter,
        db: Database,
        spec: ContractSpec,
        strategy: Strategy,
        alerter: Alerter,
        kill_switch: KillSwitch,
        order_manager: OrderManager,
        policy: ExecutionPolicy,
        reconciler: Reconciler,
        guards: Guards,
        metrics: MetricsRegistry,
        *,
        orders_allowed: bool,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        streams: StreamBundle | None = None,
    ) -> None:
        self.config = config
        self.streams = streams if (streams is not None and streams.enabled) else None
        self.exchange = exchange
        self.db = db
        self.spec = spec
        self.strategy = strategy
        self.alerter = alerter
        self.kill_switch = kill_switch
        self.order_manager = order_manager
        self.policy = policy
        self.reconciler = reconciler
        self.guards = guards
        self.metrics = metrics
        self.orders_allowed = orders_allowed
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self._sleep = sleep
        self.symbol = config.exchange.symbol
        self.rebalancer = Rebalancer(spec, config.risk, bypass_cap=strategy.bypass_max_hedge_cap)
        self.portfolio: Portfolio | None = None
        self.consecutive_data_errors = 0
        self._last_data_error_slot: datetime | None = None
        self._last_stream_restart: datetime | None = None
        # stream stall tracking: what the streams had done when we last looked, how long (awake)
        # that has stayed the same, and when we last looked
        self._stall_marker: Any = None
        self._stall_awake_seconds = 0.0
        self._last_supervised_at: datetime | None = None
        self.started = False

    # ------------------------------------------------------------------ start
    def start(self) -> dict[str, Any]:
        self.db.create_all()
        now = self.clock()
        report: dict[str, Any] = {
            "mode": self.config.mode.value,
            "orders_allowed": self.orders_allowed,
        }
        self.exchange.ping()
        offset = self._sync_time()
        report["clock_offset_ms"] = offset
        live_spec = self.exchange.contract_spec(self.symbol)
        spec_check = self.guards.check_contract_spec(live_spec)
        report["contract_spec"] = spec_check.code
        if not spec_check.ok:
            self._engage("contract specification changed", {"detail": spec_check.message})
        report["commission"] = self._commission_report()
        recovery = self.order_manager.recover()
        report["order_recovery"] = recovery
        if recovery["unresolved"] or recovery["unknown_exchange_orders"]:
            self._engage("unresolved or unknown orders at startup", recovery)
        self._restore_simulated_account()
        self.portfolio = self._load_portfolio(now)
        snap = self.reconciler.snapshot()
        mark = self._mark_price().mark_price
        if self.db.get_state(STATE_PORTFOLIO) is None:
            self.reconciler.adopt(self.portfolio, snap, now, as_opening=True)
            self._save_portfolio()
            report["portfolio"] = "adopted_from_exchange"
        else:
            self.reconciler.sync_income(
                self.portfolio, self.db.get_state(STATE_LAST_INCOME_SYNC_MS)
            )
            rec = self.reconciler.compare(
                self.portfolio, snap, self.order_manager.known_client_ids(), mark
            )
            report["portfolio"] = "reconciled" if rec.ok else "MISMATCH"
            if not rec.ok:
                self._engage("internal state differs from exchange at startup", rec.to_dict())
        if self.streams is not None:
            self.streams.start()
            report["streams"] = [st.name for st in self.streams.statuses()]
        self.started = True
        self.alerter.notify(
            "system_start",
            f"{self.config.mode.value} started (orders_allowed={self.orders_allowed})",
            payload=report,
        )
        return report

    def stop(self) -> None:
        if self.streams is not None:
            self.streams.stop()
        self._save_portfolio()
        self.metrics.persist()
        self.alerter.notify("system_stop", f"{self.config.mode.value} stopped")

    # ------------------------------------------------------------------ cycle
    def cycle(self, now: datetime | None = None) -> CycleResult:
        now = now or self.clock()
        if not self.started:
            raise RuntimeError("call start() first")
        assert self.portfolio is not None
        # Waking past the decision time runs the cycle before any heartbeat gets a turn, so a
        # stream that died during sleep would be judged before anyone tried to repair it.
        # Give the supervisor first go; if data is still stale the guard blocks, as it should.
        self._supervise_streams()
        if self.kill_switch.engaged:
            self._cancel_once_in_safe_hold()
            self._update_metrics(now, None, None)
            return CycleResult(now, "safe_hold", message=self.kill_switch.status().reason)
        try:
            return self._cycle(now)
        except (ExchangeTimeout, ExchangeUnavailable) as exc:
            self.metrics.update(api_error_count=self.metrics.current.api_error_count + 1)
            # Counted once per decision slot, not once per attempt: the run loop retries a
            # failed decision within the day, and three retries into one outage are one bad
            # day, not the three bad days this escalation is meant to catch.
            slot = self.decision_slot(now)
            if slot != self._last_data_error_slot:
                self._last_data_error_slot = slot
                self.consecutive_data_errors += 1
                self.alerter.notify("exchange_unavailable", str(exc), level="WARNING")
            else:
                log.warning(
                    "exchange still unavailable on retry",
                    fields={"error": str(exc), "bad_slots": self.consecutive_data_errors},
                )
            if self.consecutive_data_errors >= 3:
                self._engage("repeated exchange connectivity failures", {"error": str(exc)})
            return CycleResult(now, "error", message=str(exc))
        except (UnresolvedOrderError, DuplicateRiskError) as exc:
            self._engage("order state uncertain", {"error": str(exc)})
            return CycleResult(now, "safe_hold", message=str(exc))
        except Exception as exc:  # any unknown failure must fail safe
            log.exception("unexpected error in cycle")
            self._engage("unexpected error", {"error": repr(exc)})
            return CycleResult(now, "safe_hold", message=repr(exc))

    def _cycle(self, now: datetime) -> CycleResult:
        assert self.portfolio is not None
        offset = self._sync_time()
        clock_guard = self.guards.check_clock(offset)
        guard_results: list[GuardResult] = [clock_guard]
        if not clock_guard.critical and self._track_clock_skew(clock_guard, now):
            return CycleResult(
                now,
                "safe_hold",
                guard_failures=[clock_guard],
                message="clock skew persisted across cycles",
            )
        mark_info = self._mark_price()
        guard_results.append(self.guards.check_mark_freshness(mark_info, now))
        mark = mark_info.mark_price

        candles = self.completed_candles(now)
        last_close = candles["close_time"].iloc[-1].to_pydatetime()
        guard_results.append(
            self.guards.check_candle_freshness(last_close, now, max_age_seconds=26 * 3600)
        )
        signals = self.strategy.compute(candles)
        candle_time = candles.index[-1].to_pydatetime()
        raw = signals["raw_target"].iloc[-1]
        regime = str(signals["regime"].iloc[-1])
        raw_target = None if np.isnan(raw) else Decimal(str(round(float(raw), 6)))
        self.consecutive_data_errors = 0

        # shadow mode: settle the real funding history on the paper account (before today's trade)
        settle = getattr(self.exchange, "settle_funding", None)
        if callable(settle):
            applied = settle(self.db.get_state(STATE_LAST_FUNDING_SETTLE_MS))
            if applied:
                self.db.set_state(
                    STATE_LAST_FUNDING_SETTLE_MS, max(int(a["fundingTime"]) for a in applied) + 1
                )

        # exchange state, income, reconciliation
        snap = self.reconciler.snapshot()
        income = self.reconciler.sync_income(
            self.portfolio, self.db.get_state(STATE_LAST_INCOME_SYNC_MS)
        )
        self.db.set_state(STATE_LAST_INCOME_SYNC_MS, int(now.timestamp() * 1000) - 3_600_000)
        guard_results.append(
            self.guards.check_unexpected_transfers(Decimal(income["unexpected_amount_btc"]))
        )
        rec = self.reconciler.compare(
            self.portfolio, snap, self.order_manager.known_client_ids(), mark
        )
        self.metrics.update(reconciliation_ok=rec.ok, last_reconciliation_time=now.isoformat())
        if not rec.ok:
            self._engage("internal state differs from exchange", rec.to_dict())
            return CycleResult(
                now,
                "safe_hold",
                candle_time,
                regime,
                raw_target,
                reconciliation_ok=False,
                message="reconciliation mismatch",
            )

        position = snap.position
        account = snap.account
        guard_results.append(self.guards.check_position_mode(snap.mode))
        guard_results.extend(self.guards.check_position(position))
        guard_results.append(self.guards.check_hedge_cap(account, position, mark))
        guard_results.append(self.guards.check_margin_buffer(account))
        guard_results.append(
            self.guards.check_unknown_orders(
                snap.open_orders, self.order_manager.known_client_ids()
            )
        )
        guard_results.append(self.guards.check_rejects(self.order_manager.consecutive_rejects))
        if self.streams is not None:
            silence_guard = self.guards.check_stream_silence(
                self.streams.max_silence_seconds(now), self.streams.connected()
            )
            guard_results.append(silence_guard)
            if self._track_stale_data(silence_guard, now):
                return CycleResult(
                    now,
                    "safe_hold",
                    candle_time,
                    regime,
                    raw_target,
                    guard_failures=[silence_guard],
                    reconciliation_ok=True,
                    message="stale market data persisted across cycles",
                )
            ws_pos = (
                self.streams.user.last_position(self.symbol)
                if self.streams.user is not None
                else None
            )
            guard_results.append(
                self.guards.check_ws_position(ws_pos.position_amt if ws_pos else None, position)
            )
        crit = critical_failures(guard_results)
        if crit:
            self._engage(
                "critical guard failure", {"guards": [f"{g.code}: {g.message}" for g in crit]}
            )
            return CycleResult(
                now,
                "safe_hold",
                candle_time,
                regime,
                raw_target,
                guard_failures=crit,
                reconciliation_ok=True,
                message="critical guard",
            )

        equity_btc = account.wallet_balance + account.unrealized_pnl
        short = position.short_contracts if position else ZERO
        current_ratio = (
            short * self.spec.contract_value_usd / (equity_btc * mark)
            if equity_btc > ZERO
            else Decimal("Infinity")
        )
        self._record_internal_snapshot(now, mark, equity_btc, current_ratio, None)

        if raw_target is None:
            self._update_metrics(now, equity_btc, mark, current_ratio=current_ratio)
            return CycleResult(
                now,
                "skipped",
                candle_time,
                regime,
                None,
                reconciliation_ok=True,
                message="strategy warm-up",
                equity_btc=equity_btc,
                effective_ratio=current_ratio,
            )

        last_decision = self.db.latest_decision()
        if (
            last_decision is not None
            and last_decision.candle_time == candle_time
            and last_decision.action in ("rebalance", "none")
        ):
            self._update_metrics(now, equity_btc, mark, current_ratio=current_ratio)
            return CycleResult(
                now,
                "skipped",
                candle_time,
                regime,
                raw_target,
                reconciliation_ok=True,
                message="already decided for this candle",
                equity_btc=equity_btc,
                effective_ratio=current_ratio,
            )

        plan = self.rebalancer.plan(
            raw_target,
            current_ratio,
            short,
            equity_btc,
            mark,
            max_order_contracts=Decimal(self.config.execution.max_order_contracts),
        )
        soft = failures(guard_results)
        action = "none"
        report: ExecutionReport | None = None
        message = plan.skipped_reason
        if plan.intent is not None:
            if soft:
                action, message = (
                    "blocked",
                    f"non-critical guard failures: {[g.code for g in soft]}",
                )
            elif not self.orders_allowed:
                action, message = (
                    "blocked",
                    "orders not allowed in this mode/gate (intent logged only)",
                )
            else:
                action = "rebalance"
                report = self._execute(plan.intent, short, equity_btc, mark, now)
                message = f"filled {report.filled_qty}/{plan.intent.qty} avg={report.avg_price}"
        self.db.add_decision(
            ts=now,
            candle_time=candle_time,
            regime=regime,
            raw_target=raw_target,
            decision_target=plan.decision.executable_target,
            current_ratio=current_ratio,
            action=action,
            reason=message,
            details={
                "intent": None
                if plan.intent is None
                else {
                    "side": plan.intent.side.value,
                    "qty": str(plan.intent.qty),
                    "target_contracts": str(plan.target_contracts),
                },
                "guards": [g.code for g in soft],
            },
        )
        self._update_metrics(
            now,
            equity_btc,
            mark,
            current_ratio=current_ratio,
            target_ratio=plan.decision.executable_target,
        )
        self._save_portfolio()
        return CycleResult(
            now,
            action,
            candle_time,
            regime,
            raw_target,
            plan,
            plan.intent,
            report,
            soft,
            True,
            message,
            equity_btc,
            current_ratio,
        )

    # -------------------------------------------------------------- execution
    def _execute(
        self, intent: OrderIntent, short: Decimal, equity_btc: Decimal, mark: Decimal, now: datetime
    ) -> ExecutionReport:
        assert self.portfolio is not None
        ctx = ExecutionContext(short, equity_btc, mark)
        self.alerter.notify(
            "order_submitting",
            f"{intent.side.value} {intent.qty} contracts ({intent.reason}) target={intent.target_ratio}",
            payload={"current_ratio": str(intent.current_ratio)},
        )
        report = self.policy.execute(intent, ctx)
        for fill in report.fills:
            for t in fill.trades:
                ref = f"trade:{t.trade_id}"
                if self.db.ledger_has_ref(ref, "COMMISSION") or self.db.ledger_has_ref(
                    ref, "REALIZED_PNL"
                ):
                    continue
                ts = max(
                    t.time or now,
                    self.portfolio.ledger.entries[-1].ts if self.portfolio.ledger.entries else now,
                )
                res = self.portfolio.apply_fill(
                    t.side, t.qty, t.price, ZERO, ts, ref_id=ref, fee_btc=t.commission
                )
                for entry in self.portfolio.ledger.entries[-2:]:
                    if entry.ref_id == ref:
                        self.db.add_ledger_entry(entry)
                if (
                    abs(res.realized_pnl_btc - t.realized_pnl) > Decimal("0.00000005")
                    and t.realized_pnl != ZERO
                ):
                    log.warning(
                        "realized pnl differs from exchange",
                        fields={
                            "internal": str(res.realized_pnl_btc),
                            "exchange": str(t.realized_pnl),
                            "trade": t.trade_id,
                        },
                    )
        self.metrics.update(
            last_order_time=now.isoformat(),
            order_reject_count=self.metrics.current.order_reject_count
            + sum(1 for f in report.fills if f.status.value == "REJECTED"),
        )
        if report.rejected:
            self.alerter.notify(
                "order_failed", f"order rejected: {intent.side.value} {intent.qty}", level="ERROR"
            )
        elif not report.complete:
            self.alerter.notify(
                "order_partial", f"partial fill {report.filled_qty}/{intent.qty}", level="WARNING"
            )
        else:
            self.alerter.notify(
                "order_filled",
                f"{intent.side.value} {report.filled_qty} @ {report.avg_price} fee={report.commission_btc} BTC",
            )
        # post-trade verification against the exchange
        expected = short + (
            report.filled_qty if intent.side is OrderSide.SELL else -report.filled_qty
        )
        snap = self.reconciler.snapshot()
        actual = snap.position.short_contracts if snap.position else ZERO
        if actual != expected:
            self._engage(
                "post-trade position mismatch",
                {"expected_short": str(expected), "exchange_short": str(actual)},
            )
        elif snap.position is not None and snap.position.contracts > ZERO:
            self._engage("position flipped to LONG", {"contracts": str(snap.position.contracts)})
        self._save_portfolio()
        return report

    # --------------------------------------------------------------- helpers
    def completed_candles(self, now: datetime) -> pd.DataFrame:
        limit = max(self.strategy.warmup_bars, self.config.backtest.warmup_bars) + 60
        rows = self.exchange.klines(self.symbol, "1d", limit=min(limit, 1500))
        if not rows:
            raise ExchangeUnavailable("no klines returned")
        frame = klines_frame(rows)
        frame = frame[frame["close_time"] < pd.Timestamp(now)]
        if len(frame) < self.strategy.warmup_bars + 1:
            raise ExchangeUnavailable(
                f"only {len(frame)} completed candles (need {self.strategy.warmup_bars + 1})"
            )
        if strategy_requires_funding(self.strategy):
            frame = self._attach_funding(frame)
        return frame

    def _attach_funding(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Same causal funding features the backtest uses, built from the exchange history."""
        lookbacks = strategy_funding_lookbacks(self.strategy) or (21,)
        needed = max(lookbacks)
        start_ms = int((frame.index[0] - pd.Timedelta(days=needed // 3 + 3)).timestamp() * 1000)
        rows = self.exchange.funding_rate_history(self.symbol, start_ms=start_ms, limit=1000)
        events = rows_to_events(rows)
        if not events:
            raise ExchangeUnavailable(
                "funding history is empty; the funding guard cannot be evaluated"
            )
        return attach_funding_features(frame, events, lookbacks)

    def _mark_price(self) -> Any:
        return self.exchange.premium_index(self.symbol)

    def _sync_time(self) -> int:
        sync = getattr(self.exchange, "sync_time", None)
        if callable(sync):
            return int(sync())
        return 0

    def _commission_report(self) -> dict[str, Any]:
        """Live fee rates vs the ones the backtest assumed.

        Fees are not a rounding detail here: the V4 control study found that 92% of the apparent
        edge of a static hedge over a random one was transaction costs
        (``docs/strategy_v4_control_arms.md`` §5). If the account's real rates differ from the
        modelled 0.05%/0.02%, every cost-sensitive conclusion shifts with them, so the startup
        report states both and flags a material gap rather than leaving it to be noticed later.
        """
        cost = self.config.backtest.cost
        out: dict[str, Any] = {
            "assumed_taker": str(cost.taker_fee_rate),
            "assumed_maker": str(cost.maker_fee_rate),
        }
        try:
            rates = self.exchange.commission_rates(self.symbol)
        except (ExchangeError, NotImplementedError, AttributeError) as exc:
            out["actual"] = None
            out["error"] = str(exc)
            return out
        out["actual_taker"] = str(rates.taker)
        out["actual_maker"] = str(rates.maker)
        if cost.taker_fee_rate > 0:
            drift = (rates.taker - cost.taker_fee_rate) / cost.taker_fee_rate
            out["taker_drift_pct"] = f"{drift * 100:.2f}"
            # 10% off is enough to move a cost-dominated result; conservative (actual < assumed)
            # is reported too, because a backtest that overcharged is also mis-specified
            out["backtest_assumption_ok"] = abs(drift) <= Decimal("0.10")
            out["direction"] = "conservative" if rates.taker < cost.taker_fee_rate else "optimistic"
        return out

    def _track_stale_data(self, guard: GuardResult, now: datetime) -> bool:
        """Count consecutive stale-data cycles. True = time to latch SAFE_HOLD.

        A single stale cycle only blocks trading, because the usual cause is a suspended host
        and the streams recover on their own. The count lives in the database so that a restart
        cannot quietly reset it and hide a condition that has been failing for days.
        """
        return self._track_transient_block(
            guard,
            now,
            state_key=STATE_STALE_CYCLES,
            limit=self.config.risk.stale_data_cycles_to_safe_hold,
            what="stale market data",
            event="stale_data_blocked",
        )

    def _track_clock_skew(self, guard: GuardResult, now: datetime) -> bool:
        """Same rule for a clock that is off by more than the limit but not absurdly so."""
        return self._track_transient_block(
            guard,
            now,
            state_key=STATE_CLOCK_SKEW_CYCLES,
            limit=self.config.risk.clock_skew_cycles_to_safe_hold,
            what="clock skew",
            event="clock_skew_blocked",
        )

    def _track_transient_block(
        self,
        guard: GuardResult,
        now: datetime,
        *,
        state_key: str,
        limit: int,
        what: str,
        event: str,
    ) -> bool:
        """Block now, alert once per decision slot, latch after ``limit`` consecutive slots.

        The distinction this encodes: a condition that fixes itself within minutes (a
        suspended laptop, a clock that has not re-synced yet) must not cost a human a release,
        but one that is still there on the third day is not transient and must.
        """
        state = self.db.get_state(state_key) or {}
        if guard.ok:
            if int(state.get("count", 0)):
                self.db.set_state(state_key, {"count": 0})
                log.info(f"{what} cleared")
            return False
        slot = self.decision_slot(now).isoformat()
        if state.get("slot") == slot:
            # a retry of the same decision: still blocked, but the same bad day as before, and
            # the operator has already been told about it once
            log.warning(
                f"{what}; still blocked on retry",
                fields={
                    "cycle": int(state.get("count", 0)),
                    "limit": limit,
                    "guard": guard.message,
                },
            )
            return False
        count = int(state.get("count", 0)) + 1
        self.db.set_state(state_key, {"count": count, "last": now.isoformat(), "slot": slot})
        if count >= limit:
            self._engage(
                f"{what} persisted",
                {"cycles": count, "guard": f"{guard.code}: {guard.message}"},
            )
            return True
        log.warning(
            f"{what}; trading blocked this cycle",
            fields={"cycle": count, "limit": limit, "guard": guard.message},
        )
        self.alerter.notify(
            event,
            f"trading blocked: {guard.message} ({count}/{limit} cycles before SAFE_HOLD)",
            level="WARNING",
        )
        return False

    def _supervise_streams(self) -> None:
        """Restart the stream bundle when its own reconnect loop cannot recover.

        The streams heal themselves inside their background thread: a wake from suspend or a
        dead socket is noticed by the next ``recv`` timeout and reconnected with backoff, within
        seconds of the network being there. This covers what that thread cannot: a thread that
        has died outright, or a loop that is alive but no longer attempting anything.

        An alive stream is therefore never restarted on the strength of silence alone. On a
        laptop wake the heartbeat often runs *before* the stream thread has had its turn (three
        of the seven long wakes on 2026-09-13/14), and judging the stream then restarted one
        that would have reconnected on its own a second later - and paged the operator for it.
        Instead the supervisor notes what the stream has done (last message, last connect
        attempt) and restarts only when that stays unchanged for ``STREAM_STALL_SECONDS`` of
        awake time. Awake time is the sum of the gaps between polls, skipping gaps far longer
        than the poll interval: those are time the host spent suspended, which the stream thread
        could not have used either.
        """
        if self.streams is None:
            return
        now = self.clock()
        poll = self.config.execution.poll_interval_seconds
        gap = (
            0.0
            if self._last_supervised_at is None
            else (now - self._last_supervised_at).total_seconds()
        )
        self._last_supervised_at = now
        silence = self.streams.max_silence_seconds(now)
        limit = self.config.risk.ws_max_silence_seconds
        alive = self.streams.alive()
        if alive and not (silence is not None and silence > limit):
            self._stall_marker, self._stall_awake_seconds = None, 0.0
            return
        if alive:
            marker = self.streams.activity_marker()
            if marker != self._stall_marker:
                # first look, or the loop did something since the last one: it is on it
                self._stall_marker, self._stall_awake_seconds = marker, 0.0
                return
            if 0.0 < gap <= 2 * poll:
                self._stall_awake_seconds += gap
            if self._stall_awake_seconds < STREAM_STALL_SECONDS:
                return
        # The cooldown exists so a loop that stalls again right after a restart is not restarted
        # every few minutes. A dead thread is a different condition: nothing is running, so
        # restarting it cannot churn, and waiting out the cooldown just leaves the stream dead
        # longer (18 minutes on 2026-09-14).
        if (
            alive
            and self._last_stream_restart is not None
            and (now - self._last_stream_restart).total_seconds() < limit
        ):
            return
        self._last_stream_restart = now
        detail = (
            "thread dead"
            if not alive
            else f"silent for {silence:.0f}s, no reconnect attempt for "
            f"{self._stall_awake_seconds:.0f}s awake"
        )
        self._stall_marker, self._stall_awake_seconds = None, 0.0
        log.warning("restarting streams", fields={"reason": detail})
        self.alerter.notify("stream_restart", f"streams restarted ({detail})", level="WARNING")
        try:
            self.streams.restart()
        except Exception as exc:  # a failed restart must not kill the heartbeat
            log.error("stream restart failed", fields={"error": repr(exc)})

    def _engage(self, reason: str, details: dict[str, Any] | None = None) -> None:
        self.kill_switch.engage(reason, details)
        self._cancel_once_in_safe_hold()
        self.metrics.update(system_state="SAFE_HOLD")
        self.metrics.persist()

    def _cancel_once_in_safe_hold(self) -> None:
        status = self.kill_switch.status()
        marker = status.since.isoformat() if status.since else "unknown"
        if self.db.get_state(STATE_SAFE_HOLD_CANCELLED) == marker:
            return
        try:
            n = self.order_manager.cancel_all()
            log.warning("SAFE_HOLD: open orders cancelled, position kept", fields={"cancelled": n})
        except ExchangeError as exc:
            log.error("SAFE_HOLD: cancel_all failed", fields={"error": str(exc)})
        self.db.set_state(STATE_SAFE_HOLD_CANCELLED, marker)

    def _load_portfolio(self, now: datetime) -> Portfolio:
        saved = self.db.get_state(STATE_PORTFOLIO)
        pf = Portfolio.open(
            self.spec,
            Decimal(str(saved["wallet_balance_btc"])) if saved else ZERO,
            now if not saved else datetime.fromisoformat(saved["opened_at"]),
        )
        if saved:
            contracts = Decimal(str(saved["contracts"]))
            entry = saved.get("avg_entry_price")
            if contracts < ZERO and entry:
                pf.position.apply_fill(OrderSide.SELL, -contracts, Decimal(str(entry)), ZERO)
        return pf

    def _save_portfolio(self) -> None:
        assert self.portfolio is not None
        pf = self.portfolio
        self.db.set_state(
            STATE_PORTFOLIO,
            {
                "wallet_balance_btc": str(pf.wallet_balance_btc),
                "contracts": str(pf.position.contracts),
                "avg_entry_price": None
                if pf.position.avg_entry_price is None
                else str(pf.position.avg_entry_price),
                "opened_at": pf.ledger.entries[0].ts.isoformat()
                if pf.ledger.entries
                else self.clock().isoformat(),
                "saved_at": self.clock().isoformat(),
            },
        )
        self._save_simulated_account()

    def _restore_simulated_account(self) -> None:
        """Bring a saved paper account back before anything reconciles against it."""
        restore = getattr(self.exchange, "restore", None)
        state = self.db.get_state(STATE_PAPER_ACCOUNT)
        if callable(restore) and state:
            restore(state)
            log.info("paper account restored", fields={"contracts": state.get("contracts")})

    def _save_simulated_account(self) -> None:
        """Persist a simulated (paper) account alongside the ledger.

        Shadow runs on an in-memory paper exchange, so without this a restart brings up an empty
        account while the ledger still holds the position; reconciliation disagrees and the trader
        engages SAFE_HOLD. With KeepAlive on the launchd agent that ends a multi-week observation
        at the first restart — which is exactly what happened on 2026-09-04.
        """
        save = getattr(self.exchange, "state_dict", None)
        if callable(save):
            self.db.set_state(STATE_PAPER_ACCOUNT, save())

    def _record_internal_snapshot(
        self,
        now: datetime,
        mark: Decimal,
        equity_btc: Decimal,
        ratio: Decimal,
        target: Decimal | None,
    ) -> None:
        assert self.portfolio is not None
        pf = self.portfolio
        self.db.add_snapshot(
            ts=now,
            source="internal",
            contracts=pf.position.contracts,
            avg_entry_price=pf.position.avg_entry_price,
            mark_price=mark,
            wallet_balance_btc=pf.wallet_balance_btc,
            unrealized_pnl_btc=pf.unrealized_pnl_btc(mark),
            equity_btc=equity_btc,
            equity_usd=equity_btc * mark,
            target_ratio=target,
            effective_ratio=ratio if ratio.is_finite() else None,
        )

    def _update_metrics(
        self,
        now: datetime,
        equity_btc: Decimal | None,
        mark: Decimal | None,
        *,
        current_ratio: Decimal | None = None,
        target_ratio: Decimal | None = None,
    ) -> None:
        assert self.portfolio is not None
        pf = self.portfolio
        totals = pf.ledger.totals()
        # The caller's ``current_ratio`` is the PRE-trade ratio: the cycle computes it before
        # deciding, and the decision record rightly stores it that way. Metrics must show the
        # position as it stands NOW, so recompute from the portfolio after any fill — otherwise a
        # rebalance from flat reports effective_hedge_ratio 0 and hedge_ratio_error −target while
        # actually holding the full position, which reads as a permanent failure on a dashboard
        # watched for weeks. Observed 2026-09-04: 162 contracts / 16,200 USD notional against
        # 1 BTC equity reported as ratio 0.
        if mark is not None and equity_btc is not None and equity_btc > ZERO:
            current_ratio = effective_hedge_ratio(
                pf.position.short_contracts, self.spec.contract_value_usd, equity_btc, mark
            )
        equity_usd = None if equity_btc is None or mark is None else equity_btc * mark
        # daily PnL = change versus the previous cycle's equity (cycles are daily)
        prev = self.db.get_state(STATE_PREV_EQUITY) or {}
        daily_btc = daily_usd = None
        if equity_btc is not None and prev.get("equity_btc") is not None:
            daily_btc = equity_btc - Decimal(str(prev["equity_btc"]))
        if equity_usd is not None and prev.get("equity_usd") is not None:
            daily_usd = equity_usd - Decimal(str(prev["equity_usd"]))
        if equity_btc is not None and prev.get("cycle_day") != now.date().isoformat():
            self.db.set_state(
                STATE_PREV_EQUITY,
                {
                    "equity_btc": str(equity_btc),
                    "equity_usd": None if equity_usd is None else str(equity_usd),
                    "cycle_day": now.date().isoformat(),
                },
            )
        last_price: Decimal | None = None
        try:
            last_price = self.exchange.ticker_price(self.symbol).price
        except ExchangeError as exc:
            log.warning("ticker price unavailable", fields={"error": str(exc)})
        ws_connected: bool | None = None if self.streams is None else self.streams.connected()
        self.metrics.update(
            mode=self.config.mode.value,
            system_state=self.kill_switch.status().state.value,
            equity_btc=equity_btc,
            equity_usd=equity_usd,
            last_price=last_price,
            daily_pnl_btc=daily_btc,
            daily_pnl_usd=daily_usd,
            websocket_connected=ws_connected,
            wallet_balance_btc=pf.wallet_balance_btc,
            mark_price=mark,
            short_contracts=pf.position.short_contracts,
            short_notional_usd=pf.position.notional_usd(),
            avg_entry_price=pf.position.avg_entry_price,
            target_hedge_ratio=target_ratio,
            effective_hedge_ratio=None
            if current_ratio is None or not current_ratio.is_finite()
            else current_ratio,
            hedge_ratio_error=None
            if target_ratio is None or current_ratio is None or not current_ratio.is_finite()
            else current_ratio - target_ratio,
            realized_pnl_btc=totals.realized_pnl,
            unrealized_pnl_btc=None if mark is None else pf.unrealized_pnl_btc(mark),
            cumulative_funding_btc=totals.funding,
            cumulative_fees_btc=totals.commission,
            last_data_time=now.isoformat(),
            api_error_count=getattr(
                self.exchange, "error_count", self.metrics.current.api_error_count
            ),
            liquidation_price_estimate=None
            if mark is None
            else pf.snapshot(now, mark).liquidation_price_estimate,
        )
        self.metrics.persist()

    # ------------------------------------------------------------------ loop
    def next_decision_time(self, now: datetime) -> datetime:
        hh, mm = (int(x) for x in self.config.execution.decision_time_utc.split(":"))
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def decision_slot(self, now: datetime) -> datetime:
        """The most recent decision time at or before ``now``: which daily decision this is.

        Everything that escalates "one bad cycle" into SAFE_HOLD counts per slot, so that the
        in-day retries of a single failed decision cannot add up to a latch by themselves.
        """
        return self.next_decision_time(now) - timedelta(days=1)

    @staticmethod
    def retryable(res: CycleResult) -> bool:
        """A decision that failed for a reason the next few minutes may fix.

        ``error`` is the exchange being unreachable: nothing was decided or recorded. A
        ``blocked`` decision is retried only when the reasons were all transient (stale stream
        data, a clock that has not re-synced after a wake); the other soft guards (margin type,
        leverage setting) describe the account, and a retry would just log the same blocked
        intent every few minutes until someone changes it.
        The cycle is idempotent per candle, so retrying a decision that did go through is a
        no-op.
        """
        if res.action == "error":
            return True
        return (
            res.action == "blocked"
            and bool(res.guard_failures)
            and all(g.code in TRANSIENT_BLOCK_CODES for g in res.guard_failures)
        )

    def run(self, once: bool = False, max_cycles: int | None = None) -> int:
        cycles = 0
        self.start()
        try:
            if once:
                res = self.cycle()
                log.info("cycle done", fields={"action": res.action, "message": res.message})
                return 0
            # act on the latest completed candle right away (a restart after 00:05 UTC must not
            # skip today's decision); cycle() itself is idempotent per candle
            res = self.cycle()
            cycles += 1
            log.info("startup cycle done", fields={"action": res.action, "message": res.message})
            retry = timedelta(seconds=self.config.execution.decision_retry_seconds)
            while max_cycles is None or cycles < max_cycles:
                now = self.clock()
                nxt = self.next_decision_time(now)
                # A laptop that was asleep at the decision time runs the cycle the moment it
                # wakes, before Wi-Fi is back; without a retry that day's decision is simply
                # lost, and three such mornings in a row would latch SAFE_HOLD.
                due = min(nxt, now + retry) if self.retryable(res) else nxt
                if due < nxt:
                    log.info(
                        "decision will be retried",
                        fields={"at": due.isoformat(), "after": res.action, "why": res.message},
                    )
                while self.clock() < due:
                    self._heartbeat()
                    self._sleep(
                        min(
                            self.config.execution.poll_interval_seconds,
                            max(1.0, (due - self.clock()).total_seconds()),
                        )
                    )
                res = self.cycle()
                cycles += 1
                log.info(
                    "cycle done",
                    fields={"action": res.action, "message": res.message, "cycle": cycles},
                )
            return 0
        finally:
            self.stop()

    def _heartbeat(self) -> None:
        self._supervise_streams()
        try:
            info = self._mark_price()
            self.metrics.update(
                mark_price=info.mark_price,
                last_data_time=info.time.isoformat(),
                system_state=self.kill_switch.status().state.value,
            )
            self.metrics.persist()
        except ExchangeError as exc:
            self.metrics.update(api_error_count=self.metrics.current.api_error_count + 1)
            log.warning("heartbeat failed", fields={"error": str(exc)})


def mode_allows_orders(mode: RunMode) -> bool:
    return mode in (RunMode.SHADOW, RunMode.TESTNET)  # shadow orders go to the paper exchange only
