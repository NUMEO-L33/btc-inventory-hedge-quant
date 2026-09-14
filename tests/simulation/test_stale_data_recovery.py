"""Stale market data must stop trading WITHOUT latching the kill switch.

Week one of shadow ended with six days of SAFE_HOLD. A suspended laptop froze the mark-price
WebSocket while the socket still reported ``connected``; the silence guard fired, latched, and
from then on every cycle returned before it even re-evaluated the guard. Refusing to trade on
stale data was right. Latching is what turned a closed lid into a six-day outage that only a
human could clear — and nobody was watching.

These tests pin the distinction: block immediately, recover on its own, latch only once the
condition has outlived any plausible transient. The second half covers the supervisor that
restarts the stream thread — which has had bugs of its own (2026-09-14): it killed a stream
that was mid-backoff, and it restarted, with a page to the operator, streams that were a
second away from reconnecting by themselves.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from tests.simulation.test_trader_simulation import WARMUP, Harness


class FakeStreams:
    """Stands in for :class:`StreamBundle`; the test drives silence, liveness and activity."""

    enabled = True
    user = None

    def __init__(
        self, silence: float | None = 0.0, alive: bool = True, network_up: bool = True
    ) -> None:
        self.silence = silence
        self._alive = alive
        self.network_up = network_up  # a (re)connect only delivers data if the network is there
        self.attempts = 0  # connect attempts made by the stream's own loop
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

    def activity_marker(self) -> tuple[Any, ...]:
        return (self.silence, self.attempts)

    def attempt(self) -> None:
        """The stream's own loop tries to reconnect; data flows only if the network is up."""
        self.attempts += 1
        if self.network_up:
            self.silence = 0.0

    def restart(self) -> None:
        # Like the real StreamBundle: a new thread comes up and attempts a connect at once, but
        # silence is measured from the last MESSAGE, so it only clears once one arrives.
        self.restarts += 1
        self._alive = True
        self.attempt()


class TickingClock:
    """The trader's clock and sleep(), both driven off the replay market, so the run loop can be
    stepped deterministically and the paper exchange's mark price stays fresh while time passes
    within the day."""

    def __init__(self, market: Any) -> None:
        self.market = market

    @property
    def now(self) -> datetime:
        return self.market.now  # type: ignore[no-any-return]

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.market.elapse(seconds)


def _run_stale_cycles(h: Harness, n: int) -> list[Any]:
    results = []
    for _ in range(n):
        results.append(h.trader.cycle())
        h.market.advance()
    return results


# --------------------------------------------------------------------- block, don't latch
def test_stale_data_blocks_trading_but_does_not_latch_safe_hold(tmp_path: Path) -> None:
    h = Harness(tmp_path, risk={"stale_data_cycles_to_safe_hold": 3})
    streams = FakeStreams(silence=90_000.0, network_up=False)  # the 24.8 h we actually observed
    trader = h.build_trader(streams=streams)
    trader.start()
    h.market.cursor = WARMUP + 1

    res = trader.cycle()

    assert any(g.code == "WS_SILENT" for g in res.guard_failures)
    assert res.action != "rebalance", "stale data must not be traded on"
    assert not h.ks.engaged, "one stale cycle is a suspended laptop, not a reason to latch"


def test_stale_data_recovers_by_itself_when_the_stream_comes_back(tmp_path: Path) -> None:
    h = Harness(tmp_path, risk={"stale_data_cycles_to_safe_hold": 3})
    streams = FakeStreams(silence=90_000.0, network_up=False)
    trader = h.build_trader(streams=streams)
    trader.start()
    h.market.cursor = WARMUP + 1

    _run_stale_cycles(h, 2)
    assert not h.ks.engaged

    streams.network_up = True
    streams.attempt()  # the stream's own loop reconnects; nobody had to restart anything
    res = trader.cycle()

    assert not any(g.code == "WS_SILENT" for g in res.guard_failures)
    assert not h.ks.engaged
    assert streams.restarts == 0, "recovery is the stream's job, not the supervisor's"
    # and the counter is cleared, so the next unrelated blip starts from zero
    assert (h.trader.db.get_state("stale_data_cycles") or {}).get("count") == 0


def test_stale_data_engages_safe_hold_once_it_outlives_the_transient(tmp_path: Path) -> None:
    h = Harness(tmp_path, risk={"stale_data_cycles_to_safe_hold": 3})
    streams = FakeStreams(silence=90_000.0, network_up=False)
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
    trader = h.build_trader(streams=FakeStreams(silence=90_000.0, network_up=False))
    trader.start()
    h.market.cursor = WARMUP + 1
    _run_stale_cycles(h, 2)
    assert not h.ks.engaged

    h.new_exchange()  # fresh process, same database
    trader = h.build_trader(streams=FakeStreams(silence=90_000.0, network_up=False))
    trader.start()
    h.market.cursor = WARMUP + 4
    trader.cycle()

    assert h.ks.engaged, "the count restarted from zero and the condition became invisible"


def test_retries_of_one_stale_decision_count_as_one_cycle(tmp_path: Path) -> None:
    """The run loop retries a blocked decision every few minutes. Three retries into one
    morning of stale data are one bad day, not the three that latch SAFE_HOLD."""
    h = Harness(tmp_path, risk={"stale_data_cycles_to_safe_hold": 3})
    streams = FakeStreams(silence=90_000.0, network_up=False)
    trader = h.build_trader(streams=streams)
    trader.start()
    h.market.cursor = WARMUP + 1

    for _ in range(4):
        trader.cycle()
        h.market.elapse(600)

    assert not h.ks.engaged
    assert (h.trader.db.get_state("stale_data_cycles") or {}).get("count") == 1


