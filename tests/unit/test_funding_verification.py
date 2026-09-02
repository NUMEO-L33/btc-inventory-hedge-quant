"""The check that resolves the PARTIAL funding formula — including that it FAILS when it should."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from btc_inventory_quant.domain.inverse_contract import ContractSpec, funding_payment_btc
from btc_inventory_quant.exchange.models import ExchangePosition, IncomeRecord, MarkPriceInfo
from btc_inventory_quant.execution.funding_verification import verify_funding

D = Decimal
NOW = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)
CV = D("100")  # BTCUSD_PERP contractSize, as read from the exchange_info fixture


class FakeExchange:
    """Minimal adapter stub: settlements, rates and one open position."""

    name = "fake"
    environment = "demo"

    def __init__(
        self,
        settlements: list[tuple[datetime, str, str, str]],  # (ts, rate, mark, actual_btc)
        contracts: str = "-10",
        drop_mark: bool = False,
        live_mark: str = "60000",
    ) -> None:
        self.settlements = settlements
        self._contracts = D(contracts)
        self.drop_mark = drop_mark
        self.live_mark = D(live_mark)

    @property
    def supports_trading(self) -> bool:
        return True

    def income(
        self,
        symbol: str | None = None,
        income_type: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[IncomeRecord]:
        return [
            IncomeRecord(
                income_type="FUNDING_FEE",
                amount=D(actual),
                asset="BTC",
                time=ts,
                tran_id=str(i),
                symbol="BTCUSD_PERP",
                info="",
            )
            for i, (ts, _rate, _mark, actual) in enumerate(self.settlements)
        ]

    def funding_rate_history(
        self,
        symbol: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return [
            {
                "symbol": symbol,
                "fundingTime": int(ts.timestamp() * 1000) + 3,  # jitter, must be normalised
                "fundingRate": rate,
                "markPrice": "" if self.drop_mark else mark,
            }
            for ts, rate, mark, _actual in self.settlements
        ]

    def positions(self, pair: str | None = None) -> list[ExchangePosition]:
        return [
            ExchangePosition(
                symbol="BTCUSD_PERP",
                contracts=self._contracts,
                entry_price=D("60000"),
                mark_price=self.live_mark,
                unrealized_pnl=D("0"),
                liquidation_price=None,
                leverage=1,
                margin_type="CROSSED",
                position_side="BOTH",
                update_time=NOW,
            )
        ]

    def premium_index(self, symbol: str) -> MarkPriceInfo:
        return MarkPriceInfo(
            symbol=symbol,
            mark_price=self.live_mark,
            index_price=self.live_mark,
            last_funding_rate=D("0"),
            next_funding_time=None,
            time=NOW,
        )


def _truthful(
    ts: datetime, rate: str, mark: str, contracts: str = "-10"
) -> tuple[str, str, str, str]:
    """The exchange amount the documented formula predicts."""
    amount = funding_payment_btc(D(contracts), CV, D(mark), D(rate))
    return (ts, rate, mark, str(amount))  # type: ignore[return-value]


def test_matching_settlements_resolve_the_partial(spec: ContractSpec) -> None:
    ex = FakeExchange(
        [
            _truthful(NOW - timedelta(hours=10), "0.0001", "60000"),
            _truthful(NOW - timedelta(hours=2), "-0.00005", "61000"),
        ]
    )
    r = verify_funding(ex, spec, "BTCUSD_PERP", window_hours=48, now=NOW)  # type: ignore[arg-type]
    assert r.samples == 2
    assert r.verdict == "VERIFIED"
    assert r.to_dict()["resolves_partial"] is True
    assert all(c.ok for c in r.checks)
    assert r.mean_ratio is not None and abs(r.mean_ratio - 1) < D("0.001")


def test_a_wrong_formula_is_caught(spec: ContractSpec) -> None:
    """If the exchange divided by something else, the check must FAIL — the whole point."""
    ts = NOW - timedelta(hours=4)
    truthful = funding_payment_btc(D("-10"), CV, D("60000"), D("0.0001"))
    ex = FakeExchange([(ts, "0.0001", "60000", str(truthful * D("1.5")))])
    r = verify_funding(ex, spec, "BTCUSD_PERP", now=NOW)  # type: ignore[arg-type]
    assert r.verdict == "MISMATCH"
    assert r.to_dict()["resolves_partial"] is False
    assert r.mean_ratio is not None and abs(r.mean_ratio - D("1.5")) < D("0.001")


def test_partially_wrong_is_reported_as_mixed(spec: ContractSpec) -> None:
    good = _truthful(NOW - timedelta(hours=9), "0.0001", "60000")
    bad_ts = NOW - timedelta(hours=1)
    bad_amount = funding_payment_btc(D("-10"), CV, D("60000"), D("0.0001"))
    ex = FakeExchange([good, (bad_ts, "0.0001", "60000", str(bad_amount * 2))])
    r = verify_funding(ex, spec, "BTCUSD_PERP", now=NOW)  # type: ignore[arg-type]
    assert r.verdict == "MIXED"
    assert [c.ok for c in r.checks] == [True, False]


def test_no_settlements_is_no_data_not_success(spec: ContractSpec) -> None:
    r = verify_funding(FakeExchange([]), spec, "BTCUSD_PERP", now=NOW)  # type: ignore[arg-type]
    assert r.samples == 0
    assert r.verdict == "NO_DATA"
    assert r.to_dict()["resolves_partial"] is False


def test_timestamp_jitter_is_normalised_to_the_hour(spec: ContractSpec) -> None:
    ex = FakeExchange([_truthful(NOW - timedelta(hours=2), "0.0001", "60000")])
    r = verify_funding(ex, spec, "BTCUSD_PERP", now=NOW)  # type: ignore[arg-type]
    assert r.samples == 1  # the +3 ms jitter still matched the income record
    assert r.checks[0].funding_time.minute == 0


def test_explicit_contracts_override_the_live_position(spec: ContractSpec) -> None:
    ts = NOW - timedelta(hours=3)
    amount = funding_payment_btc(D("-25"), CV, D("60000"), D("0.0001"))
    ex = FakeExchange([(ts, "0.0001", "60000", str(amount))], contracts="-10")
    default = verify_funding(ex, spec, "BTCUSD_PERP", now=NOW)  # type: ignore[arg-type]
    assert default.verdict == "MISMATCH"  # live position is the wrong size
    fixed = verify_funding(ex, spec, "BTCUSD_PERP", contracts=D("-25"), now=NOW)  # type: ignore[arg-type]
    assert fixed.verdict == "VERIFIED"
    assert fixed.checks[0].contracts_source == "cli"


def test_snapshot_positions_take_priority_over_the_live_position(spec: ContractSpec) -> None:
    ts = NOW - timedelta(hours=3)
    amount = funding_payment_btc(D("-7"), CV, D("60000"), D("0.0001"))
    ex = FakeExchange([(ts, "0.0001", "60000", str(amount))], contracts="-10")
    r = verify_funding(
        ex,  # type: ignore[arg-type]
        spec,
        "BTCUSD_PERP",
        contracts_at={ts: D("-7")},
        now=NOW,
    )
    assert r.verdict == "VERIFIED"
    assert r.checks[0].contracts_source == "stored position snapshot"


def test_missing_record_mark_falls_back_to_premium_index(spec: ContractSpec) -> None:
    ts = NOW - timedelta(hours=3)
    amount = funding_payment_btc(D("-10"), CV, D("58000"), D("0.0001"))
    ex = FakeExchange([(ts, "0.0001", "58000", str(amount))], drop_mark=True, live_mark="58000")
    r = verify_funding(ex, spec, "BTCUSD_PERP", now=NOW)  # type: ignore[arg-type]
    assert r.verdict == "VERIFIED"
    assert "premium_index" in r.checks[0].mark_source


def test_a_flat_position_yields_no_samples(spec: ContractSpec) -> None:
    ex = FakeExchange([_truthful(NOW - timedelta(hours=2), "0.0001", "60000")], contracts="0")
    r = verify_funding(ex, spec, "BTCUSD_PERP", now=NOW)  # type: ignore[arg-type]
    assert r.verdict == "NO_DATA"


def test_tolerance_admits_rounding_but_not_a_formula_error(spec: ContractSpec) -> None:
    ts = NOW - timedelta(hours=2)
    exact = funding_payment_btc(D("-10"), CV, D("60000"), D("0.0001"))
    within = verify_funding(
        FakeExchange([(ts, "0.0001", "60000", str(exact * D("1.004")))]),  # type: ignore[arg-type]
        spec,
        "BTCUSD_PERP",
        now=NOW,
    )
    beyond = verify_funding(
        FakeExchange([(ts, "0.0001", "60000", str(exact * D("1.02")))]),  # type: ignore[arg-type]
        spec,
        "BTCUSD_PERP",
        now=NOW,
    )
    assert within.verdict == "VERIFIED"
    assert beyond.verdict == "MISMATCH"


def test_sign_convention_a_short_receives_when_the_rate_is_positive(spec: ContractSpec) -> None:
    """The direction that matters operationally: funding carry is why this strategy makes BTC."""
    ts = NOW - timedelta(hours=2)
    amount = funding_payment_btc(D("-10"), CV, D("60000"), D("0.0001"))
    assert amount > 0  # short + positive rate = receives
    r = verify_funding(
        FakeExchange([(ts, "0.0001", "60000", str(amount))]),  # type: ignore[arg-type]
        spec,
        "BTCUSD_PERP",
        now=NOW,
    )
    assert r.verdict == "VERIFIED"
    assert r.checks[0].actual_btc > 0


def test_report_dict_carries_the_formula_and_environment(spec: ContractSpec) -> None:
    ex = FakeExchange([_truthful(NOW - timedelta(hours=2), "0.0001", "60000")])
    d = verify_funding(ex, spec, "BTCUSD_PERP", now=NOW).to_dict()  # type: ignore[arg-type]
    assert d["environment"] == "demo"
    assert "mark_price" in d["formula"]
    assert d["settlements"][0]["contracts_source"].startswith("current exchange position")
    assert pytest.approx(float(d["settlements"][0]["difference_btc"]), abs=1e-12) == 0.0
