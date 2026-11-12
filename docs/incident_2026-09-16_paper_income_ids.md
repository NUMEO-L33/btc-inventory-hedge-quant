# Incident 2026-09-16 — Paper account income id collision: SAFE_HOLD for 4 days

**Category**: operational failure (a bug in a Shadow-only component, unrelated to the strategy) · **Loss**: 0 (paper account) ·
**Observation lost**: 4 days of decisions, 09-16 ~ 09-20 (no decisions during SAFE_HOLD) · **Discovered**: 09-20 weekly check

---

## 1. What happened

| Time (UTC) | Event |
|---|---|
| 09-14 13:00 | Agent restarted to deploy the supervisor fix (7fdd2d5). Paper account restored (−154 contracts) |
| 09-15 00:05 | Cycle 2 normal — 2 funding settlements, recorded in the ledger; reconciliation OK |
| 09-16 00:05 | Cycle 3 — 3 funding settlements. **0** recorded in the ledger. Reconciliation: `WALLET_BALANCE differs by 0.0000388 BTC` → **SAFE_HOLD** |
| 09-16 ~ 09-20 | Cycles 4–7 all `safe_hold`. 1 CRITICAL sent via Telegram |
| 09-20 00:10 | Found in the weekly check |

The kill switch engaged **correctly**. The internal ledger and the exchange (paper) wallet really did differ. The question is
why they came to differ.

## 2. Cause

The ledger deduplicates exchange income records by `income:<type>:<id>` and fills by `trade:<id>`. A real exchange issues
globally unique ids. **The paper exchange issued ids from an in-memory counter (`itertools.count(1)`).**

```
First process (09-11~09-14 01:12)   order 1 · fill 2 · fee 3 · funding 4,5,6 (09-12) · 7,8,9 (09-13) · 10,11,12 (09-14)
Restart (09-14 13:00)               counter goes back to 1
                                    funding 1 (09-14 13:00) · 2,3 (09-15)
09-16 00:05                         funding 4,5,6  ←  ledger: "income:FUNDING_FEE:4 already recorded on 09-12" → skipped ×3
```

The three settlements (0.0000388 BTC) went into the paper wallet but not into the ledger. That is 4 times the tolerance (0.00001 BTC).

What matters is that just adding the counter to the saved account state (`paper_account`) is not enough. The state is saved
**at the end of a successful cycle**, so ids issued in an interrupted cycle are not saved. 09-16 is exactly that case — the
cycle that issued ids 4,5,6 was cut short at reconciliation.

## 3. Fix (`exchange/paper.py`)

| # | Fix | Rationale |
|---|---|---|
| 1 | Make the funding income id the **funding time itself** (`funding-<fundingTime>`) | 1 settlement = 1 id, unique regardless of the counter |
| 2 | Save the counter with the account state, and on restore **floor it at the current epoch ms** | Even if the saved counter is stale, it is always greater than the previous process's ids |

3 regression tests (`tests/simulation/test_paper_restart_ids.py`) — confirmed failing on the pre-fix code. One of them reproduces
the 09-16 sequence exactly: open the hedge → two days of funding → restart → four days of funding. Before the fix it latches with
`internal state differs from exchange` on the second day after the restart; after the fix the ledger and the wallet match to the
satoshi.

## 4. Recovery (`scripts/repair_2026-09-16_paper_funding.py`)

The script inserts the three missing entries into the ledger and adds the same amounts to the two saved wallets (`portfolio`,
`paper_account`). **No number is made up** — the total is exactly the difference left in the reconciliation record, and the
split across the three is proportional to the actual funding rates fetched from the exchange (the same way the paper exchange
computed them: same position × same mark × the three rates). As a cross-check, it back-solves the mark from "position × sum of
rates / difference", compares it with the mark the cycle saw, and refuses if it is more than 10% off. The default is dry-run;
applying twice is refused.

After the repair, SAFE_HOLD is released **by a human** with `--ack`. The procedure is a single run of `state/repair_2026-09-16.sh`.

## 5. Remaining limitations

* Paper funding is computed with **the mark at the moment the settlement is executed**, not the mark at the settlement time
  (`/dapi/v1/fundingRate` has no markPrice). Settling every day at 00:05 keeps the error small, but settling four days' worth at
  once, as here, is off by as much as the mark moved over those four days (around 1e-5 BTC this time). This is a paper-only
  problem; a real account receives exactly the amount the exchange settled.
* After a process restart, the `cumulative_funding_btc` metric shows only the accumulation **since that process started** (the
  in-memory ledger, not the whole ledger). The exact cumulative figure is the sum of DB `ledger_entries`.

## 6. Lessons

The uniqueness of an identifier **must be guaranteed by whoever generates it.** The ledger was designed on the belief that
exchange ids are unique, and on the live exchange that belief is correct. When a test component is slotted into that place, you
have to check that it keeps the same contract — the paper exchange kept the safety contract "no real orders go out" but not the
contract "ids are unique", and that difference cost four days.
