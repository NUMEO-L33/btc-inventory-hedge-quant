# Incident 2026-09-30 — testnet: SAFE_HOLD on clock skew right after wake, and a user stream dead for two days

**Category**: 2 operational failures (real adapter path, demo account) · **Loss**: 0 · **Observation lost**: 1 day of testnet decisions (09-30) ·
**Discovered**: Telegram CRITICAL alert → relayed by the user (around 09-30 01:00 UTC)

The point of adding the testnet agent was "to find, with no money at stake, the problems on the real-account path that the
simulation cannot catch", and in its second week it found two more. Both would have happened exactly the same way in live.

---

## 1. What happened

| Time (UTC) | Event |
|---|---|
| 09-28 01:47 | The testnet user stream's keepalive `PUT /dapi/v1/listenKey` starts being rejected with `-1125 This listenKey does not exist`. **From then on the same error repeats every 5 seconds — about 30,000 lines over two days** |
| 09-29 00:42 | MacBook asleep at the decision time → `ConnectError` right after wake → retry → decision succeeds at 00:53 (first field verification of the retry path) |
| 09-30 00:05 | Same situation. 00:05 fails, 00:34 fails, **on the 00:44:11 retry `CLOCK_SKEW: server offset 1302 ms` → critical → SAFE_HOLD** |
| 09-30 01:0x | Telegram `[testnet] [CRITICAL] safe_hold_engaged` |

shadow got through the same two mornings safely thanks to the retry (decisions at 00:53:27, 00:44:18). The clock guard always
gets 0 from the paper exchange, so testnet was the first time it met real conditions.

## 2. Cause ① — clock skew was latched immediately

`check_clock` was unconditionally critical whenever |skew| > 1000 ms. But the 1302 ms was measured 3 seconds after the network
came back. When a MacBook wakes from sleep, its clock is commonly off by 1–2 seconds until NTP corrects it again, and the
measurement itself can be inflated by a few hundred ms on freshly connected Wi-Fi with asymmetric round-trip latency. On top of
that, the adapter already corrects for the measured offset when signing, so requests go out fine.

In other words, **the same shape of mistake as 09-05**: a condition that clears by itself within minutes was turned into a state
that a human has to release.

Fix:

| # | Fix | File |
|---|---|---|
| 1 | Make the guard 2-tier — over 1000 ms **blocks** (critical=False); only over `critical_clock_skew_ms` (60 s) latches immediately | `risk/guards.py`, `config/schema.py` |
| 2 | Blocking follows the same rules as stale data — 1 alert per slot (`clock_skew_blocked`), latch after `clock_skew_cycles_to_safe_hold` (3) consecutive slots. Counter in the DB (`clock_skew_cycles`) | `runtime/trader.py` |
| 3 | A decision blocked by `CLOCK_SKEW` alone is eligible for a retry 10 minutes later (`TRANSIENT_BLOCK_CODES`) | 〃 |
| 4 | Clock measurement takes the midpoint of the **shortest round trip** out of 3 — mitigates inflation from asymmetric latency | `exchange/binance_coinm.py` |

Replaying the 09-30 sequence now gives: 00:44 retry → 1302 ms → block + 1 WARNING → 00:54 retry → (after NTP has corrected the
clock) decision. 4 regression tests (`tests/simulation/test_clock_skew.py`; 3 of them failed on the pre-fix code).

## 3. Cause ② — two days hanging on to an expired listenKey

The user stream was built to cache the listenKey, send a `PUT` keepalive every 30 minutes, and get a new key
**when a `listenKeyExpired` event arrived**. But the key dies on the server after 60 minutes without a keepalive (official
docs), and that is exactly what happens when the MacBook sleeps for more than 60 minutes. After waking:

* `PUT` → `-1125` (key does not exist). The code only logged the error and, **leaving the key as it was**, sent the same `PUT`
  again at every subsequent recv timeout (5 s). A `listenKeyExpired` event does not arrive over a dead socket.
* Even when stall detection forced a reconnect, `_url()` reused the cached key, so it reconnected with the dead key.

Result: from 09-28 01:47 there was effectively no user stream. The daily decision was unaffected because REST is its primary
source (and the position cross-check discards the snapshot on reconnect, so there were no false alarms either); what was left
instead was 30,000 log lines and "a dead stream displayed as alive". In live, fill and account events would have gone unheard
for two days.

Fix (`exchange/binance_ws.py`, based on official docs §6.1):

| # | Fix | Basis (verbatim doc text) |
|---|---|---|
| 1 | **`POST /dapi/v1/listenKey` on every connect** | "Doing a POST on an account with an active listenKey will return the currently active listenKey and extend its validity for 60 minutes" — if the key is alive, the same key (extended); if dead, a new key. Weight 1 per reconnect |
| 2 | If the keepalive is rejected with `ExchangeAPIError` (-1125 etc.), mark the key expired and reconnect → new key | A rejected key cannot be revived by a keepalive |
| 3 | If the keepalive fails with a network error, retry **after 60 s** instead of 5 s | Prevents error floods |

