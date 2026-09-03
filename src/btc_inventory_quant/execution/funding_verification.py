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
    def report_quantum(self) -> Decimal:
        """Smallest amount the exchange's own report can express.

        Measured 2026-09-03: a settlement modelled at 0.0000001294 BTC was reported as 0.00000012 —
        the exchange truncates to 8 decimal places. On a 1-contract position that quantisation is
        7.7% of the whole amount, so a fixed 0.5% tolerance can NEVER be met however correct the
        formula is. The reported value's own exponent gives the quantum, so the check can allow
        exactly the precision the exchange actually offers and no more.
        """
        return Decimal(1).scaleb(self.actual_btc.as_tuple().exponent)  # type: ignore[arg-type]

    @property
    def allowed_difference(self) -> Decimal:
        """Tolerance in BTC: the relative band, or the reporting quantum when that is coarser."""
        return max(TOLERANCE * abs(self.computed_btc), self.report_quantum)

    @property
    def limited_by_precision(self) -> bool:
        """True when the exchange's reporting precision, not the model, sets the bar."""
        return self.report_quantum > TOLERANCE * abs(self.computed_btc)

    @property
    def resolution_pct(self) -> Decimal | None:
        """How tightly this settlement can test the formula at all, in percent."""
        if self.computed_btc == ZERO:
            return None
        return self.report_quantum / abs(self.computed_btc) * 100

    @property
    def ok(self) -> bool:
        if self.computed_btc == ZERO:
            return False
        return abs(self.difference_btc) <= self.allowed_difference

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
            "report_quantum_btc": str(self.report_quantum),
            "allowed_difference_btc": str(self.allowed_difference),
            "limited_by_report_precision": self.limited_by_precision,
            "resolution_pct": None if self.resolution_pct is None else f"{self.resolution_pct:.2f}",
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class FundingVerification:
    checks: tuple[SettlementCheck, ...]
    window_hours: int
    symbol: str
    environment: str
    # diagnostics — a NO_DATA verdict has to say WHICH side was empty, otherwise the operator
    # cannot tell "no settlement happened" from "the rate endpoint returned nothing"
    income_times: tuple[datetime, ...] = ()
    income_amounts: tuple[Decimal, ...] = ()
    rate_times: tuple[datetime, ...] = ()
    rate_source: str = ""
    environments_agree: bool | None = None
    mark_here: Decimal | None = None
    mark_production: Decimal | None = None
    skipped_no_rate: tuple[datetime, ...] = ()
    skipped_no_position: tuple[datetime, ...] = ()

    @property
    def diagnosis(self) -> str:
        if self.checks:
            return "settlements matched"
        if not self.income_times:
            return (
                "no FUNDING_FEE income in the window — either no settlement occurred while a "
                "position was open, or the window is too short"
            )
        if self.skipped_no_rate:
            return (
                f"{len(self.skipped_no_rate)} settlement(s) found in income, but fundingRate "
                f"history returned no matching rate ({len(self.rate_times)} rate row(s) in the "
                "window). The demo fundingRate endpoint may be sparse or lag the settlement — "
                "retry in a few minutes, or widen --window-hours"
            )
        if self.skipped_no_position:
            return (
                "settlements and rates matched, but the position size could not be determined "
                "(flat position now?). Re-run with --contracts <signed size held at settlement>"
            )
        return "unknown"

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
            "weakest_resolution_pct": (
                None
                if not self.checks
                else f"{max((c.resolution_pct or ZERO) for c in self.checks):.2f}"
            ),
            "precision_note": (
                None
                if not any(c.limited_by_precision for c in self.checks)
                else (
                    "at least one settlement is limited by the exchange's reporting precision "
                    "rather than by the model. A larger position raises the resolution: the "
                    "funding amount grows with contracts while the reported quantum stays fixed."
                )
            ),
            "formula": "funding_btc = -sign(c) * |c| * contract_value_usd / mark_price * rate",
            "resolves_partial": self.verdict == "VERIFIED" and self.samples >= 1,
            "diagnosis": self.diagnosis,
            "rate_source": self.rate_source,
            "rate_source_caveat": (
                None
                if self.rate_source in ("", self.environment)
                else (
                    f"rates taken from '{self.rate_source}' because '{self.environment}' publishes "
                    "no funding history. environments_agree compares the MARK PRICE only — two "
                    "venues can quote the same mark and still run different funding rates, which "
                    "is exactly what happened on 2026-09-03 (demo settled at ~2.9x production's "
                    "rate). Prefer 'verify-funding --snapshot-rate' shortly before a settlement, "
                    "which captures the rate from this same environment"
                )
            ),
            "environments_agree": self.environments_agree,
            "mark_configured": None if self.mark_here is None else str(self.mark_here),
            "mark_production": None if self.mark_production is None else str(self.mark_production),
            "diagnostics": {
                "income_records": [t.isoformat() for t in self.income_times],
                "income_amounts_btc": [str(a) for a in self.income_amounts],
                "rate_rows": [t.isoformat() for t in self.rate_times],
                "skipped_no_rate": [t.isoformat() for t in self.skipped_no_rate],
                "skipped_no_position": [t.isoformat() for t in self.skipped_no_position],
            },
            "settlements": [c.to_dict() for c in self.checks],
        }


