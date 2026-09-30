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
