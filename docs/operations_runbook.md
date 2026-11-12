# Operations runbook

## 0. Prerequisites

```bash
bash scripts/setup_macos.sh                                   # macOS: check Python 3.12 → venv → install → verify data → tests
source .venv/bin/activate
# Manual (any OS): python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
cp .env.example .env                                          # enter keys, never commit (the setup script creates it automatically)
python -m btc_inventory_quant data verify                     # raw data checksums
python -m pytest -q                                           # 119 tests
```
Always-on operation on macOS: `scripts/launchd/com.numeo.btc-inventory-quant.shadow.plist` (installation steps at the top of the file) → `launchctl load`, then `tail -f state/shadow.log`.

## 1. Backtest

```bash
python -m btc_inventory_quant backtest --config configs/backtest.yaml
python -m btc_inventory_quant research --config configs/backtest.yaml walk-forward
python -m btc_inventory_quant research --config configs/backtest.yaml sensitivity
```
Output: `reports/<run_id>/backtest_report.md`, `summary.json`, CSV, `charts/`.

Data refresh (on a machine that can reach Binance):
```bash
python -m btc_inventory_quant data --raw-dir data/raw download          # full re-download + manifest(sha256)
python -m btc_inventory_quant data --raw-dir data/raw download --include-8h-mark   # more precise mark prices at funding times (optional, docs/backtest_methodology.md §6-2)
```

## 2. Shadow (real data + simulated account, real orders impossible)

```bash
python -m btc_inventory_quant shadow --config configs/shadow.yaml            # decision daily at 00:05 UTC, runs continuously
python -m btc_inventory_quant shadow --config configs/shadow.yaml --once     # run 1 cycle
python -m btc_inventory_quant status --config configs/shadow.yaml
```
Always-on operation (macOS, recommended) — starts automatically at login and restarts if it dies:
```bash
bash scripts/install_shadow_agent.sh              # install + start (includes a config pre-check)
tail -f state/shadow.log                          # log
bash scripts/install_shadow_agent.sh --uninstall  # stop
```
Shadow **needs no API key** — it reads real-time public market data and the account is simulated, so real orders are structurally impossible.

Send alerts off the machine (recommended). In `.env`:
```
BIQ_TELEGRAM_BOT_TOKEN=...      # @BotFather → /newbot
BIQ_TELEGRAM_CHAT_ID=...        # send the bot any message, then take chat.id from getUpdates
```
`configs/shadow.yaml` already has `alert_channels: [log, telegram]`. Without credentials it only logs a
warning and keeps running on the log channel — the install script points this out in its pre-check.

**Weekly check items** (`status --config configs/shadow.yaml`):

| Field | Normal | If abnormal |
|---|---|---|
| `system_state` | `NORMAL` | If `SAFE_HOLD`, check `safe_hold_reason` |
| `latest_decision.ts` | Within the last 24 hours | If stale, the daemon has stopped — `state/shadow.log` |
| `latest_reconciliation.ok` | `true` | Internal ledger and paper account disagree |
| `unresolved_orders` | `[]` | Unconfirmed orders remain |
| `websocket_connected` | `true` | **Do not judge by this value alone** — a dead socket also reports `true` ([incident](incident_2026-09-05_stream_silence.md)). Also check how often `forcing stream reconnect` appears in the log |
| `recent_events` | No ERROR | Whether reconnects or market-data delays keep recurring |

**Stale data no longer triggers SAFE_HOLD immediately.** If the stream goes quiet, only that cycle's orders are
blocked (`action: blocked`), and the stream reconnects on its own. Only if it fails to recover for `risk.stale_data_cycles_to_safe_hold` (default 3)
consecutive cycles does it latch. Background: [incident
2026-09-05](incident_2026-09-05_stream_silence.md).

If SAFE_HOLD has been triggered, identify the cause first and release it only after the cause has been resolved:
```bash
python -m btc_inventory_quant status --config configs/shadow.yaml    # safe_hold_reason
python -m btc_inventory_quant safe-hold --config configs/shadow.yaml \
    --release --ack I_ACCEPT_COIN_M_TRADING_RISK
```

The agent runs Python wrapped in `caffeinate -s`. This is because if the machine sleeps, that day's decision runs when the lid is
opened, breaking the backtest assumption (fill at 00:05 UTC). To check:

```bash
ps aux | grep "[c]affeinate"                        # caffeinate -s ... shadow should be listed
pmset -g assertions | grep -i -A1 PreventSystemSleep
```

`-s` creates a `PreventSystemSleep` assertion — not `PreventUserIdleSystemSleep`. If you grep for the latter,
you see only macOS's own "while display is on" assertion (powerd), not ours.

**What this does not prevent**: `-s` takes effect **only while on AC power** (macOS ignores it on battery),
and **the Mac still sleeps when the lid is closed**. In other words, sleep itself cannot be eliminated. The closed-lid case is handled by the stream's stall
detection — after waking it forces a reconnect, and a stale verdict does not latch. If the machine was asleep at decision time (00:05 UTC),
the decision runs the moment it wakes, and at that point there is usually no Wi-Fi yet. So a decision that ended in an exchange
connection failure or a stale stream is **retried every `decision_retry_seconds` (600 s) until the next decision
time**. Retries on the same day count as only 1 alert and 1 failure — retries never accumulate
into a SAFE_HOLD.

