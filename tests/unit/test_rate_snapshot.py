"""Pre-settlement funding-rate capture — the only sound source when an environment publishes none."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from btc_inventory_quant.exchange.models import MarkPriceInfo
from btc_inventory_quant.execution.rate_snapshot import (
    RateSnapshot,
    SnapshotStoreError,
    append,
    capture,
    load,
    rates_for,
)

D = Decimal
SETTLE = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


class FakePremium:
    def __init__(
        self,
        rate: str | None = "0.0000927",
        next_time: datetime | None = SETTLE,
        environment: str = "demo",
    ) -> None:
        self._rate = None if rate is None else D(rate)
        self._next = next_time
        self.environment = environment

    def premium_index(self, symbol: str) -> MarkPriceInfo:
        return MarkPriceInfo(
            symbol=symbol,
            mark_price=D("77273.6"),
            index_price=D("77270.0"),
            last_funding_rate=self._rate,
            next_funding_time=self._next,
            time=SETTLE - timedelta(minutes=5),
        )


def _snap(captured_minutes_before: int, rate: str = "0.0000927") -> RateSnapshot:
    return RateSnapshot(
        symbol="BTCUSD_PERP",
        environment="demo",
        funding_time=SETTLE,
        rate=D(rate),
        mark_price=D("77273.6"),
        captured_at=SETTLE - timedelta(minutes=captured_minutes_before),
    )


def test_capture_reads_the_upcoming_rate_and_settlement() -> None:
    s = capture(FakePremium(), "BTCUSD_PERP", now=SETTLE - timedelta(minutes=4))  # type: ignore[arg-type]
    assert s.rate == D("0.0000927")
    assert s.funding_time == SETTLE
    assert s.environment == "demo"
    assert s.seconds_before_settlement == 240


def test_capture_refuses_without_a_next_funding_time() -> None:
    with pytest.raises(SnapshotStoreError, match="nextFundingTime"):
        capture(FakePremium(next_time=None), "BTCUSD_PERP")  # type: ignore[arg-type]


def test_capture_refuses_without_a_rate_rather_than_guessing() -> None:
    """A guessed rate would defeat the whole verification, so absence must be an error."""
    with pytest.raises(SnapshotStoreError, match="nothing to capture"):
        capture(FakePremium(rate=None), "BTCUSD_PERP")  # type: ignore[arg-type]


def test_round_trip_through_the_store(tmp_path: Path) -> None:
    p = tmp_path / "snaps.json"
    append(_snap(5), p)
    (loaded,) = load(p)
    assert loaded.rate == D("0.0000927")
    assert loaded.funding_time == SETTLE
    assert loaded.seconds_before_settlement == 300


def test_a_later_capture_replaces_an_earlier_one_for_the_same_settlement(tmp_path: Path) -> None:
    """Closer to the boundary is a better estimate of the rate that will be applied."""
    p = tmp_path / "snaps.json"
    append(_snap(60, rate="0.0001"), p)
    out = append(_snap(3, rate="0.0000927"), p)
    assert len(out) == 1
    assert out[0].rate == D("0.0000927")
    assert out[0].seconds_before_settlement == 180


def test_an_earlier_capture_does_not_overwrite_a_later_one(tmp_path: Path) -> None:
    p = tmp_path / "snaps.json"
    append(_snap(3, rate="0.0000927"), p)
    out = append(_snap(60, rate="0.0001"), p)
    assert len(out) == 1
    assert out[0].rate == D("0.0000927")  # the closer sample survives


def test_different_settlements_are_kept_separately(tmp_path: Path) -> None:
    p = tmp_path / "snaps.json"
    append(_snap(5), p)
    later = RateSnapshot(
        symbol="BTCUSD_PERP",
        environment="demo",
        funding_time=SETTLE + timedelta(hours=8),
        rate=D("0.00005"),
        mark_price=D("77000"),
        captured_at=SETTLE + timedelta(hours=7, minutes=55),
    )
    out = append(later, p)
    assert len(out) == 2
    assert [s.funding_time for s in out] == [SETTLE, SETTLE + timedelta(hours=8)]


def test_rates_for_filters_by_symbol_and_environment(tmp_path: Path) -> None:
    p = tmp_path / "snaps.json"
    append(_snap(5), p)
    other_env = RateSnapshot(
        symbol="BTCUSD_PERP",
        environment="production",
        funding_time=SETTLE,
        rate=D("0.0000317"),
        mark_price=D("77273.6"),
        captured_at=SETTLE - timedelta(minutes=5),
    )
    append(other_env, p)
    snaps = load(p)
    demo = rates_for(snaps, "BTCUSD_PERP", "demo")
    prod = rates_for(snaps, "BTCUSD_PERP", "production")
    assert demo[SETTLE][0] == D("0.0000927")
    assert prod[SETTLE][0] == D("0.0000317")
    assert rates_for(snaps, "ETHUSD_PERP", "demo") == {}


def test_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    assert load(tmp_path / "nope.json") == []


def test_a_non_array_store_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "snaps.json"
    p.write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(SnapshotStoreError, match="not a JSON array"):
        load(p)
