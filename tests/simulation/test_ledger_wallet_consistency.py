"""The saved wallet must never fall out of step with the ledger it is supposed to summarise.

Found 2026-10-10 on testnet: ledger 0.01001038 BTC, saved wallet 0.01001005. On 2026-09-30 00:44
a cycle mirrored three funding settlements into the ledger and then latched SAFE_HOLD on a
guard - returning before the end-of-cycle save. The 01:19 deploy restarted the process; the
wallet came back from the older save, and income sync skipped the three settlements because
the ledger, which it dedupes against, already had them. 0.00000033 BTC, permanently, below the
reconciler's 0.00001 BTC tolerance - so nothing noticed for ten days. On a larger account the
same sequence would have shown up as SAFE_HOLD again right after the release-and-restart.

Three layers, one test each: mirrored income is persisted before anything can return early;
a stale saved wallet is rebuilt from the ledger at startup (loudly); the durable ledger row is
written before the in-memory wallet moves.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from btc_inventory_quant.exchange.paper import PaperExchange
from btc_inventory_quant.runtime.trader import LEDGER_WALLET_TOLERANCE_BTC
from tests.simulation.test_trader_simulation import WARMUP, Harness

D = Decimal


def _same(a: Decimal, b: Decimal) -> bool:
    """Equal up to Decimal summation-order noise (~1e-27); a missing row is 1e-8 or more."""
    return abs(a - b) <= LEDGER_WALLET_TOLERANCE_BTC


class ExternalExchange(PaperExchange):
    """A paper account that behaves like a real exchange across OUR restarts: it lives outside
    the trading process, so nothing is saved to or restored from our database. (The shadow
    paper account is restored from the database, which would hide exactly this kind of gap.)"""

    state_dict = None  # type: ignore[assignment]
    restore = None  # type: ignore[assignment]

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.offset_ms = 0

    def sync_time(self) -> int:
        return self.offset_ms


def _harness(tmp_path: Path) -> Harness:
    h = Harness(tmp_path)
    h.exchange = ExternalExchange(
        h.market, h.spec, initial_btc=D("1"), taker_fee=D("0.0005"), slippage_bps=D("5")
    )
    return h


def _ledger_wallet(h: Harness) -> Decimal:
    derived = h.db.ledger_wallet_balance()
    assert derived is not None
    return derived[0]


def _repairs(h: Harness) -> list[Any]:
    return [a for a in h.alerter.sent if a.event_type == "ledger_wallet_repaired"]


def test_income_mirrored_by_a_cycle_that_latches_survives_a_restart(tmp_path: Path) -> None:
    """The 2026-09-30 sequence: income sync, then a critical guard, then a deploy restart."""
    h = _harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 5)  # hedge on, several days of funding mirrored and saved

    rows_before = len(h.db.ledger_entries())
    h.exchange.offset_ms = 61_000  # a guard that trips after income sync, as on 09-30
    res = trader.cycle()
    assert res.action == "safe_hold"
    assert len(h.db.ledger_entries()) > rows_before, (
        "the latched cycle must have mirrored income, or this test proves nothing"
    )

    restarted = h.build_trader()  # same exchange (it is external), same database
    restarted.start()

    assert restarted.portfolio is not None
    assert _same(restarted.portfolio.wallet_balance_btc, h.exchange.wallet_balance)
    assert _same(_ledger_wallet(h), h.exchange.wallet_balance)
    assert not _repairs(h), "nothing should have needed repairing: the cycle saved in time"


def test_a_stale_saved_wallet_is_rebuilt_from_the_ledger_and_reported(tmp_path: Path) -> None:
    """A process killed between the ledger write and the save leaves the cache behind."""
    h = _harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 5)
    saved = h.db.get_state("portfolio")
    true_wallet = D(str(saved["wallet_balance_btc"]))
    h.db.set_state("portfolio", dict(saved, wallet_balance_btc=str(true_wallet - D("0.00000033"))))

    restarted = h.build_trader()
    report = restarted.start()

    assert restarted.portfolio is not None
    assert _same(restarted.portfolio.wallet_balance_btc, true_wallet)
    assert _same(D(report["ledger_repair"]["difference_btc"]), D("0.00000033"))
    assert len(_repairs(h)) == 1, "a repair must never be silent"
    assert not h.ks.engaged
    # and the repaired value is what gets saved from now on
    assert _same(D(str(h.db.get_state("portfolio")["wallet_balance_btc"])), true_wallet)


def test_the_ledger_sum_starts_at_the_latest_opening_balance(tmp_path: Path) -> None:
    """The runbook's recovery re-adopts the exchange state as a fresh opening balance;
    summing across both openings would count the capital twice and 'repair' a sound wallet."""
    h = _harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 3)

    h.db.set_state("portfolio", None)  # the runbook: drop the saved portfolio and restart
    readopted = h.build_trader()
    readopted.start()
    h.run_days(WARMUP + 4, 2)
    restarted = h.build_trader()
    restarted.start()

    assert restarted.portfolio is not None
    every_row = sum((D(str(e.amount_btc)) for e in h.db.ledger_entries()), D(0))
    assert every_row > restarted.portfolio.wallet_balance_btc + D("0.5"), (
        "two opening balances are in the table, so a naive sum would double the capital"
    )
    assert _same(_ledger_wallet(h), restarted.portfolio.wallet_balance_btc)
    assert not _repairs(h)


def test_a_failed_ledger_write_leaves_the_wallet_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 5)
    assert trader.portfolio is not None
    before = trader.portfolio.wallet_balance_btc
    h.exchange.position.contracts = h.exchange.position.contracts or D("-100")
    assert h.exchange.apply_funding(D("0.0001")) != 0  # a settlement the ledger has not seen

    def disk_full(entry: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(h.db, "add_ledger_entry", disk_full)
    with pytest.raises(OSError):
        trader.reconciler.sync_income(trader.portfolio, None)

    assert trader.portfolio.wallet_balance_btc == before, (
        "the wallet moved for money the ledger never recorded"
    )
