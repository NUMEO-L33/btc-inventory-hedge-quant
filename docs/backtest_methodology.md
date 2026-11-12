# Backtest methodology

Implementation: `src/btc_inventory_quant/backtest/` (engine, execution_model, funding_model, metrics, scenarios, walkforward, reports).

## 1. Data

| Data | Source | Use |
|---|---|---|
| Trade-price daily candles (`klines_1d`) | `GET /dapi/v1/klines?symbol=BTCUSD_PERP&interval=1d` | Indicator calculation (close), fill price (next open) |
| Mark price daily candles (`mark_price_klines_1d`) | `GET /dapi/v1/markPriceKlines` | Unrealized PnL, equity value, hedge ratio, target contract count |
| Index daily candles (`index_price_klines_1d`) | `GET /dapi/v1/indexPriceKlines?pair=BTCUSD` | Data validation (flag on mark/index divergence > 2%) |
| Funding history (`funding_rate`) | `GET /dapi/v1/fundingRate` | Actual funding settlements (00/08/16 UTC) |
| Contract specification (`exchange_info.json`) | `GET /dapi/v1/exchangeInfo` | Contract value, quantity unit, tick |

* Every raw JSON page is recorded in `data/raw/manifest.json` together with its SHA-256 checksum and is re-verified with `python -m btc_inventory_quant data verify`.
* Incomplete candles (`close_time ≥ as_of`) are excluded. 2020-08-11 ~ 2026-09-01: 2,213 completed daily candles, 6,640 funding records.
* Data anomaly: 1 funding record (2026-06-30 08:00 UTC) is missing from the exchange's source data (left as is; treated as no settlement at that time).

## 2. Fill timeline (look-ahead prevention)

```
[close of candle j-1 = day j 00:00 UTC] compute signal (completed candles only)
[day j 00:00] 00:00 funding settlement (previous day's position)
[day j open]  compute current effective hedge ratio and target contract count (mark_open_j) → risk pipeline → order → fill at open_j × (1 ± slippage), Taker fee
[day j 08:00, 16:00] funding settlement (post-fill position)
[day j close] valuation at mark_close_j → daily record
```

* No fill happens on the candle that produced the signal (`test_signal_executes_on_next_open_never_same_candle`).
* Truncating future data does not change past results (`test_truncating_future_data_does_not_change_the_past`).
* Indicators and the strategy are causal (`test_indicators_are_causal`, `test_strategy_is_causal`).

## 3. Cost model

| Item | Default | Rationale |
|---|---|---|
| Taker fee | 0.05% | Conservative VIP0 (2021 announcement: 0.050%; the current rate is checked with `commissionRate`) |
| Maker fee | 0.02% | Conservative (announced 0.010%) — used only in the `use_maker: true` scenario |
| Slippage | 5 bp | Conservative assumption based on BTCUSD_PERP liquidity; ×3 in a scenario |
| Funding | Actual history | `funding_btc = contracts×100/mark×rate`; the short receives when it is positive (+) |
| Partial fills | Probability p, fraction r | The remainder is re-ordered the next day (`reorder_missed_next_day`) |
| Missed orders | By index / every Nth | That rebalance goes unfilled; decided again the next day |
| Rounding | Whole contracts, rounded down | Fills only at or below the target notional |

Fee = contracts × 100 / fill price × rate (BTC). The slippage cost is reflected in the fill price and is recorded separately in the ledger as an informational entry (`SLIPPAGE`).

## 4. Performance metrics

BTC-based metrics (final BTC, net increase, CAGR, MDD) and USD-based metrics (final USD, CAGR, MDD, excess performance vs. HODL) are always computed together. Starting USD = starting BTC × mark open on the first fill day. HODL is the case of simply holding 1 BTC.

Verdict rules (`metrics.verdict`): **FAIL** if the BTC quantity decreases, or USD is −30% or worse vs. HODL, or USD MDD is worse than HODL's by 5%p or more; **WEAK** if USD is −10% or worse vs. HODL; otherwise **PASS**. A result in which only BTC grows while USD falls sharply is not regarded as a success.

Note (identity): because the HODL benchmark is the same BTC, `USD excess performance vs. HODL = final BTC / starting BTC − 1`, which is **mathematically identical to the net BTC growth rate**. The second criterion is therefore read not as 'vs. HODL' but as **absolute USD return and maximum drawdown** (whether value collapses).

BTC PnL decomposition: net BTC increase = hedge-timing PnL (realized + unrealized) + funding + fees. If funding is all of it, there is no timing alpha.

## 5. Sensitivity scenarios (`scenarios.py`)

