"""Stale market data must stop trading WITHOUT latching the kill switch.

Week one of shadow ended with six days of SAFE_HOLD. A suspended laptop froze the mark-price
WebSocket while the socket still reported ``connected``; the silence guard fired, latched, and
from then on every cycle returned before it even re-evaluated the guard. Refusing to trade on
stale data was right. Latching is what turned a closed lid into a six-day outage that only a
human could clear — and nobody was watching.

These tests pin the distinction: block immediately, recover on its own, latch only once the
condition has outlived any plausible transient.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.simulation.test_trader_simulation import WARMUP, Harness


class FakeStreams:
    """Stands in for :class:`StreamBundle`; the test drives silence and liveness directly."""

    enabled = True
    user = None

    def __init__(self, silence: float | None = 0.0, alive: bool = True) -> None:
        self.silence = silence
        self._alive = alive
        self.restarts = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def statuses(self) -> list[Any]:
        return []

    def connected(self) -> bool:
        return True  # the whole point: the socket claims to be fine

    def alive(self) -> bool:
        return self._alive

    def max_silence_seconds(self, now: Any) -> float | None:
        return self.silence

    def restart(self) -> None:
        self.restarts += 1
        self._alive = True
        self.silence = 0.0


def _run_stale_cycles(h: Harness, n: int) -> list[Any]:
    results = []
    for _ in range(n):
        results.append(h.trader.cycle())
        h.market.advance()
    return results


def test_stale_data_blocks_trading_but_does_not_latch_safe_hold(tmp_path: Path) -> None:
    h = Harness(tmp_path, risk={"stale_data_cycles_to_safe_hold": 3})
    streams = FakeStreams(silence=90_000.0)  # the 24.8 h of silence we actually observed
    trader = h.build_trader(streams=streams)
    trader.start()
    h.market.cursor = WARMUP + 1

    res = trader.cycle()

    assert any(g.code == "WS_SILENT" for g in res.guard_failures)
    assert res.action != "rebalance", "stale data must not be traded on"
    assert not h.ks.engaged, "one stale cycle is a suspended laptop, not a reason to latch"


def test_stale_data_recovers_by_itself_when_the_stream_comes_back(tmp_path: Path) -> None:
    h = Harness(tmp_path, risk={"stale_data_cycles_to_safe_hold": 3})
    streams = FakeStreams(silence=90_000.0)
    trader = h.build_trader(streams=streams)
    trader.start()
    h.market.cursor = WARMUP + 1

    _run_stale_cycles(h, 2)
    assert not h.ks.engaged

    streams.silence = 1.0  # the stream reconnected on its own
    res = trader.cycle()

    assert not any(g.code == "WS_SILENT" for g in res.guard_failures)
    assert not h.ks.engaged
    # and the counter is cleared, so the next unrelated blip starts from zero
    assert (h.trader.db.get_state("stale_data_cycles") or {}).get("count") == 0


def test_stale_data_engages_safe_hold_once_it_outlives_the_transient(tmp_path: Path) -> None:
    h = Harness(tmp_path, risk={"stale_data_cycles_to_safe_hold": 3})
    streams = FakeStreams(silence=90_000.0)
    trader = h.build_trader(streams=streams)
    trader.start()
    h.market.cursor = WARMUP + 1

    results = _run_stale_cycles(h, 3)

    assert results[0].action != "safe_hold", "the first stale cycle must only block"
    assert h.ks.engaged, "three consecutive stale cycles is structural, not transient"
    assert "stale market data" in h.ks.status().reason


def test_the_stale_counter_survives_a_restart(tmp_path: Path) -> None:
    """A process restart must not wipe the evidence that data has been stale for days."""
    h = Harness(tmp_path, risk={"stale_data_cycles_to_safe_hold": 3})
    trader = h.build_trader(streams=FakeStreams(silence=90_000.0))
    trader.start()
    h.market.cursor = WARMUP + 1
    _run_stale_cycles(h, 2)
    assert not h.ks.engaged

    h.new_exchange()  # fresh process, same database
    trader = h.build_trader(streams=FakeStreams(silence=90_000.0))
    trader.start()
    h.market.cursor = WARMUP + 4
    trader.cycle()

    assert h.ks.engaged, "the count restarted from zero and the condition became invisible"


def test_heartbeat_restarts_streams_whose_thread_has_died(tmp_path: Path) -> None:
    """Self-healing lives in the stream thread. This covers the thread itself dying."""
    h = Harness(tmp_path)
    streams = FakeStreams(silence=0.0, alive=False)
    trader = h.build_trader(streams=streams)
    trader.start()

    trader._heartbeat()

    assert streams.restarts == 1
    assert streams.alive()


def test_stream_restart_is_rate_limited_when_the_network_is_simply_down(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    streams = FakeStreams(silence=90_000.0)
    trader = h.build_trader(streams=streams)
    trader.start()

    for _ in range(5):
        streams.silence = 90_000.0  # still nothing arriving
        trader._heartbeat()

    assert streams.restarts == 1, "restarting on every poll would churn, not recover"
