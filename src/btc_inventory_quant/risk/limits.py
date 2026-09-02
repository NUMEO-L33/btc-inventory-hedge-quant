"""Pure risk-limit arithmetic. The numbers live ONLY in :class:`RiskLimits` (config)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btc_inventory_quant.config.schema import RiskLimits
from btc_inventory_quant.domain.inverse_contract import (
    ZERO,
    ContractSpec,
    InverseMathError,
    OrderSide,
)


class RiskLimitViolation(Exception):
    """A pre-trade or state check failed. Carries a machine-readable ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def clamp_hedge_ratio(ratio: Decimal, limits: RiskLimits, *, bypass_cap: bool = False) -> Decimal:
    """Clamp a raw target into [min_hedge_ratio, max_hedge_ratio] (benchmarks may bypass the cap, never below 0 or above 1)."""
    upper = Decimal("1") if bypass_cap else limits.max_hedge_ratio
    return max(limits.min_hedge_ratio, min(upper, ratio))


def apply_daily_change_cap(
    current_ratio: Decimal, desired_ratio: Decimal, limits: RiskLimits
) -> Decimal:
    """Limit |Δ| per decision day to ``max_daily_hedge_change``."""
    delta = desired_ratio - current_ratio
    cap = limits.max_daily_hedge_change
    if delta > cap:
        return current_ratio + cap
    if delta < -cap:
        return current_ratio - cap
    return desired_ratio


def rebalance_needed(current_ratio: Decimal, target_ratio: Decimal, limits: RiskLimits) -> bool:
    """Trade only when the difference is at least ``min_rebalance_diff`` (percentage points)."""
    return abs(target_ratio - current_ratio) >= limits.min_rebalance_diff


@dataclass(frozen=True, slots=True)
class HedgeDecision:
    current_ratio: Decimal
    raw_target_ratio: Decimal
    clamped_target_ratio: Decimal
    capped_target_ratio: Decimal
    rebalance: bool

    @property
    def executable_target(self) -> Decimal:
        return self.capped_target_ratio if self.rebalance else self.current_ratio


def resolve_hedge_target(
    current_ratio: Decimal, raw_target: Decimal, limits: RiskLimits, *, bypass_cap: bool = False
) -> HedgeDecision:
    """clamp → daily cap → min-diff filter. Pure function used by backtest and live alike."""
    clamped = clamp_hedge_ratio(raw_target, limits, bypass_cap=bypass_cap)
    capped = apply_daily_change_cap(current_ratio, clamped, limits)
    needed = rebalance_needed(current_ratio, capped, limits)
    # Special case: if the step is capped below min diff but the full move is wanted, still allow
    # as long as the capped step itself satisfies the min-diff rule (it always does when cap ≥ min diff).
    return HedgeDecision(current_ratio, raw_target, clamped, capped, needed)


def validate_order_against_position(
    side: OrderSide,
    qty: Decimal,
    short_contracts: Decimal,
    equity_btc: Decimal,
    mark_price: Decimal,
    spec: ContractSpec,
    limits: RiskLimits,
    *,
    bypass_cap: bool = False,
) -> None:
    """Hard pre-trade checks shared by backtest execution and live order manager.

    * no long positions: BUY qty must be ≤ current short (reduce-only semantics)
    * SELL must not push |short| × CV above equity × max_short_notional_to_equity (leverage ≤ 1)
    * SELL must not push the effective hedge ratio above max_hedge_ratio (+ tolerance)
    """
    if qty <= ZERO:
        raise RiskLimitViolation("QTY_NOT_POSITIVE", f"qty={qty}")
    try:
        spec.validate_qty(qty, market=True)
    except InverseMathError as exc:
        raise RiskLimitViolation("QTY_FILTER", str(exc)) from exc
    if equity_btc <= ZERO:
        raise RiskLimitViolation("NON_POSITIVE_EQUITY", f"equity_btc={equity_btc}")
    if side is OrderSide.BUY:
        if qty > short_contracts:
            raise RiskLimitViolation(
                "BUY_EXCEEDS_SHORT", f"BUY {qty} > short {short_contracts}: would open a LONG"
            )
        return
    new_short = short_contracts + qty
    new_notional_usd = new_short * spec.contract_value_usd
    equity_usd = equity_btc * mark_price
    if new_notional_usd > equity_usd * limits.max_short_notional_to_equity:
        raise RiskLimitViolation(
            "NOTIONAL_EXCEEDS_EQUITY",
            f"short notional {new_notional_usd} USD > equity {equity_usd} USD × {limits.max_short_notional_to_equity}",
        )
    cap = Decimal("1") if bypass_cap else limits.max_hedge_ratio + limits.max_hedge_ratio_tolerance
    new_ratio = new_notional_usd / equity_usd
    if new_ratio > cap:
        raise RiskLimitViolation("HEDGE_CAP", f"resulting hedge ratio {new_ratio:.4f} > cap {cap}")