Base / fees ×2 / slippage ×3 / adverse funding (−1bp, −3bp per 8h) / 1-day signal delay / 1 missed rebalance (#1, #5) / a miss every 10th rebalance / partial fills (50%, p=0.5) / combined (fees×2 + slippage×3 + delay) / Maker fills.

## 6. Overfitting prevention (`walkforward.py`)

1. **Chronological split**: of the tradable days after warm-up, 60% research / 20% validation / 20% OOS. No random shuffling.
2. **Restricted grid (72 combinations)**: bull {0.10, 0.20} × neutral {0.30, 0.45, 0.60} × bear {0.70, 0.85} × confirmation days {2, 5, 10} × slope {5, 10}.
3. **Objective function**: `0.5·CAGR_BTC + 0.5·(CAGR_USD − HODL_CAGR_USD) − 0.25·max(0, MDD_HODL − MDD)`.
4. **Plateau preference**: final score = 0.5·own score + 0.5·mean of the neighboring combinations (one parameter, one step). A stable region is chosen over an isolated peak.
5. **Nested Walk-forward**: train 730 days → test 180 days, rolling forward 180 days. In each fold the grid point is selected using the training window only → evaluated on the test window. Compared fold by fold with fixed V1 / HODL / fixed 50%.
6. **Start-date dependence**: start dates 91 days apart, distribution of 2-year holding results.
7. **Records**: all grid results, folds, scores, config hashes, seeds and git commits are saved under `reports/research-*/`.

**If parameters are changed after looking at the OOS results, that period is no longer OOS.** In this project, OOS-period results, once reported, are not used as grounds for changing parameters.

## 6-2. Mark price at funding settlement time (priority order)

Implementation: `data/settlement_marks.py`, `backtest/funding_model.py`

| Priority | `mark_source` | Source | Period applied |
|---|---|---|---|
| 1 | `record` | The `markPrice` field of the `fundingRate` response | From 2023-11-01 onward |
| 2 | `kline_8h` | The **open** of `markPriceKlines?interval=8h` (bar start time = settlement time) | The entire period, if the 8h dataset has been downloaded |
| 3 | `daily_open` / `daily_interp` | Approximation from daily mark OHLC (00h = open, 08/16h = linear interpolation open→close) | When neither of the above is available |

The 8h dataset is **optional**. It is downloaded with `python -m btc_inventory_quant data --raw-dir <dir> --include-8h-mark download`; if the directory does not exist, the code automatically falls back to priority 3 and the results are exactly identical to before (`test_no_dataset_reproduces_the_previous_behaviour_exactly`).

**Why it was needed**: the 3rd-priority approximation covered **53.2%** of all settlements (3,533 of 6,640 settlements, 2020-08~2023-11).

**Actual impact — measured by period**: the 8h dataset was taken as ground truth and compared directly with the 3rd-priority approximation.

| Period | Count | Mean \|relative error\| | Median / p95 | Max | **Cumulative funding error** |
|---|---:|---:|---:|---:|---:|
| **2020-08 → 2023-10** (period in which the approximation was actually used) | 3,530 | 0.716% | 0.280% / 2.864% | **15.288%** | **+0.2352%** |
| 2023-11 → 2026-09 (`record` available; for reference) | 3,109 | 0.523% | 0.263% / 1.891% | 7.769% | +0.0094% |

> **⚠️ Correction of the initial estimate**: when this work started, the cumulative error was estimated at **+0.0096%** using only the period from 2023-11 onward, where ground truth was available. **That was wrong.** The 2020-2023 period, in which the approximation was actually used, was far more volatile, and its cumulative error was **+0.2352% — 25× the estimate**. The mistake was extrapolating from a calm period to a volatile one. It is also recorded here that, before the 8h data was downloaded, there was no ground truth for this period and hence no way to verify it.

The approximation was biased toward **overstating** funding received (`approx > true`). Switching to the true values therefore **lowers** performance slightly — i.e., the earlier reports were marginally optimistic.

**Measured backtest deltas** (`scripts/compare_snapshots.py --old data/raw --new data/raw_new`):

| Strategy | final_btc | Total funding | Maximum drawdown | Verdict |
|---|---:|---:|---:|---|
| `fixed_50` | 1.245966 → 1.244574 (**−0.11%**) | −0.38% | −0.09%p worse | PASS → PASS |
| `regime_v1` | 1.108451 → 1.097685 (**−0.97%**) | −1.11% | −0.45%p worse | PASS → PASS |

`regime_v1` is affected more because its position changes often and by large amounts, so larger positions are exposed on the days with large errors. **The numbers for the OOS period (2023-11 onward) are exactly identical** — that period was already using the `record` true values. Not a single verdict changed (the §4 walk-forward fold win rates are also unchanged at 2/7 and 1/7), and **on the conclusion that the static hedge beats the dynamic regime, the gap actually widened** (0.1375 → 0.1469).

## 7. Known limitations

* Daily resolution: the mark price at the moment of an intraday **liquidation** is still approximated. Funding settlement times can be resolved with the 8h dataset (§6-2).
* Contract specification history: the current specification (100 USD/contract, whole contracts) is applied to the entire period.
* Slippage and partial fills are model values. They need to be recalibrated with live-trading fill data.
* The history of exchange fee-rate changes is not reflected (conservative fixed values).
* Single symbol, single exchange.
