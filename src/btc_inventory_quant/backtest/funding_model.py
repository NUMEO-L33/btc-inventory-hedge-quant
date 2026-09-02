"""Funding settlement model for the daily backtest.

Each funding event (00:00 / 08:00 / 16:00 UTC, taken from the actual ``fundingRate`` history — no
synthetic schedule) is settled on the position held at that instant:

* 00:00 event → settled BEFORE the day's execution (position carried overnight)
* 08:00 / 16:00 events → settled AFTER the day's execution

Mark price at the event: the ``markPrice`` field of the funding record when present
(Binance populates it from 2023-11-01); otherwise a documented approximation from the daily mark
kline — open for 00:00, linear interpolation open→close for 08:00 (1/3) and 16:00 (2/3).

Stress: ``funding_adverse_bps_per_period`` lowers every rate (a short then receives less / pays more).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from btc_inventory_quant.config.schema import CostModelConfig
from btc_inventory_quant.data.funding_data import FundingCalendar


@dataclass(frozen=True, slots=True)
class FundingSettlement:
    ts: datetime
    rate: Decimal
    effective_rate: Decimal
    mark_price: Decimal
    mark_source: str  # "record" | "daily_open" | "daily_interp"


class FundingModel:
    def __init__(self, calendar: FundingCalendar, cost: CostModelConfig) -> None:
        self.calendar = calendar
        self.cost = cost
        self.approximated_count = 0
        self.exact_count = 0

    def _effective_rate(self, rate: Decimal) -> Decimal:
        return rate - self.cost.funding_adverse_bps_per_period / Decimal(10000)

    def settlements_for_day(
        self, day: date, mark_open: Decimal, mark_close: Decimal
    ) -> list[FundingSettlement]:
        out: list[FundingSettlement] = []
        for ev in self.calendar.events_on(day):
            if ev.mark_price is not None:
                mark, source = ev.mark_price, "record"
                self.exact_count += 1
            else:
                self.approximated_count += 1
                frac = Decimal(ev.hour) / Decimal(24)
                if ev.hour == 0:
                    mark, source = mark_open, "daily_open"
                else:
                    mark, source = mark_open + (mark_close - mark_open) * frac, "daily_interp"
            out.append(
                FundingSettlement(
                    ts=ev.funding_time,
                    rate=ev.rate,
                    effective_rate=self._effective_rate(ev.rate),
                    mark_price=mark,
                    mark_source=source,
                )
            )
        return out
