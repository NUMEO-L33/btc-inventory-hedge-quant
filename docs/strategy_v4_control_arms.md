# Strategy V4 — Control-arm tests: is any of this better than random?

Research script: `scripts/control_arm_study.py` → results in `reports/v4_control_arms/`
Implementation: `src/btc_inventory_quant/strategy/controls.py`, `src/btc_inventory_quant/backtest/statistics.py`
Data: `data/raw_new` (including 8h mark prices), 200 seeds

> **Conclusion first**: after multiple-comparison correction, **nothing is significant.** The 2 results that were significant before correction were both **transaction-cost-avoidance artefacts**, and against the fair control arm, with the confound removed, the actual timing of `regime_hedge_v1` **did worse than the median of random rearrangements of its own signal** (41st percentile). The "no timing alpha" suspected all along since V1 has now been **measured against a control arm**.

---

## 1. Why this study was needed

Every result in V1–V3 was a **comparison against HODL and against each other**. **What score a worthless strategy gets on this data** had never been measured. Without a baseline there is no way to tell whether a "walk-forward score of −0.17" reflects skill, exposure level or luck.

This gap came to light while reviewing the methodology of an external validator project (`control arm + Wilson interval + family-level verdict`). The same review also uncovered **an error in 1 claim already published** (§2).

## 2. A correction first — win-rate claim withdrawn

For every win rate this project has reported, **the Wilson 95% confidence interval contains 0.5.**

| Value reported | Point estimate | Wilson 95% CI | Meaning |
|---|---:|---:|---|
| `fixed_50` walk-forward 2/7 | 0.286 | [0.082, **0.641**] | Indistinguishable from a coin flip |
| `regime_v1` walk-forward 1/7 | 0.143 | [0.026, **0.513**] | Indistinguishable from a coin flip |
| V3 at every ratio 2/7 | 0.286 | [0.082, 0.641] | Indistinguishable from a coin flip |
| Start-date windows at 15% 8/14 | 0.571 | [0.326, 0.786] | Indistinguishable from a coin flip |

`docs/strategy_v2.md` §6.1 cited **"walk-forward 2/7 vs 1/7"** as one of the 5 pieces of evidence that "static beats dynamic". The two intervals overlap almost completely. **A difference of 1 in 7 samples is not evidence** — that axis has been withdrawn, and the conclusion stands on the remaining 4 axes. From now on, every win rate is reported together with its interval via `backtest/statistics.py::wilson_interval`.

## 3. Pre-registered design

