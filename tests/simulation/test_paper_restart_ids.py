"""Paper-exchange ids must never repeat across a process restart.

The ledger dedupes exchange income by ``income:<type>:<id>`` and fills by ``trade:<id>``. A real
exchange hands out globally unique ids; the paper exchange handed out an in-memory counter. On
2026-09-14 the shadow agent restarted, the counter started again at 1, and on 2026-09-16 three
funding settlements received ids 4-6 - ids the ledger had already mirrored on 09-12. They were
skipped as "already recorded", the paper wallet ran 0.0000388 BTC ahead of the ledger, and the
reconciler (correctly) engaged SAFE_HOLD for four days.

The saved counter alone is not enough: the account state is written at the end of a successful
cycle, and the ids issued in the cycle that aborted were never saved. Hence the two-layer fix
pinned here: funding ids are the funding time itself, and the counter is floored at the
current epoch millisecond on restore.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tests.simulation.test_trader_simulation import WARMUP, Harness

D = Decimal


def test_ids_issued_after_a_restart_never_repeat_earlier_ones(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    before = h.exchange
    issued = {before._new_id() for _ in range(3)}
    saved = before.state_dict()  # the state a successful cycle would have written...
    issued |= {before._new_id() for _ in range(3)}  # ...and ids issued after it, never saved

    h.new_exchange()  # process restart
    h.exchange.restore(saved)

    fresh = {h.exchange._new_id() for _ in range(20)}
    assert not (fresh & issued), f"ids reused after restart: {sorted(fresh & issued)}"


def test_funding_income_ids_are_the_funding_time(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.market.cursor = WARMUP + 1
    h.exchange.position.contracts = D("-100")
    h.exchange.position.avg_entry_price = D("30000")

    applied = h.exchange.settle_funding(None)

    assert applied, "the replay calendar has three events per day"
    ids = {r.tran_id for r in h.exchange.income_records if r.income_type == "FUNDING_FEE"}
    assert ids == {f"funding-{a['fundingTime']}" for a in applied}


def test_funding_settled_after_a_restart_is_mirrored_into_the_ledger(tmp_path: Path) -> None:
    """The 2026-09-16 sequence end to end: hedge opened, two days of funding mirrored, restart,
    more funding. Before the fix the fourth day's settlements reused ids from the first and the
    reconciler latched SAFE_HOLD on a wallet the ledger could no longer explain."""
    h = Harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 2)
    assert not h.ks.engaged

    h.new_exchange()  # fresh paper exchange, same database: the trader restores the account
    trader = h.build_trader()
    trader.start()
    results = h.run_days(WARMUP + 3, 4)

    assert not h.ks.engaged, h.ks.status().reason
    assert all(r.action != "safe_hold" for r in results)
    funding_in_ledger = sum(
        (D(str(e.amount_btc)) for e in h.db.ledger_entries() if e.entry_type == "FUNDING_FEE"),
        D(0),
    )
    funding_received = sum(
        (r.amount for r in h.exchange.income_records if r.income_type == "FUNDING_FEE"), D(0)
    )
    assert funding_received > 0
    assert funding_in_ledger > funding_received, (
        "the ledger spans both processes; the paper account only remembers the second"
    )
    # and the two wallets agree to the satoshi
    assert trader.portfolio is not None
    assert trader.portfolio.wallet_balance_btc == h.exchange.wallet_balance
