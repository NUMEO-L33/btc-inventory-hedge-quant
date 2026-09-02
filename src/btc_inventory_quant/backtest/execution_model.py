"""Backtest execution model: fill price with slippage, fee tier, partial fills, missed orders.

Deterministic given ``seed`` (``random.Random`` — no global state).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import Decimal

from btc_inventory_quant.config.schema import CostModelConfig
from btc_inventory_quant.domain.inverse_contract import ZERO, ContractSpec, OrderSide

BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class FillPlan:
    fill_qty: Decimal
    remainder_qty: Decimal
    fill_price: Decimal
    fee_rate: Decimal
    is_partial: bool


class ExecutionModel:
    def __init__(self, cost: CostModelConfig, spec: ContractSpec, seed: int) -> None:
        self.cost = cost
        self.spec = spec
        self._rng = random.Random(seed)
        self.rebalance_count = 0
        self.missed_count = 0
        self.partial_count = 0

    # --- parameters ----------------------------------------------------------
    @property
    def fee_rate(self) -> Decimal:
        base = self.cost.maker_fee_rate if self.cost.use_maker else self.cost.taker_fee_rate
        return base * self.cost.fee_multiplier

    @property
    def slippage_rate(self) -> Decimal:
        return self.cost.slippage_bps * self.cost.slippage_multiplier / BPS

    # --- behaviour -----------------------------------------------------------
    def fill_price(self, side: OrderSide, reference_price: Decimal) -> Decimal:
        """BUY pays up, SELL receives less; rounded to the price tick (adverse direction)."""
        s = self.slippage_rate
        raw = (
            reference_price * (Decimal(1) + s)
            if side is OrderSide.BUY
            else reference_price * (Decimal(1) - s)
        )
        from decimal import ROUND_CEILING, ROUND_FLOOR

        return self.spec.round_price(
            raw, rounding=ROUND_CEILING if side is OrderSide.BUY else ROUND_FLOOR
        )

    def next_rebalance_is_missed(self) -> bool:
        """Call once per generated rebalance order; returns True if this order is lost."""
        self.rebalance_count += 1
        idx = self.rebalance_count
        every = self.cost.missed_rebalance_every_n
        missed = idx in self.cost.missed_rebalance_indices or (every > 0 and idx % every == 0)
        if missed:
            self.missed_count += 1
        return missed

    def plan_fill(self, side: OrderSide, qty: Decimal, reference_price: Decimal) -> FillPlan:
        price = self.fill_price(side, reference_price)
        fill_qty = qty
        partial = False
        if self.cost.partial_fill_probability > ZERO and self._rng.random() < float(
            self.cost.partial_fill_probability
        ):
            filled = Decimal(math.ceil(float(qty * self.cost.partial_fill_ratio)))
            filled = max(self.spec.min_qty, min(qty, self.spec.round_qty_down(filled)))
            if filled < qty:
                fill_qty = filled
                partial = True
                self.partial_count += 1
        return FillPlan(
            fill_qty=fill_qty,
            remainder_qty=qty - fill_qty,
            fill_price=price,
            fee_rate=self.fee_rate,
            is_partial=partial,
        )
