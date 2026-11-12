# Incident Response

Principle: **closing out the entire short is not a response.** Stop new orders → cancel open orders → keep the position → re-query state → compare → confirm they match → alert.

## 1. Automatic SAFE_HOLD triggers

| Trigger | Detected in | Code |
|---|---|---|
| Price/mark stale | `Guards.check_mark_freshness` | STALE_MARK |
| Closed candles stale | `check_candle_freshness` | STALE_CANDLES |
| Exchange time skew | `check_clock` | CLOCK_SKEW |
| Exchange position ≠ DB | `Reconciler.compare` | POSITION_CONTRACTS / ENTRY_PRICE / WALLET_BALANCE |
| Unknown orders | `check_unknown_orders`, `OrderManager.recover` | UNKNOWN_ORDERS |
| Short > limit | `check_hedge_cap` | HEDGE_CAP_EXCEEDED / LEVERAGE_EXCEEDED |
| BUY > short | `validate_order_against_position` (blocked before the order is placed) | BUY_EXCEEDS_SHORT |
| Flip to long | `check_position`, post-trade verification | LONG_POSITION |
| Balance mismatch | `Reconciler.compare` | WALLET_BALANCE |
| Repeated order rejections | `check_rejects` | REPEATED_REJECTS |
| Contract spec change | `check_contract_spec` | CONTRACT_SPEC_CHANGED |
| Maintenance margin spike | `check_margin_buffer` | MARGIN_RATIO |
| Unexpected deposit/withdrawal | `check_unexpected_transfers` | UNEXPECTED_TRANSFER |
| Order outcome unknown | `OrderManager.resolve_uncertain` | order_unresolved |
| Consecutive connection failures (3 consecutive decision days — retries on the same day count as 1) | `Trader.cycle` | exchange_unavailable |
| Unknown exception | `Trader.cycle` | unexpected error |

## 2. Response procedure

### 2.1 On receiving a SAFE_HOLD alert
1. Check the cause with `status` (`safe_hold_reason`, `recent_events`).
2. In the exchange UI, check positions, open orders, recent fills and deposits/withdrawals.
3. Run `reconcile` → get the list of differences.

### 2.2 Action by cause
* **STALE / CLOCK / connection failure**: check the network and NTP. Once recovered, confirm `reconcile` is OK, then release.
* **POSITION_CONTRACTS / WALLET_BALANCE**: check for manual trades, deposits/withdrawals or a liquidation. Once the cause is identified, either (a) record the manual trades in the ledger, or (b) delete the DB state and re-adopt the exchange state (first-start procedure: delete the `app_state.portfolio` key and restart → `adopted_from_exchange`).
* **UNKNOWN_ORDERS**: already cancelled. Find out who placed them. Block other bots / manual trading.
* **order_unresolved**: check the order status on the exchange by `origClientOrderId`. If it was filled, update the DB order to `FILLED` and re-run `recover`. If it does not exist, `NOT_PLACED`.
* **UNEXPECTED_TRANSFER**: confirm whether the deposit/withdrawal was intended. It is already recorded in the ledger as an external flow (not profit).
* **CONTRACT_SPEC_CHANGED**: verify the math against the new spec, then update `data/raw/exchange_info.json`, and resume once the tests pass.
* **HEDGE_CAP_EXCEEDED**: if a price spike shrank the assets and pushed the ratio up → after release, the next cycle's rebalancing (BUY reduce-only) takes care of it. No manual intervention needed.

### 2.3 Release
```bash
python -m btc_inventory_quant reconcile --config configs/live.yaml    # must be ok=true
python -m btc_inventory_quant safe-hold --config configs/live.yaml --release --ack I_ACCEPT_COIN_M_TRADING_RISK
```

## 3. Restart recovery (process crash / server reboot)

Performed automatically at startup: time sync → contract spec check → query unresolved DB orders (`recover`) → exchange snapshot → compare with the DB → SAFE_HOLD on mismatch. If everything is in order, it continues as `reconciled`. Duplicate orders are prevented by write-ahead records and the `UNCERTAIN → query` procedure (`tests/simulation/test_order_manager.py`).

## 4. Exchange outages (5xx, maintenance, 429/418)

* GETs are retried with exponential backoff; orders are not retried, and their outcome is confirmed by querying.
* 418 (ban): stop immediately and alert. During the ban, cycles are recorded as `error`, and 3 in a row means SAFE_HOLD.
* After maintenance ends, `reconcile` → release.

## 5. Alert channel failures

A failed alert is only logged and does not stop the trading loop (`Alerter._send_one`). The failure count accumulates in `Alerter.failures`.

## 6. Data anomalies

* A missing funding record (e.g. 2026-06-30 08:00) is treated as "no settlement" in the backtest and recorded in the quality report.
* Days with a mark/index divergence > 2% are noted in the quality notes and are not excluded from the backtest.