def _parse_rate_rows(
    rows: list[dict[str, Any]],
) -> dict[datetime, tuple[Decimal, Decimal | None]]:
    out: dict[datetime, tuple[Decimal, Decimal | None]] = {}
    for row in rows:
        ts = normalise_funding_time(int(str(row["fundingTime"])))
        mark_raw = row.get("markPrice")
        mark = None if mark_raw in (None, "", "0") else Decimal(str(mark_raw))
        out[ts] = (Decimal(str(row["fundingRate"])), mark)
    return out


def _production_rates(
    symbol: str, start_ms: int, end_ms: int
) -> dict[datetime, tuple[Decimal, Decimal | None]]:
    """Public funding history from the production host, for environments that publish none.

    Measured on 2026-09-03: the demo host settles funding (the FUNDING_FEE income record exists)
    but ``GET /dapi/v1/fundingRate`` returns an empty list there, with or without a time filter.
    The rate is public market data, so production can supply it — but ONLY if the two environments
    price the same instrument the same way, which :func:`environments_agree` measures rather than
    assumes. No credentials are used for this call.
    """
    from btc_inventory_quant.exchange.binance_coinm import BinanceCoinMAdapter

    public = BinanceCoinMAdapter(environment="production")
    return _parse_rate_rows(public.funding_rate_history(symbol, start_ms=start_ms, end_ms=end_ms))


def environments_agree(
    exchange: ExchangeAdapter, symbol: str, tolerance: Decimal = Decimal("0.002")
) -> tuple[bool, Decimal, Decimal]:
    """Do the configured environment and production quote the same mark right now?

    Substituting a production funding rate into a demo settlement is only sound if the two price
    the same instrument alike. This compares the live marks and returns (agree, configured, prod);
    a disagreement means the substitution must not be trusted.
    """
    from btc_inventory_quant.exchange.binance_coinm import BinanceCoinMAdapter

    here = exchange.premium_index(symbol).mark_price
    there = BinanceCoinMAdapter(environment="production").premium_index(symbol).mark_price
    if here <= ZERO or there <= ZERO:
        return False, here, there
    return abs(here - there) / there <= tolerance, here, there


def _rates_and_marks(
    exchange: ExchangeAdapter, symbol: str, start_ms: int, end_ms: int
) -> dict[datetime, tuple[Decimal, Decimal | None]]:
    """Settled rates in the window.

    Some environments (the demo host in particular) return nothing when ``startTime``/``endTime``
    are supplied, or publish the row for a just-completed settlement only after a delay. When the
    windowed query comes back empty, retry WITHOUT the time filter — the endpoint then returns the
    most recent rows — and filter client-side. Same endpoint and same data either way, so this is
    a pagination workaround, not a change of source.
    """
    windowed = _parse_rate_rows(
        exchange.funding_rate_history(symbol, start_ms=start_ms, end_ms=end_ms)
    )
    if windowed:
        return windowed
    log.warning("funding_rate_window_empty_retrying_unfiltered", extra={"symbol": symbol})
    recent = _parse_rate_rows(exchange.funding_rate_history(symbol, limit=200))
    lo = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    hi = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    return {ts: v for ts, v in recent.items() if lo <= ts <= hi}