2 regression tests (`tests/integration/test_websocket_streams.py`): rejected keepalive → new key requested and no repetition;
POST on every reconnect. Both confirmed failing on the pre-fix code.

## 4. Recovery

After applying the bundle (restarting both agents), a human released the testnet SAFE_HOLD with `--ack`. Nothing in the ledger
needs touching — this time a guard tripped, not reconciliation, so the wallet matches. On restart the user stream gets a new key.

## 5. Incidental confirmations

* **Field verification of the decision retry path (09-05 doc §9) done** — on both days (09-29·09-30) and for both agents.
* testnet made 60-odd stall-detection reconnects over the day of 09-29 alone. It was a day of the MacBook's lid being opened and
  closed over and over, and there were 0 supervisor restarts. The 09-14 fix holds up under load.
* The demo account's `GET /dapi/v1/time` also returned `ConnectError` right after wake — without the retry path, two days'
  decisions would have been lost.

## 6. Lessons

Both are cases where the assumption **"we handle it when the event arrives"** broke. NTP will surely fix the clock on its own;
an expiry event will surely come for the key — but a MacBook waking from sleep first shows the state from *before* those events
arrive.
The rule that a recoverable condition is (1) blocked, (2) retried and (3) latched only if it persists for a long time has now been
applied for the third time: streams (09-05), decisions (09-14), clock (09-30). From now on, when adding a new critical guard, ask
first: "Will this condition still be the same 10 minutes from now?"

## 7. Follow-up — the ledger/wallet split left behind by that SAFE_HOLD cycle (found 2026-10-10)

At the 10-10 check, testnet showed **ledger sum 0.01001038 BTC ≠ saved wallet 0.01001005 BTC** (difference
0.00000033). Reconciliation had been OK every day — the difference was 1/30 of the tolerance (0.00001), so it did not show.

Trace (reconstructed from the DB snapshots and ledger rows):

| Time (UTC) | Wallet (saved) | Ledger sum | Event |
|---|---|---|---|
| 09-29 00:53 | 0.01000809 | 0.01000809 | Normal cycle, saved |
| 09-30 00:44 | 0.01000809 | **0.01000842** | The cycle from §2 above: wrote 3 funding settlements (1.1e-7, 1.2e-7, 1.0e-7) to the ledger DB and added them to the in-memory wallet, **then** SAFE_HOLD from the clock guard → never reached the end-of-cycle save |
| 09-30 01:19 | 0.01000809 | 0.01000842 | Restart for the fix deploy. Wallet restored to the last saved value. At startup, income sync skipped those 3 as "already in the ledger" → **permanent split** |
| From 10-01 | 0.00000033 below the ledger, carrying on normally every day | | |

This is not a clock bug but a **save-timing** bug. The cycle writes ledger rows immediately (durably), but saved the wallet
*only when the cycle finished successfully*. Any early return in between — SAFE_HOLD, a reconciliation mismatch, "candle
already decided" — followed by a restart does the same thing. Run the same sequence in simulation on a 1 BTC account and the
funding that vanishes is 0.000113 BTC, which exceeds the tolerance, so **SAFE_HOLD engages again right after the restart, on a
reconciliation mismatch**. In other words, on a real account it would have shown up as "release SAFE_HOLD → restart → immediate
SAFE_HOLD".

Fix:

| # | Fix | File |
|---|---|---|
| 1 | Save the wallet (and the paper account) **right after** applying income to the ledger — both in the cycle and at startup | `runtime/trader.py` |
| 2 | Write the ledger row **first**, then move the in-memory wallet — so that if the write fails, the wallet does not count money that is not in the ledger | `execution/reconciliation.py` |
| 3 | At startup, check the saved wallet against the **ledger sum (from the most recent OPENING_BALANCE)**; if they differ, rebuild it from the ledger value, send a WARNING `ledger_wallet_repaired` alert and record it in the startup report. The verdict is still left to the exchange reconciliation that follows | `runtime/trader.py`, `storage/database.py` |

The wallet is a cache of the ledger, and the ledger is a 1:1 record of exchange events (deduplicated by exchange id), so when the
two diverge, the cache is rebuilt. But not silently. Tolerance 1e-12 BTC — on real data, the Decimal summation-order noise
accumulated over 3 weeks is 2e-27; a missing row is 1e-8 at minimum.

4 regression tests (`tests/simulation/test_ledger_wallet_consistency.py`): latched cycle + restart (the 09-30 sequence exactly),
startup with a lagging saved value → rebuild + alert, no double counting when re-adoption leaves two OPENING_BALANCEs, wallet
unchanged when the ledger write fails. 3 confirmed failing on the pre-fix code (the re-adoption test is there to keep the new
logic from misbehaving). Result of running the new logic on copies of the real DBs: shadow difference 2e-27 (normal), testnet
3.3e-7 → to be rebuilt.

Lesson: one more question to add to the one in §6 — **"If this path ends partway, do what has been persisted and what exists
only in memory diverge?"** It should have been asked every time an early return was added.

## 8. Follow-up 2 — the "2 seconds" at 00:05 (2026-10-14)

