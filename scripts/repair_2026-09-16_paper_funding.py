"""Repair the shadow ledger after the 2026-09-16 paper income-id collision.

What happened: the paper exchange numbered income records with an in-memory counter. After the
2026-09-14 restart it started again at 1, so the three funding settlements of 2026-09-16 00:05
UTC got ids 4-6 - ids the ledger had already mirrored on 09-12 - and were skipped as "already
recorded". The paper wallet then stood 0.0000388 BTC above the ledger and the reconciler
engaged SAFE_HOLD (correctly: the two disagreed).

What this does: puts those settlements into the ledger, and the same amount into the two saved
wallet balances (the trader's portfolio state and the paper-account state), so that everything
agrees with the paper wallet again. Nothing is invented: the total is the exact difference the
reconciler recorded; the split across the three settlements is by their real funding rates
(fetched from the exchange), which is how the paper exchange computed them in the first place -
same position, same mark, three rates. The mark implied by that arithmetic is printed next to
the mark the cycle actually saw, as a plausibility check.

Dry run by default. Run with the agent STOPPED, then --apply, then release SAFE_HOLD, then
restart with the fixed code (ids are now unique across restarts).

    .venv/bin/python scripts/repair_2026-09-16_paper_funding.py --config configs/shadow.yaml
    .venv/bin/python scripts/repair_2026-09-16_paper_funding.py --config configs/shadow.yaml --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import NoReturn

from btc_inventory_quant.config.loader import load_config
from btc_inventory_quant.config.settings import load_settings
from btc_inventory_quant.data.repositories import load_contract_spec
from btc_inventory_quant.domain.ledger import LedgerEntry, LedgerEntryType
from btc_inventory_quant.exchange.binance_coinm import BinanceCoinMAdapter
from btc_inventory_quant.storage.database import Database

REASON = "internal state differs from exchange"
NOTE = (
    "FUNDING_FEE (repair 2026-09-20: settled on 2026-09-16 00:05 UTC but dropped from the "
    "ledger by a paper income-id collision after the 09-14 restart; amount = the recorded "
    "wallet difference split by funding rate)"
)


def fail(msg: str) -> NoReturn:
    print(f"REFUSED: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/shadow.yaml")
    ap.add_argument("--apply", action="store_true", help="write; without it only report")
    args = ap.parse_args()

    config = load_config(args.config)
    settings = load_settings()
    db = Database(config.storage.database_url or settings.database_url)
    symbol = config.exchange.symbol
    spec = load_contract_spec(config.data.exchange_info_path, symbol)

    # --- what the reconciler recorded ------------------------------------------------------
    ks = db.get_state("kill_switch") or {}
    if ks.get("state") != "SAFE_HOLD" or ks.get("reason") != REASON:
        fail(f"kill switch is not in the expected state: {ks.get('state')} / {ks.get('reason')}")
    rec = db.latest_reconciliation()
    if rec is None or rec.ok or not rec.differences:
        fail("latest reconciliation is not a failure")
    diffs = list(rec.differences)
    if len(diffs) != 1 or diffs[0].get("code") != "WALLET_BALANCE":
        fail(f"expected exactly one WALLET_BALANCE difference, got {diffs}")
    internal = Decimal(str(diffs[0]["internal"]))
    exchange_wallet = Decimal(str(diffs[0]["exchange"]))
    missing = exchange_wallet - internal
    if missing <= 0:
        fail(f"the paper wallet is not ahead of the ledger (difference {missing})")
    print(
        f"reconciliation {rec.ts.isoformat()}: paper wallet {exchange_wallet} vs ledger {internal}"
    )
    print(f"missing from the ledger: {missing} BTC")

    # --- the two saved wallets must both still show the pre-drift value --------------------
    portfolio = db.get_state("portfolio") or {}
    paper = db.get_state("paper_account") or {}
    for name, state, key in (
        ("portfolio", portfolio, "wallet_balance_btc"),
        ("paper_account", paper, "wallet_balance"),
    ):
        got = Decimal(str(state.get(key, "nan")))
        if got != internal:
            fail(
                f"{name}.{key} = {got}, expected {internal} (already repaired, or something else moved)"
            )

    # --- which settlements: after the last successful save, up to the last one settled ----
    saved_at = datetime.fromisoformat(portfolio["saved_at"])
    settle_ms = int(db.get_state("last_paper_funding_settle_ms"))
    start_ms = int(saved_at.timestamp() * 1000)
    if not start_ms < settle_ms:
        fail(f"no settlement window: saved_at {saved_at.isoformat()} >= last settle {settle_ms}")
    contracts = Decimal(str(paper["contracts"]))
    if contracts >= 0:
        fail(f"paper position is not short: {contracts}")

    market = BinanceCoinMAdapter(environment="production")
    rows = market.funding_rate_history(symbol, start_ms=start_ms, end_ms=settle_ms)
    events = [
        (int(r["fundingTime"]), Decimal(str(r["fundingRate"])))
        for r in rows
        if start_ms < int(r["fundingTime"]) < settle_ms
    ]
    if not events:
        fail("the exchange returned no funding events in the window")
    print(f"settlements in ({saved_at.isoformat()}, {settle_ms}): {len(events)}")
    for t, rate in events:
        print(f"  {datetime.fromtimestamp(t / 1000, tz=UTC).isoformat()}  rate {rate}")
    for t, _ in events:
        ref = f"income:FUNDING_FEE:funding-{t}"
        if db.ledger_has_ref(ref, LedgerEntryType.FUNDING_FEE.value):
            fail(f"{ref} is already in the ledger - repaired before?")

    # --- split the recorded difference by rate; the sum is exact by construction ----------
    rate_sum = sum((r for _, r in events), Decimal(0))
    if rate_sum <= 0:
        fail(f"rates sum to {rate_sum}; a short would have PAID, not received - not this incident")
    implied_mark = -contracts * spec.contract_value_usd * rate_sum / missing
    snap = db.latest_snapshot("exchange") or db.latest_snapshot("internal")
    seen_mark = snap.mark_price if snap is not None else None
    print(
        f"implied mark from position x rates / difference: {implied_mark:.2f}"
        + (f"  (last snapshot mark {seen_mark})" if seen_mark is not None else "")
    )
    if seen_mark is not None and not (
        Decimal("0.9") * seen_mark < implied_mark < Decimal("1.1") * seen_mark
    ):
        fail("the difference is not explained by these settlements on this position")
    amounts: list[Decimal] = []
    for _, rate in events[:-1]:
        amounts.append(missing * rate / rate_sum)
    amounts.append(missing - sum(amounts, Decimal(0)))
    assert sum(amounts, Decimal(0)) == missing

    ts = rec.ts if rec.ts.tzinfo else rec.ts.replace(tzinfo=UTC)
    entries = [
        LedgerEntry(
            ts,
            LedgerEntryType.FUNDING_FEE,
            amt,
            ref_id=f"income:FUNDING_FEE:funding-{t}",
            note=NOTE,
        )
        for (t, _), amt in zip(events, amounts, strict=True)
    ]
    print("ledger entries to add:")
    for e in entries:
        print(f"  {e.ts.isoformat()}  {e.entry_type.value}  {e.amount_btc}  {e.ref_id}")
    print(
        f"portfolio.wallet_balance_btc / paper_account.wallet_balance: {internal} -> {exchange_wallet}"
    )

    if not args.apply:
        print("\ndry run - nothing written (add --apply)")
        return 0

    for e in entries:
        db.add_ledger_entry(e)
    portfolio["wallet_balance_btc"] = str(exchange_wallet)
    portfolio["repaired_at"] = datetime.now(tz=UTC).isoformat()
    db.set_state("portfolio", portfolio)
    paper["wallet_balance"] = str(exchange_wallet)
    db.set_state("paper_account", paper)
    db.add_event(
        "WARNING",
        "ledger_repaired",
        f"{len(entries)} FUNDING_FEE entries, {missing} BTC",
        {"refs": [e.ref_id for e in entries]},
    )
    print("\napplied. next: release SAFE_HOLD, then restart the agent with the fixed code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
