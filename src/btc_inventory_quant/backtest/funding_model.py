"""Funding settlement model for the daily backtest.

Each funding event (00:00 / 08:00 / 16:00 UTC, taken from the actual ``fundingRate`` history — no
synthetic schedule) is settled on the position held at that instant:

* 00:00 event → settled BEFORE the day's execution (position carried overnight)
* 08:00 / 16:00 events → settled AFTER the day's execution

Mark price at the event, in priority order:

1. ``record``    — the ``markPrice`` field of the funding record (Binance populates it from 2023-11-01);
2. ``kline_8h``  — the open of the 8h mark-price kline opening at that exact settlement instant,
   when the optional ``mark_price_klines_8h`` dataset was downloaded (``--include-8h-mark``);
3. ``daily_*``   — the documented approximation from the daily mark kline: open for 00:00, linear
   interpolation open→close for 08:00 (1/3) and 16:00 (2/3).

Step 2 exists because step 3 covers 53.2% of the settlement history. Measured on the era where the
true mark IS known, that interpolation is off by 0.52% per settlement on average but only +0.0096%
cumulatively — so this is a precision fix, not a results-changing one. See
:mod:`btc_inventory_quant.data.settlement_marks`.

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
    mark_source: str  # "record" | "kline_8h" | "daily_open" | "daily_interp"


class FundingModel:
    def __init__(
        self,
        calendar: FundingCalendar,
        cost: CostModelConfig,
        settlement_marks: dict[datetime, Decimal] | None = None,
    ) -> None:
        self.calendar = calendar
        self.cost = cost
        self.settlement_marks = settlement_marks or {}
        self.approximated_count = 0
        self.exact_count = 0
        self.kline_8h_count = 0

    def _effective_rate(self, rate: Decimal) -> Decimal:
        return rate - self.cost.funding_adverse_bps_per_period / Decimal(10000)

    def settlements_for_day(
        self, day: date, mark_open: Decimal, mark_close: Decimal
    ) -> list[FundingSettlement]:
        out: list[FundingSettlement] = []
        for ev in self.calendar.events_on(day):
            kline_mark = self.settlement_marks.get(ev.funding_time)
            if ev.mark_price is not None:
                mark, source = ev.mark_price, "record"
                self.exact_count += 1
            elif kline_mark is not None:
                mark, source = kline_mark, "kline_8h"
                self.kline_8h_count += 1
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
