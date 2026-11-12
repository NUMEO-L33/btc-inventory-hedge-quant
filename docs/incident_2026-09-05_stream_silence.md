# Incident 2026-09-05 — A dead WebSocket latched SAFE_HOLD for 6 days

**Category**: operational failure (unrelated to the strategy) · **Loss**: 0 (Shadow, paper account) · **Observation lost**: all of week 1

---

## 1. What happened

| Time (UTC) | Event |
|---|---|
| 09-04 00:41 | Shadow agent starts normally, paper account restored (−162 contracts), mark price stream connected |
| 09-04 01:06 | **Last stream message.** The MacBook goes to sleep (KST 10:06) |
| 09-05 01:53 | Cycle 2 — `WS_SILENT: no stream message for 89196s (connected=True)` → **SAFE_HOLD** |
| 09-05 ~ 09-11 | Cycles 3–8 all `safe_hold`. 0 decisions. 0 alerts delivered |
| 09-11 05:50 | Found in the weekly check |

89,196 s = 24.8 hours. That is 99 times the threshold (`ws_max_silence_seconds`, 900 s).

## 2. Cause — three things coincided

### ① The socket was dead but stayed `connected=True`

`binance_ws.py` connected with `ping_interval=None`, wrapped `recv()` in a 5-second timeout, and on a timeout
quietly did `continue`. In other words, **however long the silence grew, there was no path that triggered a
reconnect.** When sleep dropped the TCP connection, `recv()` did not raise but just kept timing out, so the
stream stayed dead indefinitely in the "connected" state.

The reconnect logic did exist. But it **only ran when an exception was raised**, and this failure mode raises none.

### ② The kill switch was latched on a recoverable condition

`check_stream_silence` returned `critical=True`, and once SAFE_HOLD is engaged, `Trader.cycle()` **returns
before even evaluating the guards** (`trader.py`). Once latched, it neither attempted recovery nor re-checked
the state. Simply closing the laptop lid had put it into a state that a human had to release by hand.

Refusing to place orders on stale prices is, in itself, the right call. What was wrong was **making that irreversible**.

### ③ Alerts never left the machine

The setting was `alert_channels: [log]`. The CRITICAL alert was written only to `state/shadow.log`, and for 6 days
nobody knew. In always-on unattended operation, a log file is not an alert channel.

## 3. Why it matters

Because this was Shadow, the loss is 0; had the same thing happened in live, it would have meant **6 days frozen while
holding a real short position, no rebalancing possible, no notification**. This is why the observation period exists,
and week 1 earned its keep.

The flip side: **week 1's strategy data is unusable.** The cumulative funding of 0.0000045 BTC is effectively 1 settlement's
worth; during SAFE_HOLD, funding accrual on the paper account stopped as well. The observation clock goes back to 0 days.

## 4. What was fixed

| # | Fix | File | Rationale |
|---|---|---|---|
| 1 | **Stall detection** — if 1 iteration of the loop, which cannot take longer than `recv_timeout`, exceeds 60 s on the wall clock, treat the host as having been frozen and force a reconnect | `exchange/binance_ws.py` | Uses only the client's own clock. No assumptions about exchange behaviour |
| 2 | **Silence-based forced reconnect** — if a continuously publishing stream (`markPrice`, roughly every 3 s) is quiet for 60 s, force a reconnect | 〃 | 〃 |
| 3 | **Exclude an idle user stream from the silence verdict** — for an event-driven stream, hours of quiet are normal | 〃 | Latent bug: on a live account with no fills, the guard would have fired even though everything was normal |
| 4 | **Discard the position snapshot on reconnect** — `ACCOUNT_UPDATE` may have been missed during the gap, so the snapshot is no basis for comparison with REST | 〃 | Prevents SAFE_HOLD from engaging just because "we weren't listening" |
| 5 | **Downgrade `WS_SILENT` from critical → blocking** | `risk/guards.py` | Orders stay blocked, but it does not latch |
| 6 | **Latch only if it persists for N consecutive cycles** (`risk.stale_data_cycles_to_safe_hold`, default 3) | `runtime/trader.py` | The counter is stored in the DB — a restart does not erase the evidence |
| 7 | **Stream supervisor** — if a thread has died or fails to recover, the heartbeat restarts the bundle (with a cooldown) | 〃 | When the thread itself dies, the stream cannot fix itself |
| 8 | **Telegram alerts** | `configs/shadow.yaml` | A channel that leaves the machine |
| 9 | **`caffeinate -s`** | `scripts/launchd/*.plist` | Sleep had been pushing the decision time back by 0–113 minutes every day |

### What we could not verify

Whether Binance answers a client-initiated WebSocket ping with a pong **could not be confirmed** — in the cloud
container, WebSockets are blocked at the proxy, and the MacBook's local VM has no external DNS. So **none of the
fixes above relies on client pings.** All of them use only our own clock and our own message counters. That choice
was made so as not to build in unverified assumptions about exchange behaviour.

