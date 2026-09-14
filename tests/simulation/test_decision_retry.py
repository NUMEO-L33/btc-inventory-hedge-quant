"""A daily decision that fails for a transient reason is retried within the day.

The shadow agent runs on a laptop. Asleep at the decision time, it runs the cycle the moment
it wakes - typically before Wi-Fi is back - and without a retry that day's decision is simply
lost. Worse, three such mornings in a row are three "consecutive connectivity failures", which
latches SAFE_HOLD for a condition that fixed itself thirty seconds after each wake.

These tests pin: retry until it goes through or the next decision time; retries of one outage
count as one bad day; decisions that went through, or that are blocked for a structural
reason, are not retried.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from btc_inventory_quant.exchange.base import ExchangeUnavailable
from btc_inventory_quant.exchange.paper import PaperExchange
from btc_inventory_quant.risk.guards import GuardResult
from btc_inventory_quant.runtime.trader import CycleResult, Trader
from tests.simulation.test_stale_data_recovery import FakeStreams, TickingClock
from tests.simulation.test_trader_simulation import WARMUP, Harness

D = Decimal


class FlakyExchange(PaperExchange):
    """Paper exchange whose kline endpoint is unreachable for the next N calls."""

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.klines_failures = 0

    def klines(self, *a: Any, **k: Any) -> list[list[Any]]:
        if self.klines_failures > 0:
            self.klines_failures -= 1
            raise ExchangeUnavailable("simulated: name resolution failed (Wi-Fi not up yet)")
        return super().klines(*a, **k)


def _harness(tmp_path: Path, klines_failures: int) -> tuple[Harness, TickingClock]:
    h = Harness(tmp_path)
    h.exchange = FlakyExchange(
        h.market, h.spec, initial_btc=D("1"), taker_fee=D("0.0005"), slippage_bps=D("5")
    )
    h.exchange.klines_failures = klines_failures
    h.market.cursor = WARMUP + 1
    return h, TickingClock(h.market)  # 00:05 UTC of the execution day


def test_an_unreachable_exchange_is_retried_within_the_day(tmp_path: Path) -> None:
    h, clock = _harness(tmp_path, klines_failures=1)
    trader = h.build_trader(clock=clock, sleep=clock.sleep)
    start = clock.now

    assert trader.run(max_cycles=2) == 0

    decision = h.db.latest_decision()
    assert decision is not None, "the retry must have produced the day's decision"
    assert decision.action in ("rebalance", "none")
    elapsed = clock.now - start
    assert timedelta(minutes=10) <= elapsed < timedelta(minutes=12), (
        f"retried after {elapsed}, expected the configured 10 minutes, not tomorrow"
    )


def test_a_decision_that_went_through_waits_for_the_next_decision_time(tmp_path: Path) -> None:
    h, clock = _harness(tmp_path, klines_failures=0)
    trader = h.build_trader(clock=clock, sleep=clock.sleep)
    start = clock.now

    assert trader.run(max_cycles=2) == 0

    assert clock.now - start >= timedelta(hours=23), "nothing to retry: the next cycle is tomorrow"


def test_retries_of_one_outage_are_one_bad_day_not_three(tmp_path: Path) -> None:
    """Three failed attempts inside one morning must not reach the three-strike latch."""
    h, clock = _harness(tmp_path, klines_failures=3)
    trader = h.build_trader(clock=clock, sleep=clock.sleep)

    assert trader.run(max_cycles=4) == 0  # error, retry, retry, success

    assert not h.ks.engaged
    assert trader.consecutive_data_errors == 0, "the successful retry clears the count"
    unavailable = [a for a in h.alerter.sent if a.event_type == "exchange_unavailable"]
    assert len(unavailable) == 1, "one outage, one page - not one per retry"
    assert h.db.latest_decision() is not None


def test_three_bad_days_still_latch(tmp_path: Path) -> None:
    """The escalation is per decision slot, and three slots in a row is what it is for."""
    h, clock = _harness(tmp_path, klines_failures=10_000)
    trader = h.build_trader(clock=clock, sleep=clock.sleep)
    trader.start()

    for _ in range(3):
        trader.cycle()
        h.market.advance()

    assert h.ks.engaged
    assert "connectivity" in h.ks.status().reason


def test_only_transient_failures_are_retryable() -> None:
    ts = datetime(2026, 9, 14, 0, 5, tzinfo=UTC)
    stale = [GuardResult(False, "WS_SILENT", "no stream message for 1490s")]
    margin = [GuardResult(False, "MARGIN_TYPE", "expected isolated, got cross")]
    assert Trader.retryable(CycleResult(ts, "error"))
    assert Trader.retryable(CycleResult(ts, "blocked", guard_failures=stale))
    assert not Trader.retryable(CycleResult(ts, "blocked", guard_failures=margin)), (
        "an account setting does not fix itself in ten minutes"
    )
    assert not Trader.retryable(CycleResult(ts, "blocked", guard_failures=stale + margin))
    assert not Trader.retryable(CycleResult(ts, "blocked")), (
        "'orders not allowed' is a mode, not an outage"
    )
    for action in ("none", "rebalance", "skipped", "safe_hold"):
        assert not Trader.retryable(CycleResult(ts, action))


def test_a_stale_stream_block_is_retried_and_clears_once_data_flows(tmp_path: Path) -> None:
    """The morning-wake case end to end: stale streams block the first attempt, the stream's
    own loop reconnects a little later, the retry goes through - no restart, one alert."""
    h = Harness(tmp_path)
    h.market.cursor = WARMUP + 1
    clock = TickingClock(h.market)
    streams = FakeStreams(silence=1_490.0, network_up=False)
    trader = h.build_trader(streams=streams, clock=clock, sleep=clock.sleep)

    original_heartbeat = trader._heartbeat

    def heartbeat_with_wifi_back_after_two_minutes() -> None:
        original_heartbeat()
        if clock.now >= start + timedelta(minutes=2) and not streams.network_up:
            streams.network_up = True
            streams.attempt()

    start = clock.now
    trader._heartbeat = heartbeat_with_wifi_back_after_two_minutes  # type: ignore[method-assign]

    assert trader.run(max_cycles=2) == 0

    decision = h.db.latest_decision()
    assert decision is not None and decision.action in ("rebalance", "none")
    assert clock.now - start < timedelta(minutes=12)
    assert streams.restarts == 0
    assert not h.ks.engaged
    blocked = [a for a in h.alerter.sent if a.event_type == "stale_data_blocked"]
    assert len(blocked) == 1
