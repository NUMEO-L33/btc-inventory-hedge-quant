# Strategy specification — Regime Hedge V1 (baseline strategy for initial validation)

Implementation: `src/btc_inventory_quant/strategy/regime_hedge.py`, `state_machine.py`, `benchmarks.py`, `risk/limits.py`, `execution/rebalancer.py`
Parameters: `configs/backtest.yaml` → `strategy.regime_hedge` (not hard-coded)

## 1. Purpose and economic structure

While holding BTC long-term, the strategy gradually adjusts the ratio of a BTC-collateralised COIN-M inverse perpetual (BTCUSD_PERP) short within the 0~85% range.

* A 100% hedge = a synthetic dollar (USD value locked in). This is not a profit strategy.
* Profit comes only from the cycle "high price / decline confirmed → hedge ↑ → the short earns BTC during the decline → rebound confirmed → hedge ↓ → take part in the rally with the enlarged BTC holding".
* Performance is judged on **BTC quantity** and **USD value and drawdown** together. If only BTC grows while USD collapses, that is a failure.

## 2. Signals (completed UTC daily bars only)

| Item | Definition |
|---|---|
| EMA20/50/200 | `ewm(span=n, adjust=False)` |
| ATR20 | Wilder RMA(True Range, 20) |
| Bull regime (bull) | close > EMA200 **and** EMA50[t] > EMA50[t−5] |
| Bear regime (bear) | close < EMA200 **and** EMA50[t] < EMA50[t−5] |
| Neutral regime (neutral) | otherwise |
| State confirmation | switch only when the new state holds for **2 consecutive completed daily bars** (`RegimeStateMachine`) |
| Base hedge ratio | bull 10% / neutral 45% / bear 80% |
| Overbought adjustment | close > EMA20 + 2·ATR20 → +10%p |
| Oversold-rebound adjustment | close < EMA20 − 2·ATR20 **and** close > previous day's close → −10%p |
| Raw target | base + adjustments, clipped to [0, 1] |

## 3. Risk pipeline (outside the strategy, single definition in `RiskLimits`)

```
raw target → clamp[min 0%, max 85%] → daily change cap ±20%p (relative to the current effective hedge ratio)
           → if |target − current| < 10%p, no trade
           → target contracts = floor(equity_btc × mark × target / 100) → δ = target − current contracts
           → δ > 0: SELL (increase the short) / δ < 0: BUY reduce-only (never exceed the short quantity, never flip to long)
```

* Effective hedge ratio = short notional (USD) / (equity_btc × mark). It falls automatically when the price rises and rises when the price falls.
* Effective leverage ≤ 1, short notional ≤ total equity. No martingale, no averaging down.

## 4. Benchmarks

HODL (hedge 0) / fixed 100% short (explicitly bypasses the cap) / fixed 50% short / simple EMA200 (close < EMA200 → 80%, otherwise 0%) / dynamic V1.
The fixed strategies are also rebalanced under the same 20%p cap and 10%p minimum-difference rules (drift correction).

## 5. Summary of validation results (2021-03-10 → 2026-09-01, real data, Taker 0.05%·5bp·actual funding)

| Strategy | Final BTC | USD CAGR | USD MDD | Trades | Funding (BTC) | Verdict |
|---|---|---|---|---|---|---|
| HODL | 1.000 | +6.4% | −76.7% | 0 | 0 | Baseline |
| Fixed 50% | 1.246 | +10.8% | −48.7% | 11 | +0.246 | PASS |
| Fixed 100% | 1.172 | +9.6% | −4.3% | 9 | +0.527 | PASS |
| Simple EMA200 | 1.071 | +7.8% | −50.6% | 165 | +0.051 | PASS |
| **Dynamic V1** | **1.109** | **+8.5%** | **−47.1%** | **243** | **+0.125** | PASS (full period) |

BTC P&L decomposition (V1): hedge timing +0.0076 / funding +0.1246 / fees −0.0238 → **the source of the net gain is funding carry**.

### Overfitting checks (`reports/research_v1/research_report.md`)

* Chronological 60/20/20: research segment PASS, **validation segment (2024-06~2025-07 bull market): every hedge strategy FAILs**, OOS (2025-07~2026-09 bear market): all PASS → the outcome is determined by the market direction of the segment.
* Nested walk-forward, 7 folds: V1 grew BTC in 1/7, outperformed HODL (USD) in 1/7. Adaptive parameters chosen by a grid on the training segment were **worse** than fixed V1 → no parameter tuning.
* Start-date dependence (2-year holds, 14 start dates): BTC grew in 6/14, median −8%. The V1 curve almost coincides with fixed 50% → the state-classification logic carries negligible information value.
* Sensitivity: fees ×2 and slippage ×3 cause a mild deterioration, **funding at −1bp/8h means FAIL**, and a 1-day signal delay, missed orders and partial fills actually improve the result (= the signal is oversensitive).

### Conclusion (honest verdict)

**V1 has no timing alpha.** Its only consistent effects are two — (1) a smaller USD drawdown and (2) positive (+) funding carry — and both are also obtained with a static 50% hedge. Therefore

1. The purpose of live trading must be redefined not as "profit" but as "USD drawdown management + funding collection".
2. In phases where funding turns negative (−) (bear markets crowded with shorts), the strategy loses money → a funding-conditional hedge-reduction rule is the V2 candidate.
3. Reducing state switches (98 switches / 2,002 days) — a longer confirmation period, hysteresis — cuts trading costs, but walk-forward showed that this does not create alpha.
4. Any change must be evaluated only on a new OOS segment (data after 2026-09).

## 6. Parameter table (initial values, not permanent)

| Key | Value | Notes |
|---|---|---|
| ema_fast / ema_mid / ema_slow | 20 / 50 / 200 | |
| atr_period | 20 | Wilder |
| slope_lookback | 5 | EMA50 slope |
| hedge_bull / neutral / bear | 0.10 / 0.45 / 0.80 | |
| overbought_atr_mult / adjustment | 2 / +0.10 | |
| oversold_atr_mult / adjustment | 2 / −0.10 | |
| regime_confirm_bars | 2 | |
| risk.max_hedge_ratio | 0.85 | single definition |
| risk.max_daily_hedge_change | 0.20 | |
| risk.min_rebalance_diff | 0.10 | |