## 5. Regression tests

Committed **after confirming that all 10 fail on the pre-fix code**.

* `tests/integration/test_websocket_streams.py` — does a quiet socket trigger a reconnect, does an idle user
  stream avoid false alarms, is a stall gap detected, does a reconnect discard the snapshot (4 tests)
* `tests/simulation/test_stale_data_recovery.py` — does it block without latching, does it recover on its own,
  does it latch if the condition persists, does the counter survive a restart, is a dead thread restarted, do
  restarts respect the cooldown (6 tests)

## 6. What remains

* `check_ws_position` is still critical. Since the snapshot is discarded on reconnect, it will not fire from this
  cause, but look at it again on the first live run.
* Week 1 observations are discarded. Counting starts over from the restart.
* ~~Field verification pending~~ → **Field verification done (2026-09-13 01:49 UTC)**. In the 48 hours after the
  restart there were 0 `forcing stream reconnect` lines, but that was because caffeinate prevented sleep altogether,
  so the situation that would trigger the fix never arose — that removed the condition; it did not verify the fix.
  So with `scripts/test_suspend_recovery.sh` we **froze the shadow process with SIGSTOP for 90 seconds and then woke it**
  (the same thing sleep does to a process). Result:

  ```
  "forcing stream reconnect", "stream": "mark_price", "reason": "host suspended or stalled for 93s"
  system_state: NORMAL · ws_connected: True · 154 contracts held
  ```

  Stall detection fired and forced a reconnect, and SAFE_HOLD did not engage. A recovery that had taken 6 days
  under the same conditions as 9/5 was done within 20 seconds. Closing the lid and running on battery take the
  same path (process stopped → wall-clock gap detected on wake), so they need no separate verification.

## 7. Follow-up — a bug in the supervisor (fix #7) itself (2026-09-14)

On the first night of running with the lid closed, the MacBook kept going to sleep and waking up. On each wake, DNS
fails for a few dozen seconds before Wi-Fi reconnects (`gaierror: nodename nor servname provided`) and the stream waits
in backoff — that is normal. But **at 23:58 UTC, at the moment the supervisor called `restart()`, the stream thread was
in a 30-second backoff sleep.**

* `stop()` joined for only 5 seconds and gave up
* `start()` concluded that "the old thread is still alive" and **did nothing**
* Once its sleep ended, the old thread saw the stop flag and exited quietly — without a single log line
* Not until 00:16 — once the supervisor's 900-second cooldown had passed — was it found as `thread dead` and actually restarted

**For 18 minutes there was no stream**, and the 00:05 decision fell inside that window and was blocked with
`stale_data_blocked (1/3)`. It did not latch, so this was not a repeat of 9/5 — but the supervisor that was there to
keep the stream alive had killed the stream.

Fix (`de0477a`, 3 regression tests — confirmed failing on the pre-fix code; in a reproduction, `restart` took
5.0 s and started no new thread on the pre-fix code / 0.4 s with a new thread after the fix):

| # | Fix | Rationale |
|---|---|---|
| 1 | The backoff sleep checks for stop every 0.5 s | `restart()` takes effect immediately |
| 2 | `start()` waits for an old thread that is still exiting, then starts a new one | The silent no-op was the cause |
| 3 | ERROR log when the thread dies (including BaseException) | This time the reason was not in the log and had to be traced back from timing |
| 4 | A dead thread is restarted immediately, without the cooldown | The cooldown is there to prevent churn on a dead network, not for dead threads |
| 5 | `cycle()` runs the supervisor before evaluating the guards | Right after a wake, the decision runs before the heartbeat |