| Item | Details |
|---|---|
| Arms under test | `hodl`, `fixed_hedge 20%` (Shadow default), `regime_hedge_v1` (the strategy rejected in V2) |
| Control arms | `shuffled` (bar-level permutation) / `block_shuffled` (run-level permutation) / `uniform` / `coin_flip`, 200 seeds each |
| Statistics | full-period `final_btc`, mean `score_run` over the 7 walk-forward folds, full-period `max_drawdown_usd` |
| Verdict | one-sided empirical p = (1 + #{control ≥ observed}) / (1 + N), α = 0.05, **family-wise correction mandatory** |
| Reporting | every win rate together with its Wilson interval |

The control arms go through **the same risk pipeline** as the real strategies (20pp daily cap, 10pp minimum rebalance, 85% cap).

## 4. Round 1 results — 2 significant before correction, 0 after correction

**15** tests were actually run (2 arms × control arms × 3 statistics). Šidák-corrected α = **0.00341**.

| arm | Control arm | Statistic | p | Before correction | After correction |
|---|---|---|---:|---|---|
| fixed_20 | coin_flip | final_btc | 0.0348 | significant | **not significant** |
| regime_v1 | shuffled | wf_mean_score | 0.0348 | significant | **not significant** |
| regime_v1 | uniform | wf_mean_score | 0.0348 | significant | **not significant** |
| fixed_20 | uniform | final_btc | 0.0398 | significant | **not significant** |
| (remaining 11) | | | ≥ 0.06 | — | not significant |

> **⚠️ Design limitation admitted**: with 200 seeds the p floor is 1/201 = **0.00498**, which is **larger than** the corrected threshold 0.00341. In other words, this design **cannot, in principle, detect family-wise corrected significance.** A result of no significance after correction cannot distinguish "there is no effect" from "the test is underpowered". Claiming corrected significance requires ≥ 300 seeds. However, the results in §5 and §6 reach their conclusions regardless of this limitation.

## 5. What the 2 results significant before correction really were — transaction costs

We examined the mechanism behind the result that `fixed_20` "has more BTC than random".

| | Trades | Fees (BTC) | Funding (BTC) | Final BTC |
|---|---:|---:|---:|---:|
| `fixed_20` (actual) | **5** | −0.00037 | +0.0709 | **1.1594** |
| `uniform` control | **962** | −0.0828 | +0.0721 | 0.9793 |
| `coin_flip` control | **1,308** | −0.1176 | +0.0648 | 0.8382 |

**Funding received is almost identical** (naturally, given the same average exposure). The difference is entirely friction. Of the 0.180 gap in `final_btc`, fees alone account for 0.083; adding slippage, **about 92% is transaction costs**.

In other words, "static 20% beats random" means **"trading 5 times beats trading 962 times"**. It is not an insight into the market.

## 6. The flaw in the control arm itself and its fix — and a reversed conclusion

The finding above exposes a flaw in the control-arm design. **A bar-level shuffle destroys not only timing but also persistence.** A strategy that held the same target for weeks at a time becomes one that changes almost every day, so it trades far more than the arm it is compared with.

| | Trades | vs actual |
|---|---:|---:|
| `regime_v1` (actual) | 243 | 1.00× |
| `shuffled` (bar-level) | 1,607 | **6.61×** |
| `block_shuffled` (run-level) | 265 | **1.09×** |

Of the `final_btc` gap against the bar-level shuffle, **69% is explained by fees alone**. So the `regime_v1 wf_mean_score p=0.0348` in §4 measured a cost difference, not timing skill.

`BlockShuffleControl` **permutes whole runs, i.e. stretches over which the target value stays constant,** preserving the run-length distribution and the number of transitions. The multiset of values is identical; only the alignment with the market is destroyed. This is the fair control arm that isolates timing alone.

### Fair control-arm results (200 seeds, trades 243 vs 270)

| Statistic | Observed | Control mean | [p05, p95] | **Percentile** | p |
|---|---:|---:|---:|---:|---:|
| Final BTC | 1.0977 | **1.2913** | [0.591, 2.369] | **41.0** | 0.592 |
| Walk-forward mean score | −0.3017 | −0.3527 | [−0.511, −0.182] | 70.0 | 0.304 |
| Maximum drawdown | −0.4757 | −0.5594 | [−0.688, −0.420] | 84.5 | 0.159 |

**The actual timing of `regime_v1` produces less BTC than the median of random rearrangements of its own signal (41st percentile).** The p=0.035 that looked significant against the bar-level shuffle becomes **p=0.30** against the fair control arm.

On the drawdown side, the 84.5th percentile (p=0.159) is the highest, for what it is worth, but it is not significant either, and it is one of 15 tests in the first place.

## 7. Verdict

| Question | Answer |
|---|---|
| Is any arm significantly better than the control arms? | **No** (0 after correction) |
| What were the 2 that were significant before correction? | **Transaction-cost-avoidance artefacts** (§5, 92% is friction) |
| Does the timing of `regime_v1` carry information? | **No.** 41st percentile against the fair control arm — worse than the median random rearrangement |
| Is `fixed_20` better than random? | Only in the sense that it trades less. Not a market insight |
| Did this study prove "no effect"? | **No.** It cannot be told apart from insufficient power (admitted in §4). It is "absence of evidence", not "evidence of absence" |

**V1's "no timing alpha" is now a measurement, not an impression.** And V2's rejection of the dynamic regime is reconfirmed from the control-arm perspective — except that the reason is not "because static beat dynamic" but **"because dynamic cannot even beat random rearrangements of itself"**.

## 8. Impact on operations

1. **The Shadow default `fixed_hedge 20%` stays.** The rationale has not changed — it was the robustness shown in V3 §6 (surviving −1bp adverse funding, the worst 2-year window), not a performance edge. These results merely reconfirm that modest rationale.
2. **Any change in the direction of more frequent trading is opposed, citing §5's table.** 962 trades lost 0.18 BTC to 5 trades.
3. Any attempt to revive `regime_hedge_v1` must first refute §6's 41st percentile.

## 9. Remaining limitations

* **Insufficient power**: with 200 seeds, family-wise corrected significance cannot be detected (§4). A re-run with 300 or more seeds is needed.
* There is still only 1 crash sample. Control arms cannot touch this problem.
* The block shuffle preserves the run-length distribution, but runs of equal value that end up adjacent merge, so the number of transitions decreases slightly (243 → 265–270, about +9%). Preservation is not perfect.
* The 14 start-date-dependence windows overlap at 91-day spacing, so they are not independent samples. Their Wilson interval is narrower than the true one.

**Returns are not guaranteed.** The conclusion of this document is not "something works" but **"we found nothing distinguishable from random, and what we thought we had found was fees"**.
