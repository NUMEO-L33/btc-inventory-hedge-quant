# Strategy V2 study — funding guard

Implementation: `src/btc_inventory_quant/data/funding_features.py`, `src/btc_inventory_quant/strategy/funding_guard.py`
Research script: `scripts/v2_funding_guard_study.py` → results in `reports/v2_funding_guard/`
Parameters: `configs/*.yaml` → `strategy.funding_guard` (default `enabled: false`)

> **Conclusion first**: the funding guard succeeded in turning the FAIL in the "adverse funding −1bp/8h" scenario into a PASS. However, **the improvement it obtained on actual historical data is not a funding-cost saving but a directional bet (luck).** And the more important fact this study confirmed along the way is that **there is no evidence that dynamic regime classification (V1) is better than a static fixed hedge**. See the §6 verdict below.

---

## 1. Why this was built (a question raised by the V1 audit)

An honest dissection of the V1 backtest produced the following.

* Practically all of the net BTC gain was **funding received by the short (carry)**. No timing alpha was observed.
* Of the 12 sensitivity-analysis scenarios, **the only one that produced a FAIL was "adverse funding"** (`final_btc` 0.929 at −1bp/8h, 0.554 at −3bp/8h).
* In other words, the single biggest vulnerability of this strategy is not direction but **the sign of the carry flipping**.

So V2 step 1 was defined not as improving the directional forecast but as adding a **cost rule**.

```
mean settled funding rate over the last lookback_periods settlements < threshold  →  target hedge ratio × scale,  and capped at max_ratio_when_adverse
```

This is not a market-direction forecast. It is an accounting rule: "when the carry on holding the short turns negative, reduce the short".

### Does it violate the original principles?

It does not. Checks:

| Principle | Status in V2 |
|---|---|
| No long futures positions | Kept (the guard only ever **reduces** `raw_target`, `clip(lower=0)`) |
| 85% maximum hedge cap | Kept (single definition in `RiskLimits`; the guard operates only below it) |
| BUY is reduce-only, never exceeding the short quantity | Kept (no change to the risk/execution layers) |
| 20%p daily change cap / 10%p minimum rebalance | Kept (the guard modifies only `raw_target`; everything downstream in the pipeline is unchanged) |
| No look-ahead | Kept (see §2; causality unit tests exist) |
| Decimal arithmetic for amounts | Kept (the guard sits in the indicator layer, hence float; orders/equity/fees are all Decimal) |

The guard is **a decorator that wraps any base strategy** (`FundingGuardStrategy(base, params)`). Because it does not change the base strategy, the guard's contribution can be measured independently — that is the purpose of the 2×2 design.

## 2. Causality (look-ahead prevention)

`build_funding_features()` computes the features for bar t **using only funding actually settled by the close of bar t**.

```python
counts = series.index.searchsorted(closes, side="right")   # closes = bar close times
start  = np.clip(counts - n, 0, None)
total  = cumsum[counts] - cumsum[start]
```

* The engine fills a bar-t signal at the bar t+1 **open**, so there is no room for future information to get in.
* `tests/unit/test_funding_features.py::test_features_are_causal` — verifies that appending future data does not change the features of past bars.
* `test_settlement_after_bar_close_is_not_visible` — verifies that the next day's 00:00 settlement does not leak into the previous day's bar.
* `tests/simulation/test_trader_simulation.py::test_trader_runs_the_funding_guard_with_the_same_features_as_the_backtest` — verifies that **the live path and the backtest path produce the same features and the same target ratio** (rtol 1e-12 / atol 1e-6). It is a safeguard against deploying a strategy that only works in the backtest.

## 3. Experimental design (pre-registered)

* 2×2: {`fixed_50` static 50% hedge, `regime_v1` dynamic regime} × {no guard, with guard} + `hodl` benchmark.
* The guard parameters were **fixed to a single setting before looking at any results**: `lookback=21` settlements (≈7 days), `threshold=0`, `scale=0.5`, `cap=0.45`. The values were set on economic grounds alone and were not tuned to these results.
* Evaluation segments: full period / pre-registered chronological 60·20·20 split / nested walk-forward, 7 folds / funding-regime split.
* The neighbourhood sensitivity (lookback 9·21·45 × scale 0.25·0.5·0.75) is printed **not to pick a winner**, only to see whether the result is balanced on a knife-edge.

## 4. Results

### 4.1 Full period and pre-registered splits (`reports/v2_funding_guard/splits.csv`)