def _resolve_rates(
    exchange: ExchangeAdapter,
    symbol: str,
    start_ms: int,
    end_ms: int,
    allow_production_rates: bool,
) -> tuple[dict[datetime, tuple[Decimal, Decimal | None]], str]:
    """Rates in priority order: the environment's own history > pre-settlement snapshots of that
    same environment > production (opt-in, cross-environment, weakest)."""
    rates = _rates_and_marks(exchange, symbol, start_ms, end_ms)
    if rates or exchange.environment == "production":
        return rates, exchange.environment

    from btc_inventory_quant.execution import rate_snapshot

    lo = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    hi = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    snapped = {
        ts: v
        for ts, v in rate_snapshot.rates_for(
            rate_snapshot.load(), symbol, exchange.environment
        ).items()
        if lo <= ts <= hi
    }
    if snapped:
        log.info("funding_rate_from_pre_settlement_snapshot", extra={"count": len(snapped)})
        return snapped, f"{exchange.environment} (captured before settlement)"

    if not allow_production_rates:
        return {}, exchange.environment
    log.warning("funding_rate_absent_falling_back_to_production", extra={"symbol": symbol})
    return _production_rates(symbol, start_ms, end_ms), "production"


def verify_funding(
    exchange: ExchangeAdapter,
    spec: ContractSpec,
    symbol: str,
    window_hours: int = 48,
    contracts: Decimal | None = None,
    contracts_at: dict[datetime, Decimal] | None = None,
    now: datetime | None = None,
    allow_production_rates: bool = False,
) -> FundingVerification:
    """Compare every FUNDING_FEE in the window against the modelled amount."""
    end = now or datetime.now(tz=UTC)
    start = end - timedelta(hours=window_hours)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    records: list[IncomeRecord] = exchange.income(
        symbol=symbol, income_type="FUNDING_FEE", start_ms=start_ms, end_ms=end_ms
    )
    rates, rate_source = _resolve_rates(exchange, symbol, start_ms, end_ms, allow_production_rates)
    agree: bool | None = None
    mark_here: Decimal | None = None
    mark_prod: Decimal | None = None
    if rate_source != exchange.environment:
        agree, mark_here, mark_prod = environments_agree(exchange, symbol)

    fallback_contracts: Decimal | None = contracts
    fallback_source = "cli"
    if fallback_contracts is None:
        live = [p for p in exchange.positions(pair=None) if p.symbol == symbol]
        if live:
            fallback_contracts = live[0].contracts
            fallback_source = "current exchange position (assumes it did not change)"

    checks: list[SettlementCheck] = []
    income_times: list[datetime] = []
    income_amounts: list[Decimal] = []
    skipped_no_rate: list[datetime] = []
    skipped_no_position: list[datetime] = []
    for r in records:
        if r.time is None:
            continue
        ts = r.time.replace(minute=0, second=0, microsecond=0)
        income_times.append(ts)
        income_amounts.append(r.amount)
        if ts not in rates:
            log.warning("funding_verify_no_rate", extra={"funding_time": ts.isoformat()})
            skipped_no_rate.append(ts)
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
            skipped_no_position.append(ts)
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
        income_times=tuple(sorted(income_times)),
        income_amounts=tuple(income_amounts),
        rate_times=tuple(sorted(rates)),
        rate_source=rate_source,
        environments_agree=agree,
        mark_here=mark_here,
        mark_production=mark_prod,
        skipped_no_rate=tuple(sorted(skipped_no_rate)),
        skipped_no_position=tuple(sorted(skipped_no_position)),
    )


__all__ = ["TOLERANCE", "FundingVerification", "SettlementCheck", "verify_funding"]
