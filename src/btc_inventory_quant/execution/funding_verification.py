"""Resolve the PARTIAL funding formula against real settlements.

``docs/inverse_math.md`` §6 and ``docs/exchange_api_verification.md`` record that the COIN-M
funding *amount* formula is only PARTIAL-verified: the official documentation states the funding
rate and the settlement schedule, but the "÷ Mark Price" denominator for inverse contracts was
never found stated in the COIN-M reference. Everything this project models about funding rests on

    funding_btc = -sign(contracts) × |contracts| × contract_value_usd / mark_price × rate

and funding carry is the ONE effect that survived all four research rounds
(``docs/strategy_v4_control_arms.md``). If this formula is wrong, that finding is wrong.

The only way to settle it is to hold a real position through a real settlement and compare the
exchange's own ``FUNDING_FEE`` income record with the modelled amount. This module does that
comparison; ``python -m btc_inventory_quant verify-funding`` drives it.

Position size at each settlement is the one input the income record does not carry. It is taken,
in order of preference, from an explicit ``--contracts``, then from the position snapshot stored
closest before each settlement, then from the current exchange position (only sound if the
position has not changed during the window — the report says which source was used).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from btc_inventory_quant.data.funding_data import normalise_funding_time
from btc_inventory_quant.domain.inverse_contract import ZERO, ContractSpec, funding_payment_btc
from btc_inventory_quant.exchange.base import ExchangeAdapter
from btc_inventory_quant.exchange.models import IncomeRecord
from btc_inventory_quant.monitoring.logging import get_logger

log = get_logger(__name__)

TOLERANCE = Decimal("0.005")  # 0.5% — a formula error shows up far larger than rounding


@dataclass(frozen=True, slots=True)
class SettlementCheck:
    funding_time: datetime
    rate: Decimal
    mark_price: Decimal
    mark_source: str
    contracts: Decimal
    contracts_source: str
    actual_btc: Decimal
    computed_btc: Decimal

    @property
    def difference_btc(self) -> Decimal:
        return self.actual_btc - self.computed_btc

    @property
    def ratio(self) -> Decimal | None:
        return None if self.computed_btc == ZERO else self.actual_btc / self.computed_btc

    @property
    def ok(self) -> bool:
        r = self.ratio
        return r is not None and abs(r - 1) <= TOLERANCE

    def to_dict(self) -> dict[str, Any]:
        return {
            "funding_time": self.funding_time.isoformat(),
            "rate": str(self.rate),
            "mark_price": str(self.mark_price),
            "mark_source": self.mark_source,
            "contracts": str(self.contracts),
            "contracts_source": self.contracts_source,
            "actual_btc": str(self.actual_btc),
            "computed_btc": str(self.computed_btc),
            "difference_btc": str(self.difference_btc),
            "ratio": None if self.ratio is None else str(self.ratio),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class FundingVerification:
    checks: tuple[SettlementCheck, ...]
    window_hours: int
    symbol: str
    environment: str

    @property
    def samples(self) -> int:
        return len(self.checks)

    @property
    def mean_ratio(self) -> Decimal | None:
        ratios = [c.ratio for c in self.checks if c.ratio is not None]
        return None if not ratios else sum(ratios, ZERO) / len(ratios)

    @property
    def verdict(self) -> str:
        if self.samples == 0:
            return "NO_DATA"
        if all(c.ok for c in self.checks):
            return "VERIFIED"
        if any(c.ok for c in self.checks):
            return "MIXED"
        return "MISMATCH"

    def to_dict(self) -> dict[str, Any]:
        mean = self.mean_ratio
        return {
            "symbol": self.symbol,
            "environment": self.environment,
            "window_hours": self.window_hours,
            "samples": self.samples,
            "mean_ratio": None if mean is None else str(mean),
            "tolerance": str(TOLERANCE),
            "verdict": self.verdict,
            "formula": "funding_btc = -sign(c) * |c| * contract_value_usd / mark_price * rate",
            "resolves_partial": self.verdict == "VERIFIED" and self.samples >= 1,
            "settlements": [c.to_dict() for c in self.checks],
        }


def _rates_and_marks(
    exchange: ExchangeAdapter, symbol: str, start_ms: int, end_ms: int
) -> dict[datetime, tuple[Decimal, Decimal | None]]:
    out: dict[datetime, tuple[Decimal, Decimal | None]] = {}
    for row in exchange.funding_rate_history(symbol, start_ms=start_ms, end_ms=end_ms):
        ts = normalise_funding_time(int(str(row["fundingTime"])))
        mark_raw = row.get("markPrice")
        mark = None if mark_raw in (None, "", "0") else Decimal(str(mark_raw))
        out[ts] = (Decimal(str(row["fundingRate"])), mark)
    return out


def verify_funding(
    exchange: ExchangeAdapter,
    spec: ContractSpec,
    symbol: str,
    window_hours: int = 48,
    contracts: Decimal | None = None,
    contracts_at: dict[datetime, Decimal] | None = None,
    now: datetime | None = None,
) -> FundingVerification:
    """Compare every FUNDING_FEE in the window against the modelled amount."""
    end = now or datetime.now(tz=UTC)
    start = end - timedelta(hours=window_hours)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    records: list[IncomeRecord] = exchange.income(
        symbol=symbol, income_type="FUNDING_FEE", start_ms=start_ms, end_ms=end_ms
    )
    rates = _rates_and_marks(exchange, symbol, start_ms, end_ms)

    fallback_contracts: Decimal | None = contracts
    fallback_source = "cli"
    if fallback_contracts is None:
        live = [p for p in exchange.positions(pair=None) if p.symbol == symbol]
        if live:
            fallback_contracts = live[0].contracts
            fallback_source = "current exchange position (assumes it did not change)"

    checks: list[SettlementCheck] = []
    for r in records:
        if r.time is None:
            continue
        ts = r.time.replace(minute=0, second=0, microsecond=0)
        if ts not in rates:
            log.warning("funding_verify_no_rate", extra={"funding_time": ts.isoformat()})
            continue
        rate, mark = rates[ts]
        if mark is None:
            mark = exchange.premium_index(symbol).mark_price
            mark_source = "premium_index (record carried no markPrice)"
        else:
            mark_source = "fundingRate record"

        size, size_source = fallback_contracts, fallback_source
        if contracts_at and ts in contracts_at:
            size, size_source = contracts_at[ts], "stored position snapshot"
        if size is None or size == ZERO:
            log.warning("funding_verify_no_position", extra={"funding_time": ts.isoformat()})
            continue

        checks.append(
            SettlementCheck(
                funding_time=ts,
                rate=rate,
                mark_price=mark,
                mark_source=mark_source,
                contracts=size,
                contracts_source=size_source,
                actual_btc=r.amount,
                computed_btc=funding_payment_btc(size, spec.contract_value_usd, mark, rate),
            )
        )
    checks.sort(key=lambda c: c.funding_time)
    return FundingVerification(
        checks=tuple(checks),
        window_hours=window_hours,
        symbol=symbol,
        environment=exchange.environment,
    )


__all__ = ["TOLERANCE", "FundingVerification", "SettlementCheck", "verify_funding"]
