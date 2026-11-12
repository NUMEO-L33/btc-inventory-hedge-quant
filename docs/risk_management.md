# Risk management

All numeric limits are defined in one place only: the `risk` section of `configs/*.yaml` (`config/schema.py::RiskLimits`).

## 1. Position limits

| Limit | Value | Enforced in |
|---|---|---|
| Maximum effective hedge ratio | 85% (+3%p drift tolerated, then SAFE_HOLD) | `risk/limits.py::clamp_hedge_ratio`, `validate_order_against_position`, `risk/guards.py::check_hedge_cap` |
| Minimum hedge ratio | 0% | Same as above |
| Maximum daily hedge ratio change | 20%p | `apply_daily_change_cap` |
| Minimum rebalancing difference | 10%p | `rebalance_needed` |
| Futures long | Prohibited | `InversePosition(allow_long=False)`, `RiskLimits.allow_long: Literal[False]`, guard `LONG_POSITION`, reconciliation `LONG_POSITION` |
| BUY quantity | ≤ current short | `validate_reduce_only_buy`, `BUY_EXCEEDS_SHORT`, rebalancer cap |
| Effective leverage | ≤ 1 | `NOTIONAL_EXCEEDS_EQUITY` |
| Short notional | ≤ total equity in USD | Same as above |
| While an unconfirmed order exists | No new orders | `OrderManager.submit` → `DuplicateRiskError` |
| Martingale / averaging down | Prohibited | The target is derived only from the equity ratio (no loss-based sizing-up logic) |

## 2. Recommended account structure

* A **sub-account** dedicated to automated trading, with no other futures positions, no other bots and no manual trading.
* No collateral other than BTC. API **withdrawal permission disabled**, **IP whitelist**.
* **One-way Position Mode** (note that since the unification it is a setting shared by UM/CM), Cross Margin by default (checked at runtime).
* Leave the leverage value in the UI at 1 but exercise control through the effective hedge ratio.

## 3. Data and time guards

| Condition | Threshold | Result |
|---|---|---|
| Mark price stale | > 120 s | SAFE_HOLD |
| Completed candle stale | > 26 h | SAFE_HOLD |
| Server time offset | > 1000 ms | SAFE_HOLD |
| Contract spec change (contractSize/step/tick) | Mismatch | SAFE_HOLD |

## 4. State consistency guards

* Internal DB vs exchange: position contract count, average price (0.1%), wallet balance (1e-5 BTC), unknown orders, position mode, cap → SAFE_HOLD if even one of them disagrees.
* Post-order re-check: if the exchange position after a fill differs from what was expected, SAFE_HOLD.
* An unexpected deposit or withdrawal (income TRANSFER) is recorded in the ledger as an external flow, and SAFE_HOLD is used to require operator confirmation.

## 5. Liquidation risk

A single short position theoretically has no liquidation price when hedge ratio ≤ 85% and leverage ≤ 1 (`docs/inverse_math.md` §7). Even so, if the maintenance-margin-to-equity ratio exceeds 25%, the system goes into SAFE_HOLD, and the exchange's `positionRisk.liquidationPrice` is recorded as a monitoring metric.

## 6. Fail-safe principles

On failure, **do not close out the entire short** (doing so could suddenly increase downside exposure).

1. Stop new orders → 2. Cancel open orders → 3. Keep the position → 4. Re-query the exchange state → 5. Compare with the DB → 6. SAFE_HOLD until agreement is confirmed → 7. Alert.
SAFE_HOLD is persisted in the DB and survives restarts; it is released only with `safe-hold --release --ack <string>`.

## 7. Live gates (all must pass for real orders)

`BIQ_ENABLE_LIVE_TRADING=true` · `live.live_trading_enabled: true` · `--confirm-live` · 3-way acknowledgement match (`I_ACCEPT_COIN_M_TRADING_RISK`) · API key present, with permissions · time synchronized · positions agree · no unconfirmed orders · hedge ratio within limits · market data healthy · contract spec verified · One-way · equity ≤ `max_initial_equity_btc` (small-size guard).
If even one fails, it runs **read-only (recording intent only)**.