| Strategy | Segment | final_btc | CAGR(USD) | MDD(USD) | Funding (BTC) | Trades | Verdict |
|---|---|---:|---:|---:|---:|---:|---|
| hodl | full | 1.000 | +6.44% | −76.7% | 0 | 0 | Baseline |
| fixed_50 | full | 1.2460 | +10.80% | −48.7% | 0.2462 | 11 | PASS |
| **fixed_50+guard** | full | **1.3650** | **+12.66%** | −52.7% | 0.2465 | 89 | PASS |
| regime_v1 | full | 1.1085 | +8.46% | −47.1% | 0.1246 | 243 | PASS |
| regime_v1+guard | full | 1.3056 | +11.75% | −46.2% | 0.1471 | 319 | PASS |
| fixed_50 | validation | 0.7804 | +40.5% | −14.4% | 0.0312 | 4 | **FAIL** |
| fixed_50+guard | validation | 0.8156 | +46.3% | −15.6% | 0.0301 | 18 | **FAIL** |
| fixed_50 | out_of_sample | 1.2990 | −14.6% | −28.5% | 0.0302 | 4 | PASS |
| **fixed_50+guard** | out_of_sample | **1.2686** | −16.4% | −31.0% | 0.0282 | 18 | PASS |
| regime_v1 | out_of_sample | 1.2372 | −18.3% | −26.7% | 0.0319 | 38 | PASS |
| regime_v1+guard | out_of_sample | 1.2186 | −19.4% | −31.4% | 0.0332 | 64 | PASS |

**Over the full period the guard improves the result. But in the most recent OOS segment, the guard made both base strategies worse** (1.299→1.269, 1.237→1.219). The FAIL in the validation segment also remains — the structural fact that a short loses BTC in a bull market is not fixed by the guard.

### 4.2 Adverse-funding scenarios — the original goal is achieved (`sensitivity_2x2.csv`)

| Scenario | fixed_50 | fixed_50+guard | regime_v1 | regime_v1+guard |
|---|---:|---:|---:|---:|
| base | 1.2460 PASS | 1.3650 PASS | 1.1085 PASS | 1.3056 PASS |
| **Adverse funding −1bp/8h** | **0.9286 FAIL** | **1.0547 PASS** | **0.8461 FAIL** | **1.0149 PASS** |
| Adverse funding −3bp/8h | 0.5537 FAIL | 0.6209 FAIL | 0.5001 FAIL | 0.6167 FAIL |

The guard **does actually fix the stated failure mode** — but only up to mild adverse funding (−1bp). In a sustained −3bp/8h environment (−33% a year), both variants still FAIL. **This strategy does not work in a long-lasting adverse-funding regime. The guard only delays that limit; it cannot remove it.**

### 4.3 But the actual improvement is not a funding saving (the key counter-evidence)

If the guard had improved results "by saving on funding costs", `total_funding_btc` should have risen meaningfully. It did not.

| | Δfinal_btc | Δfunding | Δfees | Residual (= directional) |
|---|---:|---:|---:|---:|
| fixed_50 → +guard | **+0.1191** | +0.00029 (**0.2%**) | −0.01108 | **+0.1298 (109%)** |
| regime_v1 → +guard | **+0.1972** | +0.02246 (11.4%) | −0.01435 | **+0.1891 (96%)** |

For the static hedge, **only 0.2% of the improvement came from funding; all of the rest came from the price path.** And that directionality is luck — which is clear from what the guard's trigger days actually were (`funding_regime_split.csv`).

| Segment | Days | HODL USD return | fixed_50 BTC | fixed_50+guard BTC |
|---|---:|---:|---:|---:|
| Adverse funding (guard active) | 268 | **+80.4%** | −0.2205 | −0.1759 |
| Normal funding | 1,734 | −23.3% | +0.6037 | +0.6620 |

In other words, **in the historical data, adverse-funding phases were mostly sharp rallies.** The guard cut the short during rallies, and that is why performance went up. This is not a "funding hedge" but **a counter-trend directional bet that uses the adverse funding rate as a proxy variable**. There is no guarantee that this correlation (negative funding → subsequent rise) will hold in the future. Indeed, in the recent OOS segment it has already worked the other way (§4.1).

### 4.4 Robustness counter-evidence — the more the signal is broken, the better it does

| Scenario | fixed_50 | fixed_50+guard | regime_v1+guard |
|---|---:|---:|---:|
| base | 1.2460 | 1.3650 | 1.3056 |
| Signal delayed by 1 day | 1.2537 | **1.3930** | **1.4304** |
| 1 in 10 rebalances skipped | 1.2445 | **1.4397** | 1.2761 |
| 50% partial fills | 1.2894 | **1.4819** | **1.4011** |