Lesson: **recovery paths have failure modes too.** When the 1st line (the thread's own detection) and the 2nd line
(the supervisor) set off at the same moment, the 2nd could kill the 1st. A cooldown's condition has to spell out
"what is it meant to prevent?"

## 8. Follow-up — the supervisor got ahead of 1st-line recovery and raised an alert (2026-09-14)

Between 09-13 06:31 and 09-14 12:34 UTC there were 17 wakes (excluding the SIGSTOP test). In 7 of them the Mac had
slept for longer than 900 s (the threshold), and laying the logs of those 7 side by side shows two branches.

| Wake | Handled by | Log | Alert |
|---|---|---|---|
| 07:04 · 07:38 · 07:54 · 08:11 (09-13) | Stream thread (1st line) | `forcing stream reconnect … host suspended` | None |
| 09:12 (09-13) · 23:58 (09-13) · 12:13 (09-14) | Supervisor (2nd line) | `restarting streams … silent for 958s / 1091s / 1057s` | 3 WARNINGs |

The 3 wakes the supervisor handled **have no stream-side `forcing stream reconnect` line just before them.** That is,
after the wake the heartbeat ran before the stream thread had even had its turn (`recv` timeout ≤ 5 s), reached the
verdict "silent for 1000 s, alive, not reconnecting" and restarted it. The outcome is the same (the stream comes back).
What differs is that **in 3 of the 7 long sleeps, a WARNING went to the operator**, and as such alerts pile up, real
WARNINGs (`stale_data_blocked`) get ignored. Alert fatigue is an operational risk.

The first fix that came to mind — "yield if the stream has attempted to connect since its last message" — was rejected
on the evidence of the logs. In all 3 lost races, there had **not** yet been any attempt when the supervisor looked.
However well the condition is written, as long as the verdict is reached on the first look, the race remains.

Fix: **an alive stream is never restarted on silence alone.** The supervisor notes what the stream has done (last message,
last connection attempt) and restarts only when that has **stayed unchanged for 180 s of awake time**.

* The first look always yields — the wake race itself disappears
* As long as connection attempts keep coming (even with no network for hours, it attempts every 30–40 s), it is left
  alone — previously, during a long outage, a restart + alert was repeated every 900 s
* Awake time is measured as the sum of the intervals between polls, but gaps far longer than the poll interval (sleep)
  are not added — while asleep, the stream thread could not have attempted anything either
* Basis for 180 s: in a healthy loop the longest interval between attempts is about 76 s (30 s backoff + 10 s connect
  timeout, or 60 s silence + 5 s recv + the connect). 3 minutes without a single attempt means the loop has stopped, not
  that the network is slow
* A dead thread (`alive() == False`) is restarted immediately, as before

7 regression tests (`test_stale_data_recovery.py`, confirmed failing on the pre-fix code): no restart on the first look
(reproducing the 12:13 case, 1057 s, exactly), restarts over 30 minutes of continuing attempts: 0, a restart within
3–4 minutes once attempts stop, sleep gaps not counted as stall time, the cooldown applied if it stalls again after a restart.

## 9. Found in analysis — if asleep at the decision time, that day's decision is lost

Found while investigating §8; this has **not happened yet**. The decisions of 09-12/13/14 all ran at 00:05:01 — on all
three mornings the MacBook was awake. Had it not been:

1. The process that was asleep runs `cycle()` the moment it wakes (because the decision time has passed)
2. At that moment there is usually no Wi-Fi. REST uses up its retries (≈15 s) and raises `ExchangeUnavailable` → result `error`
3. The loop **sleeps until the next decision time (tomorrow).** There is no decision that day
4. `consecutive_data_errors` goes up by 1; **3 such mornings in a row means SAFE_HOLD** — the same ending as week 1
   but this time brought about by a Wi-Fi connection that comes back by itself 30 seconds later

Fix: a decision that ended in `error`, or in `blocked` with stream staleness as its only reason, is **retried every
`decision_retry_seconds` (600 s) until the next decision time**. `cycle()` is idempotent per candle, so there is no risk
of re-running a decision that succeeded. A decision blocked by account settings (margin type, leverage) is not retried —
that is not the kind of thing that fixes itself 10 minutes later.

Along with this, the two "3 consecutive failures → SAFE_HOLD" counters (connection failures, stale data) were changed to
count **per decision slot (day)**. 3 retries in one morning are one bad day, not the "three bad days" this escalation
condition is meant to catch. Alerts are also 1 per slot. 6 regression tests (`test_decision_retry.py`): retry after 10
minutes, a successful decision waits until tomorrow, 3 retries in one outage = 1 alert and 1 failure, three days in a row
still latch, classification of what gets retried, stale → the stream recovers by itself → retry succeeds (0 restarts, 1 alert).

~~To be honest about it: this fix came from reading the code, not from an incident. It does not mean it has been verified
in the field.~~ → **Field verification done (2026-09-29, 09-30).** On two days in a row the MacBook was asleep at the
decision time, and right after waking both agents failed with `GET …: ConnectError`. The log lines, in order:

```
09-29 00:42:10  ALERT exchange_unavailable (1 per slot)
09-29 00:42:10  decision will be retried  at 00:52:10
09-29 00:53:04  cycle done  action=none            ← testnet · shadow at 00:53:27
09-30 00:05:51  ALERT exchange_unavailable
09-30 00:34:08  exchange still unavailable on retry  bad_slots=1   ← no alert, counter unchanged
09-30 00:44:18  cycle done  action=none            ← shadow
```

Without the retry, the connection-failure counter would have reached 2 and two days' decisions would have been lost.
The testnet retry on 09-30 ran into the clock-skew guard instead of making a decision, but that belongs to the next
incident document ([incident_2026-09-30_testnet_clock_listenkey.md](incident_2026-09-30_testnet_clock_listenkey.md)).
