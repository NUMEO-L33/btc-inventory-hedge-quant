"""Income is mirrored into the ledger even on a host that answers nothing to an unfiltered query.

Measured on the Binance demo host on 2026-09-27: ``GET /dapi/v1/income`` with no ``incomeType``
returned 0 records for a window in which ``incomeType=FUNDING_FEE`` returned 22. The reconciler
sent the unfiltered call, so seven days of demo funding never reached the testnet ledger - and
the wallet tolerance (0.00001 BTC) hid a 0.00000225 BTC gap. The documented behaviour is the
opposite ("all kinds of flow will be returned"); fetching per documented type works on both.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from btc_inventory_quant.exchange.models import IncomeRecord
from btc_inventory_quant.exchange.paper import PaperExchange
from tests.simulation.test_trader_simulation import WARMUP, Harness

D = Decimal


class DemoLikeExchange(PaperExchange):
    """Paper exchange with the demo host's quirk: unfiltered income queries come back empty."""

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.unfiltered_calls = 0

    def income(
        self,
        symbol: str | None = None,
        income_type: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[IncomeRecord]:
        if income_type is None:
            self.unfiltered_calls += 1
            return []
        return super().income(symbol, income_type, start_ms, end_ms, limit)


def _harness(tmp_path: Path) -> Harness:
    h = Harness(tmp_path)
    h.exchange = DemoLikeExchange(
        h.market, h.spec, initial_btc=D("1"), taker_fee=D("0.0005"), slippage_bps=D("5")
    )
    return h


def test_funding_reaches_the_ledger_on_a_host_that_ignores_unfiltered_income(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)
    trader = h.build_trader()
    trader.start()

    h.run_days(WARMUP + 1, 4)

    assert trader.portfolio is not None
    funding_received = sum(
        (r.amount for r in h.exchange.income_records if r.income_type == "FUNDING_FEE"), D(0)
    )
    assert funding_received > 0
    assert trader.portfolio.ledger.totals().funding == funding_received, (
        "every settlement the account received must be in the ledger"
    )
    assert trader.portfolio.wallet_balance_btc == h.exchange.wallet_balance
    assert not h.ks.engaged


def test_transfers_are_still_noticed_when_fetched_per_type(tmp_path: Path) -> None:
    """The unexpected-transfer guard only works if TRANSFER records are fetched at all."""
    h = _harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 2)

    h.exchange.external_transfer(D("0.5"))
    res = trader.cycle()

    assert res.action == "safe_hold"
    assert trader.portfolio is not None
    assert trader.portfolio.ledger.totals().deposits == D("0.5")


def test_the_reconciler_never_sends_the_unfiltered_query(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    trader = h.build_trader()
    trader.start()
    h.run_days(WARMUP + 1, 2)

    assert h.exchange.unfiltered_calls == 0