**A signal whose performance actually improves when it is delayed by a day, when rebalances are skipped and when orders are only half filled is not a timing edge.** The static `fixed_50` barely reacts to these perturbations (as expected by design), while only the guard variants swing widely. This is direct evidence that the guard's gain is noise that depends on a small number of specific dates.

### 4.5 Walk-forward (7 folds, `walk_forward.csv`)

| Strategy | Folds with BTC growth | **Wilson 95% CI** | Mean net_btc_growth | Mean MDD(USD) |
|---|---:|---:|---:|---:|
| hodl | 0 / 7 | [0.000, 0.354] | 0.0000 | −25.5% |
| fixed_50 | 2 / 7 | **[0.082, 0.641]** | −0.0459 | −13.5% |
| fixed_50+guard | 2 / 7 | **[0.082, 0.641]** | −0.0267 | −13.9% |
| regime_v1 | 1 / 7 | **[0.026, 0.513]** | −0.0472 | −16.5% |
| regime_v1+guard | 1 / 7 | **[0.026, 0.513]** | −0.0442 | −17.2% |

The guard did not change the fold win rate at all (2/7 → 2/7, 1/7 → 1/7). But what matters more is that **this column itself says nothing**: the confidence intervals of all 4 hedge strategies contain 0.5, so none of them can be distinguished from a coin flip, and comparisons between them do not hold either. n=7 is too small to support such a verdict.

### 4.6 Neighbourhood sensitivity (`guard_sensitivity.csv`, fixed_50 base)

| lookback \ scale | 0.25 | 0.50 | 0.75 |
|---|---:|---:|---:|
| 9 | 1.4183 | 1.3130 | 1.3344 |
| 21 | **1.6411** | 1.3650 | 1.3589 |
| 45 | 1.5169 | 1.3038 | 1.3876 |

That all of them PASS and all are above the base (1.2460) is reassuring, but **the results are not monotonic along the scale axis** (0.25 best, 0.5 worst, 0.75 in between). A genuine cost rule should improve monotonically the more it cuts. The broken monotonicity is consistent with the §4.3~4.4 "directional noise" interpretation. And the top value of 1.6411 (lookback 21 / scale 0.25) is **a peak, not a plateau**, so it is not adopted.

### 4.7 Re-validation on the new snapshot (8h mark prices)

The tables in §4.1~4.6 were produced from the snapshot taken **before the 8h mark-price dataset was obtained** (`data/raw`). After the mark price at funding-settlement times was switched from an approximation to measured values (`docs/backtest_methodology.md` §6-2), the same experiment was re-run (`reports/v2_funding_guard_new/`).

| Strategy | Full period (old → new) | OOS (old → new) | Walk-forward winning folds |
|---|---|---|---|
| fixed_50 | 1.2460 → **1.2446** | 1.2990 → **1.2990** | 2/7 → **2/7** |
| fixed_50+guard | 1.3650 → **1.3630** | 1.2686 → **1.2686** | 2/7 → **2/7** |
| regime_v1 | 1.1085 → **1.0977** | 1.2372 → **1.2372** | 1/7 → **1/7** |
| regime_v1+guard | 1.3056 → **1.2976** | 1.2186 → **1.2186** | 1/7 → **1/7** |

* **The OOS numbers are identical down to the last decimal place.** This is because the OOS segment (2023-11 onward) was already using the true mark prices supplied by the exchange, and it also verifies that the plumbing works as intended.
* Over the full period every strategy dropped slightly (the approximation had been overstating funding).
* **The verdicts, the walk-forward win rates and the static vs dynamic ranking are all unchanged.** If anything, the static–dynamic gap **widened**, 0.1375 → 0.1469.

So the conclusion of §6 holds on the new data as well, and nothing was retrofitted in either direction.

## 5. Pre-registered verdict rule

The rule committed to before this document was written is kept exactly as stated.

> A new rule is judged only on **new OOS data from 2026-09 onward** and on walk-forward, and is never retrofitted on the basis of existing OOS performance.

Therefore the full-period performance above (1.246 → 1.365) is **not used as grounds for adoption.** What the verdict used is (a) the walk-forward fold win rate, (b) the recent OOS segment, (c) P&L attribution, (d) robustness to perturbations — and none of the four favours the guard.

## 6. Verdict