**What Shadow tells you and what it does not**: the *performance* of the 20% static hedge is already known from the backtest. The new information gained here concerns the **operational layer** — whether the daemon survives for weeks, whether reconnects work, whether the ledger stays in agreement. Observe for at least 2–4 weeks before the next step.

Week 1 (2026-09-04 ~ 09-11) ended with **3 operational bugs caught and 0 days of strategy observation**. The record is in
[docs/incident_2026-09-05_stream_silence.md](incident_2026-09-05_stream_silence.md).
Week 2 (09-11 ~ 09-20) brought 2 bugs in the recovery path (same document, §7–§9) and an id collision in the paper exchange, resulting in
**4 days of SAFE_HOLD** ([docs/incident_2026-09-16_paper_income_ids.md](incident_2026-09-16_paper_income_ids.md)).
The observation clock restarts from the recovery point (09-20). In week 3 (09-27~30), testnet caught 3 bugs in the real-account path
(income query, clock-skew latch, listenKey expiry — `incident_2026-09-30_testnet_clock_listenkey.md`); shadow ran incident-free.

## 3. Testnet (demo)

1. Issue an API key in Binance Demo Trading (official FAQ procedure; whether it applies to the COIN-M demo is PARTIAL → confirm from the first run's report).
2. Enter `BIQ_BINANCE_DEMO_API_KEY/SECRET` in `.env`.
3. Account settings: One-way mode, BTCUSD_PERP Cross, leverage 1. **Confirm directly in the exchange UI that the API key has no withdrawal permission and has an IP whitelist** (withdrawal permission cannot be queried through the COIN-M API).
4. `python -m btc_inventory_quant testnet --config configs/testnet.yaml --once` → check the `contract_spec`, `order_recovery` and `portfolio` items in the startup report.
5. After starting continuous operation, confirm with `reconcile` that the state agrees.
6. **Funding formula verification (top priority)**: after a settlement time (00/08/16 UTC) has passed, run `python -m btc_inventory_quant verify-funding --config configs/testnet.yaml`.
   If `verdict: VERIFIED`, the PARTIAL in `docs/inverse_math.md` §6 is resolved. Detailed procedure: **[docs/demo_verification_runbook.md](demo_verification_runbook.md)**.
7. **Continuous operation (alongside shadow)**: `bash scripts/install_agent.sh testnet` — a separate launchd agent
   (`com.numeo.btc-inventory-quant.testnet`) with its own DB (`state/testnet.sqlite3`) and log (`state/testnet.log`).
   Where shadow verifies the decision and recovery layers, testnet verifies the **real adapter path** — signed orders,
   exchange-assigned ids, the real-account user stream, order recovery. As the 09-16 incident (id collision in the paper exchange) showed,
   a simulated exchange cannot verify itself. The two agents' Telegram alerts are distinguished by the `[shadow]`/`[testnet]`
   prefixes. What even the demo does not verify: production fee tier and liquidity, actual funding rates, production
   account settings and key permissions. Demo quirks: no `fundingRate` history; `income` returns an empty array without `incomeType`
   (§18, 2026-09-27 — this finding changed the reconciliation logic to per-type queries).
   The clock guard bases its verdict on the **production public `/time`**, not the demo host (shadow does the same; there were days when the demo
   `/time` appeared 2 s ahead at 00:05 UTC — §20). Every decision's `clock check` log records both offsets.

## 4. Live (small size)

1. Copy `configs/live.example.yaml` → `configs/live.yaml` (git-ignored); set `live_trading_enabled: true` and `acknowledgement: I_ACCEPT_COIN_M_TRADING_RISK`.
2. `.env`: `BIQ_ENABLE_LIVE_TRADING=true`, `BIQ_LIVE_ACKNOWLEDGEMENT=I_ACCEPT_COIN_M_TRADING_RISK`, production keys (no withdrawals, IP whitelist).
3. Sub-account equity ≤ `max_initial_equity_btc` (default 0.05 BTC).
4. `python -m btc_inventory_quant live --config configs/live.yaml --confirm-live`
   * If the gate fails, the log prints `LIVE GATE FAILED` and the failed items, and the program runs read-only.
5. After the first order, run `reconcile` and compare against the exchange UI.

## 5. Routine checks

| Frequency | Item | Command |
|---|---|---|
| Daily, after 00:10 UTC | Decision log, orders, hedge ratio error | `status` |
| Daily | Internal/exchange agreement | `reconcile` |
| Weekly | Ratio of measured-live to computed funding (≈1) | DB `ledger_entries`, income |
| Weekly | Contract spec changes, fee rate (`commissionRate`) | Startup report |
| Monthly | Re-run the backtest (new data) | `backtest` |

## 6. Manual intervention

```bash
python -m btc_inventory_quant safe-hold --config configs/live.yaml                      # immediate SAFE_HOLD (cancel open orders, keep the position)
python -m btc_inventory_quant safe-hold --config configs/live.yaml --release --ack I_ACCEPT_COIN_M_TRADING_RISK
```
Check the cause of a SAFE_HOLD in `safe_hold_reason` from `status` and in the `events` table. Before releasing, `reconcile` must be OK.

## 7. Docker

```bash
docker compose up -d shadow          # run shadow mode continuously
docker compose run --rm backtest     # backtest
```

## 8. Logs and secrets

* Logs are JSON (`BIQ_LOG_JSON=true`); API keys, signatures and listenKeys are masked automatically.
* `.env`, `configs/live.yaml`, `state/` and `*.sqlite3` are not committed to git.
