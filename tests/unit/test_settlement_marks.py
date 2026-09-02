"""Exact settlement marks from the optional 8h mark-kline dataset, and their priority order."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from btc_inventory_quant.backtest.funding_model import FundingModel
from btc_inventory_quant.config.schema import CostModelConfig
from btc_inventory_quant.data.funding_data import FundingCalendar, FundingEvent
from btc_inventory_quant.data.settlement_marks import (
    SettlementMarkError,
    load_settlement_marks,
    load_settlement_marks_if_present,
)

D = Decimal
DAY = datetime(2021, 6, 1, tzinfo=UTC)


def _write_pages(directory: Path, bars: list[tuple[datetime, str]]) -> None:
    """Write 8h mark klines as Binance returns them: [openTime, open, high, low, close, ...]."""
    directory.mkdir(parents=True, exist_ok=True)
    rows = [
        [int(ts.timestamp() * 1000), o, o, o, o, "0", int(ts.timestamp() * 1000) + 1, "0", 0]
        for ts, o in bars
    ]
    (directory / "page_001.json").write_text(json.dumps(rows), encoding="utf-8")


def _calendar(events: list[FundingEvent]) -> FundingCalendar:
    return FundingCalendar(events)


def _event(hour: int, rate: str, mark: str | None = None) -> FundingEvent:
    ts = DAY + timedelta(hours=hour)
    return FundingEvent(
        funding_time=ts,
        rate=D(rate),
        mark_price=D(mark) if mark is not None else None,
        raw_funding_time_ms=int(ts.timestamp() * 1000),
    )


# --------------------------------------------------------------------------- loading
def test_open_price_at_each_settlement_hour_is_loaded(tmp_path: Path) -> None:
    d = tmp_path / "mark_price_klines_8h"
    _write_pages(
        d,
        [(DAY, "35000"), (DAY + timedelta(hours=8), "36000"), (DAY + timedelta(hours=16), "34000")],
    )
    marks = load_settlement_marks(d)
    assert marks[DAY] == D("35000")
    assert marks[DAY + timedelta(hours=8)] == D("36000")
    assert marks[DAY + timedelta(hours=16)] == D("34000")


def test_bars_not_opening_on_a_settlement_hour_are_ignored(tmp_path: Path) -> None:
    d = tmp_path / "mark_price_klines_8h"
    _write_pages(
        d,
        [(DAY, "35000"), (DAY + timedelta(hours=4), "99999"), (DAY + timedelta(hours=8), "36000")],
    )
    marks = load_settlement_marks(d)
    assert set(marks) == {DAY, DAY + timedelta(hours=8)}


def test_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert load_settlement_marks_if_present(tmp_path) == {}


def test_empty_or_invalid_pages_raise(tmp_path: Path) -> None:
    d = tmp_path / "mark_price_klines_8h"
    d.mkdir(parents=True)
    with pytest.raises(SettlementMarkError, match="no page_"):
        load_settlement_marks(d)
    (d / "page_001.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(SettlementMarkError, match="not a JSON array"):
        load_settlement_marks(d)


def test_non_positive_mark_is_rejected(tmp_path: Path) -> None:
    d = tmp_path / "mark_price_klines_8h"
    _write_pages(d, [(DAY, "0")])
    with pytest.raises(SettlementMarkError, match="non-positive"):
        load_settlement_marks(d)


def test_only_off_hour_bars_raises(tmp_path: Path) -> None:
    d = tmp_path / "mark_price_klines_8h"
    _write_pages(d, [(DAY + timedelta(hours=1), "35000")])
    with pytest.raises(SettlementMarkError, match="settlement hour"):
        load_settlement_marks(d)


# --------------------------------------------------------------------------- priority order
def test_kline_mark_replaces_the_daily_interpolation() -> None:
    cal = _calendar([_event(0, "0.0001"), _event(8, "0.0001"), _event(16, "0.0001")])
    marks = {
        DAY: D("35000"),
        DAY + timedelta(hours=8): D("36000"),
        DAY + timedelta(hours=16): D("34000"),
    }
    model = FundingModel(cal, CostModelConfig(), marks)
    out = model.settlements_for_day(DAY.date(), mark_open=D("30000"), mark_close=D("60000"))
    assert [s.mark_source for s in out] == ["kline_8h"] * 3
    assert [s.mark_price for s in out] == [D("35000"), D("36000"), D("34000")]
    assert model.kline_8h_count == 3
    assert model.approximated_count == 0


def test_record_mark_still_wins_over_the_kline() -> None:
    cal = _calendar([_event(8, "0.0001", mark="41000")])
    model = FundingModel(cal, CostModelConfig(), {DAY + timedelta(hours=8): D("36000")})
    (s,) = model.settlements_for_day(DAY.date(), mark_open=D("30000"), mark_close=D("60000"))
    assert s.mark_source == "record"
    assert s.mark_price == D("41000")
    assert model.exact_count == 1 and model.kline_8h_count == 0


def test_gap_in_the_kline_dataset_falls_back_to_the_daily_approximation() -> None:
    cal = _calendar([_event(0, "0.0001"), _event(8, "0.0001")])
    model = FundingModel(cal, CostModelConfig(), {DAY: D("35000")})  # 08:00 missing
    at_0, at_8 = model.settlements_for_day(DAY.date(), mark_open=D("30000"), mark_close=D("60000"))
    assert at_0.mark_source == "kline_8h" and at_0.mark_price == D("35000")
    assert at_8.mark_source == "daily_interp"
    assert at_8.mark_price == D("30000") + (D("60000") - D("30000")) * D(8) / D(24)
    assert model.kline_8h_count == 1 and model.approximated_count == 1


def test_no_dataset_reproduces_the_previous_behaviour_exactly() -> None:
    cal = _calendar([_event(0, "0.0001"), _event(8, "0.0001"), _event(16, "0.0001")])
    before = FundingModel(cal, CostModelConfig()).settlements_for_day(
        DAY.date(), mark_open=D("30000"), mark_close=D("60000")
    )
    after = FundingModel(cal, CostModelConfig(), {}).settlements_for_day(
        DAY.date(), mark_open=D("30000"), mark_close=D("60000")
    )
    assert [(s.mark_source, s.mark_price) for s in before] == [
        (s.mark_source, s.mark_price) for s in after
    ]
    assert [s.mark_source for s in before] == ["daily_open", "daily_interp", "daily_interp"]
