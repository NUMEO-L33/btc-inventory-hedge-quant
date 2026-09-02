"""Rebalancer: (raw target, current state) → at most one order intent.

Shared by the backtest engine and the live trader so both paths make identical decisions:

    raw target → clamp[min,max] → daily cap → min-diff → target contracts (floor) → δ contracts
    δ > 0 → SELL (open/increase short); δ < 0 → BUY reduce-only (never beyond the short size)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btc_inventory_quant.config.schema import RiskLimits
from btc_inventory_quant.domain.inverse_contract import (
    ZERO,
    ContractSpec,
    OrderSide,
    target_contracts,
)
from btc_inventory_quant.risk.limits import HedgeDecision, resolve_hedge_target


@dataclass(frozen=True, slots=True)
class OrderIntent:
    side: OrderSide
    qty: Decimal
    reduce_only: bool
    reason: str
    target_ratio: Decimal
    current_ratio: Decimal
    target_contracts: Decimal
    current_short_contracts: Decimal

    @property
    def notional_usd(self) -> Decimal:
        return self.qty


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    decision: HedgeDecision
    target_contracts: Decimal
    delta_contracts: Decimal  # positive = add short, negative = reduce short
    intent: OrderIntent | None
    skipped_reason: str = ""


class Rebalancer:
    def __init__(self, spec: ContractSpec, limits: RiskLimits, *, bypass_cap: bool = False) -> None:
        self.spec = spec
        self.limits = limits
        self.bypass_cap = bypass_cap

    def plan(
        self,
        raw_target: Decimal,
        current_ratio: Decimal,
        short_contracts: Decimal,
        equity_btc: Decimal,
        mark_price: Decimal,
        *,
        force: bool = False,
        reason: str = "rebalance",
        max_order_contracts: Decimal | None = None,
    ) -> RebalancePlan:
        decision = resolve_hedge_target(
            current_ratio, raw_target, self.limits, bypass_cap=self.bypass_cap
        )
        do_rebalance = decision.rebalance or force
        if not do_rebalance:
            return RebalancePlan(decision, short_contracts, ZERO, None, "below min_rebalance_diff")
        target = target_contracts(equity_btc, mark_price, decision.capped_target_ratio, self.spec)
        delta = target - short_contracts
        if delta == ZERO:
            return RebalancePlan(decision, target, ZERO, None, "target equals current contracts")
        side = OrderSide.SELL if delta > ZERO else OrderSide.BUY
        qty = abs(delta)
        if max_order_contracts is not None and qty > max_order_contracts:
            qty = self.spec.round_qty_down(max_order_contracts)
        qty = self.spec.round_qty_down(qty)
        if qty < self.spec.min_qty:
            return RebalancePlan(decision, target, delta, None, "delta below min order size")
        if side is OrderSide.BUY and qty > short_contracts:
            qty = short_contracts  # never flip: reduce-only BUY capped at the short size
            if qty <= ZERO:
                return RebalancePlan(decision, target, delta, None, "no short to reduce")
        intent = OrderIntent(
            side=side,
            qty=qty,
            reduce_only=side is OrderSide.BUY,
            reason=reason,
            target_ratio=decision.capped_target_ratio,
            current_ratio=current_ratio,
            target_contracts=target,
            current_short_contracts=short_contracts,
        )
        return RebalancePlan(decision, target, delta, intent)