Three times (10-04·10-12·10-14), testnet's 00:05:00 decision raised a `clock_skew_blocked` WARNING (1/3). The 2-tier guard from
§2 only blocked, as intended, and reset the counter to 0 the next day. All three were days with nothing to trade
(`below min_rebalance_diff`), so no order was actually blocked. Why it still cannot be left alone: three days in a row means
SAFE_HOLD under the latch rule, and that may **not be our clock's fault.**

| Date (UTC) | Offset vs demo `/time` | Conditions |
|---|---|---|
| 10-04 00:05:00 | +2025 ms | Ran on schedule (awake); no wake or reconnect log in the preceding 1 hour |
| 10-12 00:05:00 | +2066 ms | 〃 |
| 10-14 00:05:00 | +2048 ms | 〃. Cycle start → alert logged: 270 ms (including 3 round trips → shortest round trip ≤ 90 ms) |
| The other 10 days of 10-02 to 10-14 | Within 1000 ms (values not recorded) | Same time of day |
| 6 measurements at restarts (09-30 to 10-12) | 7–88 ms | Various times |

How this differs from §2: 09-30 was **3 seconds after a wake** and went away within minutes. This time the MacBook was awake,
and measurement noise (half the shortest round trip, ≤ 45 ms) cannot produce 2 seconds. So at that moment either the demo host's
clock was 2 seconds ahead or the MacBook's clock was 2 seconds behind. **Which one is not yet known** — with only the demo offset
measured, the two could not be told apart.

Restating the question: what the clock guard asks is "**is our clock right**" (candle boundaries, decision time); "how many ms
are we off from the exchange" is something the adapter already corrects for signing. In live the two are the same question
(exchange = production), but on testnet the demo host had been acting as the reference clock.

Fix:

| # | Fix | File |
|---|---|---|
| 1 | A **reference clock** for the trader — the guard reaches its verdict on the offset from it. testnet: production public `/time` (no key, 1 retry, 5 s timeout). shadow: reuses the production adapter it already reads prices from — **shadow, too, goes through the clock guard for real for the first time** (the paper account has no clock, so it was always 0). live: none (the exchange is production) — behaviour unchanged | `runtime/trader.py`, `runtime/app.py` |
| 2 | If the reference clock cannot be reached, log a warning and **reach the verdict on the exchange offset** (previous behaviour). A second opinion must not become a new failure path | `runtime/trader.py` |
| 3 | A `clock check` log every cycle: exchange offset and round trip, reference offset and round trip. If the two disagree by more than `max_clock_skew_ms`, WARNING `exchange clock disagrees with the reference` | 〃, `exchange/binance_coinm.py` (`time_sync_rtt_ms`) |
| 4 | `reference_clock_offset_ms` in the startup report (`system_start`) | `runtime/trader.py` |

Signing still uses the demo offset — even if the demo is 2 seconds ahead, the request timestamps match the demo clock.

From the next 00:05 onward, the verdict can be read as follows:

* `exchange_offset_ms ≈ +2000`, `reference_offset_ms ≈ 0` → **the demo host's side**. The guard passes (correctly), and only a
  warning log is left. Update `exchange_api_verification.md` §20 to confirmed.
* Both ≈ +2000 → **the MacBook clock's side**. The guard keeps blocking (correctly — it would have to block in live too), and
  shadow is blocked on the same day as well. In that case, macOS time sync is what needs looking into.

9 tests: demo alone +2048 · production +12 → even three days in a row, blocks/alerts: 0 / both ≈ +2050 → block / shadow
(offset 0) + reference −1302 → block / reference unreachable → verdict on the exchange (same as before) / the adapter remembers
the shortest round trip / wiring, 4 tests (live=none, testnet=keyless production·retries ≤ 1, shadow=reuse of the existing
adapter, replay=none). Confirmed that reverting the guard to ignore the reference makes 3 of them fail.

A side effect to be aware of: since shadow is now judged against a real clock, on mornings when the MacBook's clock is off by
more than 1 second, as right after a wake, shadow can also get `clock_skew_blocked`. This is what live would go through on the
same morning, so it is an intended change.

**Verdict (2026-10-16 00:05)**: in the same cycle, testnet saw demo +2126 ms (round trip 49 ms) · production +2125 ms (round trip
48 ms), and shadow saw production +2125 ms (round trip 48 ms). The two servers agree within 1 ms — **the MacBook's clock was 2.1
seconds slow; it was not a demo host problem.** The guard blocked on both agents as intended (1/3, shadow's first
`clock_skew_blocked`) and cleared the next day at +56 ms. It was a day with nothing to trade, so the impact was 0. 4 of the 17
on-schedule decision days (10-04·12·14·16), none consecutive. Other days: 4–88 ms.

These 2 seconds have no functional effect on signing (corrected with the measured offset) or on the daily-candle decision (run 5
minutes after the close), but three days in a row would latch. So the guard threshold (1000 ms) is left as it is, and the cause —
the Mac's automatic time sync — is looked at first. Widening the threshold is a decision that would need its own justification
(what is the guard protecting?), and if live also runs on the same Mac, it will go through the same mornings.