| Question | Answer | Basis |
|---|---|---|
| Does the guard fix the stated failure mode (adverse funding)? | **Partly, yes** | −1bp FAIL→PASS, −3bp still FAIL (§4.2) |
| Is the guard's actual historical gain a funding saving? | **No** | Only 0.2% of the gain (static basis) is funding (§4.3) |
| Does the guard introduce an unverified directional bet? | **Yes** | HODL +80.4% on the days it fired, fragile under perturbation (§4.3~4.4) |
| Is the guard switched on by default? | **No** | `enabled: false` kept |
| Is the guard kept in the code? | **Yes** | As an **operational switch** for an adverse-funding regime, only under the §7 conditions below |
| **Is the dynamic regime (V1) better than a static hedge?** | **No** | §6.1 |

### 6.1 The most important result of this study — the dynamic regime adds no value

The question raised before V2 began now has an answer. The static `fixed_50` beats `regime_v1` **on every axis**.

| Comparison axis | fixed_50 | regime_v1 | Winner |
|---|---:|---:|---|
| Full-period final_btc | 1.2460 | 1.1085 | **Static** |
| Full-period CAGR(USD) | +10.80% | +8.46% | **Static** |
| OOS final_btc | 1.2990 | 1.2372 | **Static** |
| ~~Walk-forward winning folds~~ | ~~2 / 7~~ | ~~1 / 7~~ | **Withdrawn — see below** |
| Total trades (operational risk, fees) | 11 | 243 | **Static** |
| Full period with the guard applied | 1.3650 | 1.3056 | **Static** |

> **⚠️ 1 line of evidence withdrawn (2026-09-02)**: the table above used "walk-forward winning folds 2/7 vs 1/7" as evidence of static superiority. **That was a mistake.** With Wilson 95% confidence intervals attached, 2/7 = [0.082, **0.641**] and 1/7 = [0.026, **0.513**]: the two intervals overlap almost completely, and both contain 0.5 — that is, **neither can be distinguished from a coin flip, and the difference between them is not evidence either.** Citing a difference of 1 across 7 samples as evidence was statistically meaningless.
>
> The remaining 4 axes (full-period BTC and CAGR, OOS, trade count, with the guard applied) are valid, so **the conclusion of §6.1 itself stands**, but it is supported by 4 rather than 5 lines of evidence. This error was found while reviewing the methodology of an external validator project (Wilson intervals + control arms + family-level verdicts). From now on, every win rate in this project is reported together with its interval, using `backtest/statistics.py::wilson_interval`.

**The EMA/ATR regime classifier paid for 243 trades and 22× the operational complexity and still underperformed a static 50% hedge.** This matches exactly the observation from the earlier audit that "HODL returned +52.7% over the 761 days labelled bear" — that is, the fact that the classifier is measurably mislabelling.

**If the goal is to hold BTC long-term while reducing USD drawdown, what the current evidence supports is a static hedge ratio.** Dynamic adjustment is not justified by the current signals.

## 7. Operational recommendations

1. **Keep the default setting**: `strategy.funding_guard.enabled: false`. Do not switch it on based on backtest performance.
2. **Treat the static hedge as the baseline.** Do not promote `regime_v1` to a live candidate. If going live is discussed, `fixed_hedge` is the starting point.
3. **The only condition under which the guard may be switched on**: when realised funding is observed to have **actually** turned persistently negative (e.g., the 30-day realised funding sum stays < 0 for 2 weeks or more). This is a cost response to an observation, not a forecast, and the grounds for switching it on at that point are §4.2 (recovery in the −1bp scenario), not §4.1 (improved historical returns).
4. **Under sustained adverse funding at the −3bp/8h level, stop the hedge strategy itself.** It has been measured that the guard does not protect against this (§4.2).
5. Any future change follows the §5 rule of this document: judged only on new data from 2026-09 onward.

## 8. Remaining unverified items (honest disclosure)

* Smoke test with a real connection to the live/demo exchange — not performed.
* Measured-live comparison of the funding formula (actual settlement amounts vs the model) — not performed. Backtest funding is a model value based on the official documentation.
* Docker image build — not performed.
* Current VIP0 fee rate — documented value used; not checked against the account's measured live rate.
* All of the results above rest on a single snapshot, 2020-08-11 ~ 2026-09-01 (2,213 daily bars). It is a sample of a single market cycle, and there is no guarantee that this period's funding–return correlation will hold in the future.

**No profit is guaranteed under any circumstances.** The purpose of this document was to judge whether the V2 idea worked, and the verdict is: "the stated failure mode was partly fixed, but the claim of improved returns has no basis, and the dynamic regime is rejected".