# ------------------------------------------------------------------------- the supervisor
def test_heartbeat_restarts_streams_whose_thread_has_died(tmp_path: Path) -> None:
    """Self-healing lives in the stream thread. This covers the thread itself dying."""
    h = Harness(tmp_path)
    streams = FakeStreams(silence=0.0, alive=False)
    trader = h.build_trader(streams=streams)
    trader.start()

    trader._heartbeat()

    assert streams.restarts == 1
    assert streams.alive()


def test_a_dead_stream_thread_is_restarted_without_waiting_out_the_cooldown(
    tmp_path: Path,
) -> None:
    """The cooldown protects against churn on a dead network. A dead THREAD is not churn - nothing
    is running - and on 2026-09-14 the cooldown left it dead for 18 minutes."""
    h = Harness(tmp_path)
    streams = FakeStreams(silence=0.0, alive=False)
    trader = h.build_trader(streams=streams)
    trader.start()

    trader._heartbeat()  # restarts, starts the cooldown
    streams._alive = False  # dies again immediately
    trader._heartbeat()

    assert streams.restarts == 2, "a dead thread must not wait for the cooldown"


def test_cycle_gives_the_supervisor_first_go_before_judging_stale_data(tmp_path: Path) -> None:
    """Waking past the decision time runs cycle() before any heartbeat, so a stream that died
    during sleep was judged stale before anyone tried to repair it (2026-09-14 00:05 UTC)."""
    h = Harness(tmp_path)
    streams = FakeStreams(silence=90_000.0, alive=False)
    trader = h.build_trader(streams=streams)
    trader.start()
    h.market.cursor = WARMUP + 1

    res = trader.cycle()

    assert streams.restarts >= 1, "cycle() must attempt a repair before evaluating the guard"
    assert not any(g.code == "WS_SILENT" for g in res.guard_failures), (
        "the repaired stream (silence reset by restart) must not be judged stale"
    )


def test_supervisor_never_restarts_a_silent_stream_on_the_first_look(tmp_path: Path) -> None:
    """On a laptop wake the heartbeat often runs before the stream thread has had its turn
    (3 of 7 long wakes on 2026-09-13/14). Judged at that instant the stream looks silent and
    idle - and restarting it there paged the operator for a stream that was a second away from
    reconnecting by itself."""
    h = Harness(tmp_path)
    streams = FakeStreams(silence=1_057.0, network_up=False)  # the 12:13 UTC wake, verbatim
    trader = h.build_trader(streams=streams)
    trader.start()

    trader._heartbeat()

    assert streams.restarts == 0


def test_supervisor_leaves_a_stream_that_keeps_trying_alone(tmp_path: Path) -> None:
    """Wi-Fi down for a long time: the stream's own loop attempts a connect every 30-40 s.
    That is the recovery path working; a restart on top of it only resets its backoff and
    raises an alert per poll."""
    h = Harness(tmp_path)
    clock = TickingClock(h.market)
    streams = FakeStreams(silence=90_000.0, network_up=False)
    trader = h.build_trader(streams=streams, clock=clock, sleep=clock.sleep)
    trader.start()

    for _ in range(30):  # 30 minutes offline
        streams.attempt()
        trader._heartbeat()
        clock.sleep(60)

    assert streams.restarts == 0
    assert streams.attempts == 30


def test_supervisor_restarts_a_stream_whose_loop_has_stopped_trying(tmp_path: Path) -> None:
    """Alive thread, silent stream, and not one connect attempt across minutes of awake time:
    that is a stuck loop, the one case the stream cannot fix from the inside."""
    h = Harness(tmp_path)
    clock = TickingClock(h.market)
    streams = FakeStreams(silence=90_000.0, network_up=False)
    trader = h.build_trader(streams=streams, clock=clock, sleep=clock.sleep)
    trader.start()

    restarted_after = None
    for minute in range(10):
        trader._heartbeat()
        if streams.restarts:
            restarted_after = minute
            break
        clock.sleep(60)

    assert restarted_after is not None, "a stuck loop must eventually be restarted"
    assert restarted_after >= 3, "not before STREAM_STALL_SECONDS of awake time"
    assert restarted_after <= 4


def test_time_spent_suspended_does_not_count_towards_a_stall(tmp_path: Path) -> None:
    """Two looks 40 minutes apart with nothing in between is a laptop that was asleep, during
    which the stream thread could not have attempted anything either. Not a stall."""
    h = Harness(tmp_path)
    clock = TickingClock(h.market)
    streams = FakeStreams(silence=90_000.0, network_up=False)
    trader = h.build_trader(streams=streams, clock=clock, sleep=clock.sleep)
    trader.start()

    trader._heartbeat()  # first look, right before the lid closes
    clock.sleep(40 * 60)  # asleep
    trader._heartbeat()  # first look after the wake
    clock.sleep(60)
    trader._heartbeat()

    assert streams.restarts == 0


def test_stall_restarts_are_rate_limited(tmp_path: Path) -> None:
    """If the replacement thread stalls too, the cooldown keeps this from becoming a restart
    (and an alert) every three minutes."""
    h = Harness(tmp_path)
    clock = TickingClock(h.market)
    streams = FakeStreams(silence=90_000.0, network_up=False)
    trader = h.build_trader(streams=streams, clock=clock, sleep=clock.sleep)
    trader.start()

    for _ in range(14):  # 14 minutes: enough for several stall windows
        trader._heartbeat()
        clock.sleep(60)

    assert streams.restarts == 1, "a second restart must wait out the cooldown"
