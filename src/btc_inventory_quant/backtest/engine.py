"""Event-driven daily backtest engine for the inverse hedge strategy.

Timeline for execution day *j* (bar j opens at 00:00 UTC of day j):

1. Decision is derived from the signal of bar ``j − 1 − signal_delay_days`` — a *completed* candle.
2. 00:00 funding is settled on the position carried overnight (before any trade).
3. Current effective hedge ratio and target contracts are computed with ``mark_open[j]``.
4. Risk pipeline: clamp → daily cap → min-diff (plus pending re-orders from partial fills).
5. Orders fill at ``open[j]`` with slippage and taker fees (whole contracts, rounded down).
6. 08:00 / 16:00 funding is settled on the post-trade position.
7. End-of-day mark-to-market at ``mark_close[j]`` → daily record.

A signal computed on bar t is therefore never executed on bar t (no look-ahead), which is
enforced structurally and unit-tested (``test_no_lookahead``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from btc_inventory_quant.backtest.execution_model import ExecutionModel
from btc_inventory_quant.backtest.funding_model import FundingModel
from btc_inventory_quant.config.schema import CostModelConfig, RiskLimits
from btc_inventory_quant.data.funding_data import FundingCalendar
from btc_inventory_quant.data.market_data import DailyDataset
from btc_inventory_quant.domain.inverse_contract import ZERO, ContractSpec, OrderSide
from btc_inventory_quant.domain.portfolio import Portfolio
from btc_inventory_quant.execution.rebalancer import Rebalancer
from btc_inventory_quant.risk.limits import RiskLimitViolation, validate_order_against_position
from btc_inventory_quant.strategy.base import Strategy


class BacktestError(RuntimeError):
    pass


@dataclass(slots=True)
class PendingOrder:
    side: OrderSide
    qty: Decimal
    reason: str
    target_ratio: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy_name: str
    daily: pd.DataFrame
    trades: pd.DataFrame
    funding: pd.DataFrame
    fees: pd.DataFrame
    signals: pd.DataFrame
    ledger: pd.DataFrame
    portfolio: Portfolio
    stats: dict[str, Any]

    @property
    def start(self) -> datetime:
        return self.daily.index[0].to_pydatetime()  # type: ignore[no-any-return]

    @property
    def end(self) -> datetime:
        return self.daily.index[-1].to_pydatetime()  # type: ignore[no-any-return]


@dataclass(slots=True)
class BacktestEngine:
    dataset: DailyDataset
    funding_calendar: FundingCalendar
    spec: ContractSpec
    strategy: Strategy
    limits: RiskLimits
    cost: CostModelConfig
    initial_btc: Decimal = Decimal("1")
    seed: int = 42
    start: datetime | None = None
    end: datetime | None = None
    warmup_bars: int = 210
    strict: bool = True  # raise on risk violations (a violation in backtest is a bug)
    _signals: pd.DataFrame | None = field(default=None, init=False)

    # ------------------------------------------------------------------
    def _first_execution_index(self, frame: pd.DataFrame, signals: pd.DataFrame) -> int:
        warm = max(self.warmup_bars, self.strategy.warmup_bars)
        j0 = warm + 1 + self.cost.signal_delay_days
        if self.start is not None:
            start_ts = pd.Timestamp(self.start)
            pos = int(frame.index.searchsorted(start_ts))
            j0 = max(j0, pos)
        # first day whose decision signal is available
        while j0 < len(frame) and np.isnan(
            signals["raw_target"].iloc[j0 - 1 - self.cost.signal_delay_days]
        ):
            j0 += 1
        if j0 >= len(frame):
            raise BacktestError("not enough data after warm-up to run a backtest")
        return j0

    def run(self) -> BacktestResult:
        frame = self.dataset.frame
        if self.end is not None:
            frame = frame[frame.index <= pd.Timestamp(self.end)]
        signals = self.strategy.compute(frame)
        self._signals = signals
        j0 = self._first_execution_index(frame, signals)
        idx = frame.index
        delay = self.cost.signal_delay_days

        exec_model = ExecutionModel(self.cost, self.spec, self.seed)
        funding_model = FundingModel(self.funding_calendar, self.cost)
        rebalancer = Rebalancer(
            self.spec, self.limits, bypass_cap=self.strategy.bypass_max_hedge_cap
        )
        t_open = idx[j0].to_pydatetime()
        pf = Portfolio.open(self.spec, self.initial_btc, t_open)

        daily_rows: list[dict[str, Any]] = []
        trade_rows: list[dict[str, Any]] = []
        funding_rows: list[dict[str, Any]] = []
        fee_rows: list[dict[str, Any]] = []
        pending: list[PendingOrder] = []
        prev_equity_usd: Decimal | None = None
        violations = 0

        for j in range(j0, len(frame)):
            ts: pd.Timestamp = idx[j]
            day_dt = ts.to_pydatetime()
            day = day_dt.date()
            open_px = self.dataset.dec(ts, "open")
            mark_open = self.dataset.dec(ts, "mark_open")
            mark_close = self.dataset.dec(ts, "mark_close")

            # 2) overnight funding at 00:00 UTC
            settlements = funding_model.settlements_for_day(day, mark_open, mark_close)
            funding_today = ZERO
            for s in settlements:
                if s.ts.hour == 0:
                    funding_today += self._settle(pf, s, funding_rows)

            # 1) decision from a completed candle (j-1-delay)
            sig_i = j - 1 - delay
            raw = signals["raw_target"].iloc[sig_i]
            regime = signals["regime"].iloc[sig_i]
            equity_btc = pf.equity_btc(mark_open)
            if equity_btc <= ZERO:
                raise BacktestError(f"equity non-positive on {day}: {equity_btc}")
            current_ratio = pf.effective_hedge_ratio(mark_open)
            orders: list[PendingOrder] = []
            decision_target = current_ratio
            raw_dec = None if np.isnan(raw) else Decimal(str(round(float(raw), 6)))
            forced = bool(pending)
            missed = False
            if raw_dec is not None:
                rplan = rebalancer.plan(
                    raw_dec,
                    current_ratio,
                    pf.position.short_contracts,
                    equity_btc,
                    mark_open,
                    force=forced,
                    reason="reorder" if forced else "rebalance",
                )
                do_rebalance = rplan.decision.rebalance or forced
                decision_target = (
                    rplan.decision.capped_target_ratio if do_rebalance else current_ratio
                )
                if rplan.intent is not None:
                    if exec_model.next_rebalance_is_missed():
                        missed = True
                    else:
                        orders.append(
                            PendingOrder(
                                rplan.intent.side,
                                rplan.intent.qty,
                                rplan.intent.reason,
                                rplan.intent.target_ratio,
                            )
                        )
            pending = []

            # 5) execution at the open of day j
            realized_today = ZERO
            fees_today = ZERO
            traded_notional_usd = ZERO
            for order in orders:
                plan = exec_model.plan_fill(order.side, order.qty, open_px)
                try:
                    validate_order_against_position(
                        order.side,
                        plan.fill_qty,
                        pf.position.short_contracts,
                        pf.equity_btc(mark_open),
                        mark_open,
                        self.spec,
                        self.limits,
                        bypass_cap=self.strategy.bypass_max_hedge_cap,
                    )
                except RiskLimitViolation as exc:
                    violations += 1
                    if self.strict:
                        raise BacktestError(f"risk violation on {day}: {exc}") from exc
                    continue
                ratio_before = pf.effective_hedge_ratio(mark_open)
                ref = f"bt-{day.isoformat()}-{order.side.value}-{len(trade_rows) + 1}"
                res = pf.apply_fill(
                    order.side,
                    plan.fill_qty,
                    plan.fill_price,
                    plan.fee_rate,
                    day_dt,
                    ref_id=ref,
                    reference_price=open_px,
                )
                realized_today += res.realized_pnl_btc
                fees_today += res.fee_btc
                traded_notional_usd += plan.fill_qty * self.spec.contract_value_usd
                ratio_after = pf.effective_hedge_ratio(mark_open)
                slip_bps = (plan.fill_price / open_px - Decimal(1)) * Decimal(10000)
                trade_rows.append(
                    {
                        "ts": day_dt,
                        "ref_id": ref,
                        "side": order.side.value,
                        "reason": order.reason,
                        "requested_qty": order.qty,
                        "filled_qty": plan.fill_qty,
                        "partial": plan.is_partial,
                        "reference_price": open_px,
                        "fill_price": plan.fill_price,
                        "slippage_bps": slip_bps,
                        "fee_rate": plan.fee_rate,
                        "fee_btc": res.fee_btc,
                        "realized_pnl_btc": res.realized_pnl_btc,
                        "notional_usd": plan.fill_qty * self.spec.contract_value_usd,
                        "contracts_after": res.contracts_after,
                        "avg_entry_after": res.avg_entry_after,
                        "target_ratio": order.target_ratio,
                        "ratio_before": ratio_before,
                        "ratio_after": ratio_after,
                    }
                )
                fee_rows.append(
                    {
                        "ts": day_dt,
                        "ref_id": ref,
                        "fee_btc": res.fee_btc,
                        "fee_rate": plan.fee_rate,
                        "qty": plan.fill_qty,
                        "price": plan.fill_price,
                    }
                )
                if plan.remainder_qty > ZERO and self.cost.reorder_missed_next_day:
                    pending.append(
                        PendingOrder(order.side, plan.remainder_qty, "reorder", order.target_ratio)
                    )

            # 6) intraday funding on the post-trade position
            for s in settlements:
                if s.ts.hour != 0:
                    funding_today += self._settle(pf, s, funding_rows)

            # 7) end-of-day mark-to-market
            snap = pf.snapshot(day_dt, mark_close)
            hodl_usd = self.initial_btc * mark_close
            daily_ret_usd = (
                None
                if prev_equity_usd is None
                else (snap.equity_usd / prev_equity_usd - Decimal(1))
            )
            prev_equity_usd = snap.equity_usd
            daily_rows.append(
                {
                    "date": ts,
                    "open": open_px,
                    "close": self.dataset.dec(ts, "close"),
                    "mark_open": mark_open,
                    "mark_close": mark_close,
                    "regime": regime,
                    "raw_target": raw_dec,
                    "decision_target": decision_target,
                    "current_ratio_at_open": current_ratio,
                    "effective_ratio": snap.effective_hedge_ratio,
                    "ratio_error": (snap.effective_hedge_ratio - decision_target),
                    "wallet_btc": snap.wallet_balance_btc,
                    "unrealized_btc": snap.unrealized_pnl_btc,
                    "equity_btc": snap.equity_btc,
                    "equity_usd": snap.equity_usd,
                    "hodl_btc": self.initial_btc,
                    "hodl_usd": hodl_usd,
                    "short_contracts": snap.short_contracts,
                    "short_notional_usd": snap.short_notional_usd,
                    "avg_entry_price": snap.avg_entry_price,
                    "realized_btc_today": realized_today,
                    "fees_btc_today": fees_today,
                    "funding_btc_today": funding_today,
                    "trades_today": len(orders),
                    "traded_notional_usd": traded_notional_usd,
                    "missed_rebalance": missed,
                    "daily_return_usd": daily_ret_usd,
                    "liquidation_price_estimate": snap.liquidation_price_estimate,
                }
            )
            pf.check_ledger_invariant()

        daily = pd.DataFrame(daily_rows).set_index("date")
        trades = pd.DataFrame(trade_rows)
        funding = pd.DataFrame(funding_rows)
        fees = pd.DataFrame(fee_rows)
        ledger = pd.DataFrame(
            [
                {
                    "ts": e.ts,
                    "type": e.entry_type.value,
                    "amount_btc": e.amount_btc,
                    "ref_id": e.ref_id,
                    "price": e.price,
                    "contracts": e.contracts,
                    "note": e.note,
                }
                for e in pf.ledger.entries
            ]
        )
        stats = {
            "strategy": self.strategy.name,
            "first_execution_day": idx[j0].isoformat(),
            "last_day": idx[-1].isoformat(),
            "days": len(daily),
            "rebalance_orders_generated": exec_model.rebalance_count,
            "missed_rebalances": exec_model.missed_count,
            "partial_fills": exec_model.partial_count,
            "funding_events_exact_mark": funding_model.exact_count,
            "funding_events_approximated_mark": funding_model.approximated_count,
            "risk_violations": violations,
            "seed": self.seed,
        }
        return BacktestResult(
            strategy_name=self.strategy.name,
            daily=daily,
            trades=trades,
            funding=funding,
            fees=fees,
            signals=signals,
            ledger=ledger,
            portfolio=pf,
            stats=stats,
        )

    @staticmethod
    def _settle(pf: Portfolio, s: Any, funding_rows: list[dict[str, Any]]) -> Decimal:
        contracts_before = pf.position.short_contracts
        amount = pf.apply_funding(
            s.effective_rate, s.mark_price, s.ts, ref_id=f"funding-{s.ts.isoformat()}"
        )
        funding_rows.append(
            {
                "ts": s.ts,
                "rate": s.rate,
                "effective_rate": s.effective_rate,
                "mark_price": s.mark_price,
                "mark_source": s.mark_source,
                "short_contracts": contracts_before,
                "amount_btc": amount,
            }
        )
        return amount
