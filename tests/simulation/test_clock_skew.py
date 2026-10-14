"""A clock a second off after a wake blocks the cycle; a clock a minute off latches.

2026-09-30 00:44 UTC: the testnet agent's retried decision ran three seconds after the network
came back from a sleep, measured a 1302 ms offset against the demo host, and latched SAFE_HOLD
as a critical guard failure. The adapter had already signed every request with that offset
applied; NTP fixed the clock minutes later; a human still had to release the kill switch.
Same shape as the 09-05 stream incident: a recoverable condition made irreversible.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from btc_inventory_quant.config.schema import RiskLimits
from btc_inventory_quant.domain.inverse_contract import ContractSpec
from btc_inventory_quant.exchange.base import ExchangeUnavailable
from btc_inventory_quant.exchange.paper import PaperExchange
from btc_inventory_quant.risk.guards import Guards
from tests.conftest import FIXTURES
from tests.simulation.test_stale_data_recovery import TickingClock
from tests.simulation.test_trader_simulation import WARMUP, Harness

D = Decimal


class SkewedExchange(PaperExchange):
    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.offset_ms = 0

    def sync_time(self) -> int:
        return self.offset_ms


def _harness(tmp_path: Path, offset_ms: int) -> Harness:
    h = Harness(tmp_path)
    h.exchange = SkewedExchange(
        h.market, h.spec, initial_btc=D("1"), taker_fee=D("0.0005"), slippage_bps=D("5")
    )
    h.exchange.offset_ms = offset_ms
    h.market.cursor = WARMUP + 1
    return h


def test_the_clock_guard_has_two_tiers() -> None:
    sym = json.loads((FIXTURES / "exchange_info_btcusd_perp.json").read_text())["symbol"]
    guards = Guards(RiskLimits(), ContractSpec.from_exchange_info_symbol(sym, "fixture"), "X")
    assert guards.check_clock(900).ok
    blocked = guards.check_clock(1302)
    assert not blocked.ok and blocked.code == "CLOCK_SKEW" and not blocked.critical
    latched = guards.check_clock(-61_000)
    assert not latched.ok and latched.critical


def test_a_wake_time_skew_blocks_the_cycle_and_the_retry_goes_through(tmp_path: Path) -> None:
    h = _harness(tmp_path, offset_ms=1302)
    clock = TickingClock(h.market)
    trader = h.build_trader(clock=clock, sleep=clock.sleep)
    start = clock.now

    original_heartbeat = trader._heartbeat

    def heartbeat_ntp_fixes_the_clock_after_two_minutes() -> None:
        original_heartbeat()
        if clock.now >= start + timedelta(minutes=2):
            h.exchange.offset_ms = 12

    trader._heartbeat = heartbeat_ntp_fixes_the_clock_after_two_minutes  # type: ignore[method-assign]

    assert trader.run(max_cycles=2) == 0

    assert not h.ks.engaged, "1.3 s of skew after a wake is not a reason to latch"
    decision = h.db.latest_decision()
    assert decision is not None and decision.action in ("rebalance", "none")
    blocked = [a for a in h.alerter.sent if a.event_type == "clock_skew_blocked"]
    assert len(blocked) == 1


def test_a_skew_that_persists_for_three_slots_latches(tmp_path: Path) -> None:
    h = _harness(tmp_path, offset_ms=1302)
    trader = h.build_trader()
    trader.start()

    results = h.run_days(WARMUP + 1, 3)

    assert results[0].action != "safe_hold"
    assert h.ks.engaged, "three days of skew is a broken clock, not a wake"
    assert "clock skew" in h.ks.status().reason


def test_a_clock_off_by_a_minute_latches_at_once(tmp_path: Path) -> None:
    h = _harness(tmp_path, offset_ms=61_000)
    trader = h.build_trader()
    trader.start()

    res = trader.cycle()

    assert res.action == "safe_hold"
    assert h.ks.engaged


# ------------------------------------------------------------- judged against production time
class ReferenceClock:
    """Production Binance's /time as measured from our clock: the guard's second opinion."""

    def __init__(self, offset_ms: int = 0, fail: bool = False) -> None:
        self.offset_ms = offset_ms
        self.fail = fail
        self.calls = 0
        self.time_sync_rtt_ms = 41

    def sync_time(self) -> int:
        self.calls += 1
        if self.fail:
            raise ExchangeUnavailable("production /time unreachable")
        return self.offset_ms


def _skew_alerts(h: Harness) -> list[Any]:
    return [a for a in h.alerter.sent if a.event_type == "clock_skew_blocked"]


def test_a_demo_host_ahead_of_production_does_not_block(tmp_path: Path) -> None:
    """2026-10-04, 10-12, 10-14 at 00:05:00 UTC: the demo host's /time read 2.03-2.07 s ahead
    while the Mac had been awake all night. Signing already follows the demo host's offset;
    whether OUR clock is right is a question for production time, and it said 12 ms."""
    h = _harness(tmp_path, offset_ms=2048)  # the demo host, verbatim from 10-14
    reference = ReferenceClock(offset_ms=12)
    trader = h.build_trader(clock_reference=reference)
    report = trader.start()

    results = h.run_days(WARMUP + 1, 3)  # three days in a row: the case that would latch

    assert report["clock_offset_ms"] == 2048 and report["reference_clock_offset_ms"] == 12
    assert reference.calls >= 4, "the reference is asked at start and on every cycle"
    assert not any(g.code == "CLOCK_SKEW" for r in results for g in r.guard_failures)
    assert not _skew_alerts(h)
    assert not h.ks.engaged


def test_a_local_clock_that_is_really_off_is_still_caught(tmp_path: Path) -> None:
    """If the Mac is the one that moved, both hosts see it - and the guard must too."""
    h = _harness(tmp_path, offset_ms=2048)
    trader = h.build_trader(clock_reference=ReferenceClock(offset_ms=2051))
    trader.start()

    res = trader.cycle()

    assert any(g.code == "CLOCK_SKEW" and "2051" in g.message for g in res.guard_failures)
    assert len(_skew_alerts(h)) == 1
    assert not h.ks.engaged, "still the two-tier rule: block now, latch only if it persists"


def test_shadow_gets_a_clock_to_judge(tmp_path: Path) -> None:
    """The paper account has no clock (offset always 0), so shadow never exercised this guard;
    live will. With production time as the reference, shadow sees what live would see."""
    h = Harness(tmp_path)  # plain PaperExchange: no sync_time at all
    h.market.cursor = WARMUP + 1
    trader = h.build_trader(clock_reference=ReferenceClock(offset_ms=-1302))
    trader.start()

    res = trader.cycle()

    assert any(g.code == "CLOCK_SKEW" for g in res.guard_failures)
    assert not h.ks.engaged


def test_an_unreachable_reference_falls_back_to_the_exchange(tmp_path: Path) -> None:
    """A second opinion must never become a new way to fail: no reference, old behaviour."""
    h = _harness(tmp_path, offset_ms=2048)
    trader = h.build_trader(clock_reference=ReferenceClock(fail=True))
    report = trader.start()

    blocked = trader.cycle()
    h.market.advance()
    h.exchange.offset_ms = 12
    fine = trader.cycle()

    assert report["reference_clock_offset_ms"] is None
    assert any(g.code == "CLOCK_SKEW" for g in blocked.guard_failures), "judged by the exchange"
    assert fine.action != "safe_hold"
    assert not any(g.code == "CLOCK_SKEW" for g in fine.guard_failures)
    assert not h.ks.engaged
