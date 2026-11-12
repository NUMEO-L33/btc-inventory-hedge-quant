# Strategy V3 study — static hedge ratio (the only parameter left)

Research script: `scripts/static_ratio_frontier.py` → results in `reports/v3_static_ratio/`
Data: `data/raw_new` (snapshot including 8h mark prices, 2020-08-11 ~ 2026-09-01)

> **Conclusion first**: **no optimal ratio exists in this data.** The pre-registered verdict statistic pointed at the grid boundary (0%) — and under the rule I set, that means **inconclusive**. The frontier is nearly linear, and the noise between adjacent 5%p steps is larger than the full range. In other words, the static ratio is **a choice of risk preference, not an optimisation result**. And the band that passes every robustness check is **10~20%**, where the drawdown reduction is small. **With this data, a "large drawdown reduction" cannot be delivered in a verified form.**

---

## 1. Why this study was done

V2 rejected the dynamic regime classifier (`docs/strategy_v2.md` §6.1). That leaves **just one** free parameter, the static ratio — and that parameter **had never been searched.** The 72-combination grid in `reports/research_v1/` swept the *regime* parameters (bull/neutral/bear), and `fixed_50` was merely a value slotted in as a benchmark. **The number 50 had no basis.**

## 2. Pre-registered design (fixed before looking at the results)

| Item | Content |
|---|---|
| Grid | ratio ∈ {0.00, 0.05, …, 0.85}, 18 points. 0.85 is the hard risk cap |
| **Verdict statistic** | **Mean `score_run` over the walk-forward test folds.** Full-period performance is not used for the verdict |
| Plateau rule | Each ratio is re-scored as the mean of itself and its ±5%p neighbours. A sharp peak loses to a broad plateau |
| **Boundary rule** | If the winner sits on a grid boundary (0.00 or 0.85), it is reported **not as a winner but as "inconclusive"** |
| Deliverable | Not a single number but a **frontier** (BTC quantity vs USD maximum drawdown). If the frontier is flat, it is written down as is: "this is not optimisation but risk preference" |
| Robustness | Pre-registered splits + start-date dependence (2-year holds, 91-day spacing) + adverse funding (−1bp/−3bp) |

## 3. Verdict-statistic result — and my design error

The mean `score_run` **decreased monotonically** as the ratio rose. The maximum is at 0.00.

| Ratio | 0.00 | 0.20 | 0.50 | 0.65 | 0.85 |
|---|---:|---:|---:|---:|---:|
| WF mean score | **0.000** | −0.168 | −0.386 | −0.446 | −0.506 |
| WF mean final BTC | 1.000 | 0.985 | 0.954 | 0.945 | **0.936** |
| Folds with BTC growth | — | 2/7 | 2/7 | 2/7 | 2/7 |
| **Wilson 95% CI** | — | [0.082, 0.641] | [0.082, 0.641] | [0.082, 0.641] | [0.082, 0.641] |

> **⚠️ The fold win-rate column is not evidence (added 2026-09-02)**: the Wilson 95% intervals of all ratios are [0.082, 0.641] — **identical, and all of them contain 0.5.** "2/7" should be read not as "bad" but as **"cannot be known with n=7"**. This column is not used as grounds for the §7 verdict below.

**The winner is on the grid boundary (0.00) → inconclusive under the boundary rule.**

> **⚠️ Design error, confessed**: `score_run` = 0.5·CAGR_BTC + 0.5·(CAGR_USD − HODL) − 0.25·max(0, MDD_HODL − MDD), and the final penalty term applies **only when the strategy's drawdown is worse than HODL's**. That is, **it awards no score at all for reducing drawdown below HODL's.** Yet drawdown reduction is the main purpose of this strategy. I should have caught this inconsistency when I pre-registered the objective function. I did not.
>
> Even so, swapping in a different objective function after seeing the results would be retrofitting. So **the pre-registered result is reported as is, above**, and from here on the judgement is made on the **frontier**, which does not depend on the objective function (rule 4).

One more thing: averaged over the walk-forward folds, **every hedge ratio lost BTC** (0.993 → 0.936) — even though over the full period every ratio of 0.10 and above increased BTC. This contradiction is the crux of this study (§5).

## 4. The frontier — there is no optimum

![frontier](../reports/v3_static_ratio/charts/static_ratio_frontier.png)

| Ratio | Final BTC | USD CAGR | USD maximum drawdown | Funding (BTC) | Trades |
|---|---:|---:|---:|---:|---:|
| 0% (HODL) | 1.000 | +6.4% | −76.7% | 0 | 0 |
| 20% | 1.159 | +9.4% | −69.0% | 0.071 | 5 |
| **25%** | **1.091** | +8.1% | −65.6% | 0.091 | 4 |
| **30%** | **1.271** | +11.2% | −61.2% | 0.137 | 10 |
| 50% | 1.245 | +10.8% | −48.7% | 0.245 | 11 |
| 65% | **1.328** | +12.1% | −35.8% | 0.332 | 14 |
| 85% | 1.166 | +9.5% | **−14.8%** | 0.438 | 8 |

**The oscillation between adjacent 5%p steps (25% → 30%: BTC 1.091 → 1.271, +0.18) is comparable to the entire spread (1.09~1.33, 0.24).** That is noise, not signal. The maximum of 1.328 at 0.65 is a peak, not a plateau, so it is not a candidate for adoption.

**Cost/benefit is effectively linear.** Averaged over the walk-forward folds (180 days):

| Ratio | BTC loss per fold | Drawdown reduction vs HODL | BTC cost per 1%p of drawdown |
|---|---:|---:|---:|
| 10% | −0.70% | 2.1%p | 0.33% |
| 50% | −4.59% | 12.0%p | 0.38% |
| 85% | −6.45% | 20.3%p | 0.32% |

**At every ratio you pay about 0.33% of BTC (per 180 days) for each 1%p of drawdown.** There is no band where it can be bought cheaply = **there is nothing to optimise.** This is a pure dial.

## 5. Why the full period and walk-forward contradict each other — this is insurance

The per-fold results at the 85% ratio reveal what is really going on.

| Fold | Final BTC | HODL USD CAGR in that segment |
|---|---:|---:|
| 0 | 0.897 | +63.6% |
| **1** | **0.485** | **+625.7%** |
| 2 | 1.142 | −26.0% |
| 3 | 0.767 | +108.4% |
| 4 | 0.838 | +75.7% |
| **5** | **1.532** | **−63.9%** |
| 6 | 0.888 | +34.8% |

**Only 2 of the 7 folds made a profit, and those 2 folds are exactly the bear markets.** And in fold 1 (HODL +626%, a parabolic rally) it **lost 51.5% of its BTC.**

The same structure appears, monotonically, in the split segments as well.

| Ratio | validation (bull market) | out_of_sample (bear market) |
|---|---:|---:|
| 0% | 1.000 | 1.000 |
| 20% | 0.919 | 1.146 |
| 50% | 0.780 | 1.299 |
| 85% | **0.633** | **1.475** |

**In the bull market, a monotonic loss as the ratio rises; in the bear market, a monotonic gain.** That is the very definition of **a directional bet on the market phase** rather than an edge.

Stated honestly: **this strategy is insurance, not alpha.** It pays an insurance premium most of the time and gets paid out when a crash comes. And this sample contains **one crash (2022).** With data that has only one claim event, the expected value of the insurance cannot be statistically verified.

## 6. Robustness — high ratios do not survive

### 6.1 Adverse funding (the only scenario that produces a FAIL)

| Ratio | Base | −1bp/8h | −3bp/8h |
|---|---|---|---|
| 10~20% | PASS | **PASS** | FAIL |
| 30~35% | PASS | **PASS** | FAIL |
| 25%, 40% and above | PASS | **FAIL** | FAIL |

**Under −1bp adverse funding only roughly the 10~35% band survives, and everything at 40% and above FAILs.** At −3bp every band FAILs — this part was not solved by the V2 funding guard either.

> **⚠️ The paragraph below was withdrawn in §10.1 (2026-09-11).** PASS/FAIL itself requires full-period BTC
> growth, and that growth comes from 2022 alone, so this argument too is a function of a single event. The replacement verdict is in §10.2.
>
> **The PASS/FAIL boundary is not monotonic (added 2026-09-11)**: laying the grid out in 5%p steps gives
> 10·15·20 PASS → **25 FAIL** → 30·35 PASS → 40 and above FAIL. The adverse-funding burden increases
> monotonically with the ratio, so pass/fail should be monotonic too, but it is not. **In other words, this boundary is
> noise, not signal.** Therefore the PASS at 30·35%, which sits just above a FAIL, cannot be used as independent evidence,
> and the only band that can be said to pass safely is **10~20%, which PASSes contiguously**. The "10~20%"
> verdict in §7 is reinforced by this argument.

### 6.2 Start-date dependence (2-year holds, 14 windows)

| Ratio | Windows with BTC growth | **Wilson 95% CI** | **Final BTC in the worst window** |
|---|---:|---:|---:|
| 10% | 7/14 = 50.0% | [0.268, 0.732] | 0.925 |
| **15%** | 8/14 = **57.1%** | [0.326, 0.786] | 0.858 |
| 20% | 6/14 = 42.9% | [0.214, 0.674] | 0.794 |
| 50% | 6/14 = 42.9% | [0.214, 0.674] | 0.499 |
| 85% | 6/14 = 42.9% | [0.214, 0.674] | **0.284** |

On the win-rate side it is **still inconclusive, even with n=14** — the intervals of all ratios contain 0.5, so the observation that "15% is best at 57.1%" is not evidence (an earlier version cited it as if it were; that is withdrawn). Moreover, the 14 windows overlap one another at 91-day spacing and are not independent samples, so even these intervals come out narrower than they really are.

By contrast, **the worst-window figure, unlike the win rate, is a single observed value, so it has no confidence-interval problem and stands as is**: it worsens monotonically with the ratio, and at 85% **71.6% of the BTC is lost.** The verdicts in §7·§8 rest on this.

## 7. Verdict

| Question | Answer |
|---|---|
| Does an optimal static ratio exist? | **No.** The verdict statistic points at the boundary, hence inconclusive; the frontier is linear; adjacent-grid noise is comparable to the entire spread |
| Is there a basis for 50%? | **None.** But there is no basis for any other value either — 50% is not particularly bad |
| What is the ratio choice? | **A choice of risk preference.** A uniform price list: about 0.33% of BTC (per 180 days) paid for each 1%p of drawdown |
| Which band passes every robustness check? | **10~20%** (survives −1bp adverse funding, worst 2-year window 0.79~0.93) |
| Does that band achieve the goal? | **Hardly.** Drawdown −76.7% → −69~72%. Not the "drastic drawdown reduction" that was wanted |
| What if a high ratio (50~85%) is used? | Drawdown shrinks substantially, to −49~−15%, but BTC loss of 22~37% in the bull market, worst 2-year window −50~−72%, and FAIL under −1bp adverse funding |

**Key conclusion: with this data, a "large drawdown reduction" and "verified robustness" cannot be had at the same time.** One of the two has to be given up explicitly, and research cannot make that choice for you.

## 8. Operational recommendations

1. **The shadow default was set to `fixed_hedge` 20%** (`configs/shadow.yaml`). The reason is not good performance but that **it is the only band that passes every robustness check**; it is a conservative default, not an optimisation result. If your risk preference differs, change the single line `strategy.fixed_hedge.ratio`.

   > **2026-09-11 — after reviewing the full frontier table, the owner explicitly chose 20%.**
   > Rationale: it is the upper end of the band that contiguously passes −1bp adverse funding (10~20%), while the band
   > that actually cuts drawdown substantially (50% and above, drawdown no deeper than −49%) FAILs under adverse funding
   > and has a worst 2-year window BTC of 0.50 or below. **20% is now a stated risk preference, not a placeholder default.**
   > It is still not an optimisation result, though — no such thing exists. Figures at the time of the choice: drawdown −69.0%,
   > worst 2-year window BTC 0.794, bull-market segment 0.919, bear-market segment 1.146.
   >
   > **⚠️ The rationale above was replaced in §10 on the same day.** The rationale "the band that contiguously passes adverse
   > funding" was contaminated by a single event. The conclusion of 20% itself stands, but the reason changes to **it being the band
   > where, across all 14 windows, the final BTC is not heavily determined by the start date** (§10.4).
2. **Always check the §6.2 worst-window figures before raising the ratio.** At 85%, a window that lost 71.6% of its BTC within 2 years actually exists.
3. **Monitor realised funding at all times.** Ratios of 40% and above fail if −1bp/8h adverse funding persists. If this condition is observed, switch on the V2 funding guard (it defends only up to −1bp) or lower the ratio.
4. **Do not set the ratio on the basis of full-period performance (1.25~1.33).** Those numbers depend on the single 2022 crash.

## 9. Remaining limitations

* The sample contains only **one** large crash. The expected value of an insurance-type strategy cannot be verified with this data.
* The pre-registered objective function `score_run` cannot evaluate drawdown reduction (§3). Future studies must fix the objective function first and pre-register it, and **must not choose the objective function on the basis of these results.**
* The walk-forward folds are short, at 180 days, so only 2 folds contain a crash phase.
* The §8 unverified items (live-exchange connection, measured-live check of the funding formula, fee rate) remain open — `docs/requirements_audit.md`.

**No profit is guaranteed.** The conclusion of this study is not "we found the optimal ratio" but **"we confirmed that there is no optimal ratio to be found"**.

---

## 10. Correction — the verdict criterion was contaminated by a single event (2026-09-11)

Reproduce: `python scripts/window_dispersion.py`

### 10.1 What was wrong

The PASS condition of `verdict` includes **full-period `btc_growth > 0`** (`backtest/metrics.py:243`).
But as §5 shows, full-period BTC growth came from only 2 of the 7 folds — exactly the downturn phases.
So everything below is **a function of the single 2022 drawdown**.

* The −1bp adverse-funding PASS/FAIL table in §6.1
* **"The band that passes every robustness check = 10~20%"**, derived from it
* The part of §8 operational recommendation 1 that relied on that band
* The argument added to §6.1 on the same day, "the PASS/FAIL boundary is non-monotonic → the PASS at 30·35% cannot be trusted"

It looked as if several checks had been passed, but in reality it was **the same event being asked about again from several angles**.
It is not a rule; it is 1 observation. This error came to light when the owner pointed out that "a single event in '22 must not be what changes the chosen value;
it has to be found across many other samples".

### 10.2 Replacement — a verdict that uses all 14 windows

Look at the **dispersion** of the final BTC count. The question is not "how much does it earn on average" (which cannot be answered with 1 sample) but
**"how much does the outcome depend on something unpredictable"**, and this uses all 14 windows.

| Ratio | Worst | Median | Best | Range | **Best/worst** | Median of 8 calm windows | Median of 6 crash windows |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10% | 0.925 | 0.983 | 1.170 | 0.245 | **1.26×** | 0.941 | 1.071 |
| 15% | 0.858 | 1.005 | 1.239 | 0.381 | **1.44×** | 0.915 | 1.109 |
| 20% | 0.794 | 0.983 | 1.339 | 0.545 | **1.69×** | 0.859 | 1.129 |
| 30% | 0.680 | 0.950 | 1.577 | 0.897 | 2.32× | 0.787 | 1.196 |
| 50% | 0.499 | 0.849 | 1.976 | 1.477 | 3.96× | 0.645 | 1.269 |
| 85% | 0.284 | 0.698 | 2.811 | 2.527 | **9.91×** | 0.450 | 1.338 |

The windows were not classified by hand; they were split mechanically by **in-window HODL drawdown ≤ −50%** (crash 6 / calm 8).
Because the windows overlap, the number of independent samples is much smaller (6 years ÷ 2 years ≈ 3).

**Three facts backed by many samples:**

1. **At every ratio the median is 1.0 or below** (10%: 0.983 … 85%: 0.698). The *typical* outcome of hedging is a BTC loss.
2. **In the 8 calm windows, 7 out of 8 lose**, worsening monotonically with the ratio.
3. **The best/worst ratio widens from 1.26× (10%) to 9.91× (85%).** The higher the ratio, the more the final BTC count becomes
   a function of *when you started*.

### 10.3 The decisive observation — entry timing, not the crash, makes the outcome

At the same ratio, even facing **the same 2022 episode**, the outcome is reversed.

| Ratio | Entry just before the peak (2021-03) | Entry after the decline began (2022-03) | Multiple |
|---:|---:|---:|---:|
| 20% | 1.339 | 0.987 | 1.4× |
| 50% | 1.976 | 0.891 | 2.2× |
| 85% | **2.811** | **0.731** | **3.8×** |

**What produced the payoff was not "a crash happened" but "the hedge was on before the peak".** That is timing, and
V1 and V4 concluded that this strategy has no timing edge. Therefore **the 2.811 at 85% is not a strategy result one can
expect; it is the outcome of a start-date lottery.** The appeal of a high ratio is visible only in hindsight.

Incidentally, the windows starting 2024-03 / 2024-06 have in-window HODL drawdowns of −49.6% / −51.1% and contain **a second
episode, distinct from 2022** (because of the −50% threshold, the former was classified as calm — a threshold artefact).
In those windows the hedge also paid off (20%: 1.010 / 1.049), but the multiple is not as large as in the 2022 episode.

### 10.4 So is 20% valid?

**It is. But not for the reason §8 gave.**

* Discarded: "because it is the only band that passes every robustness check" — that check was a function of a single event.
* Replacement: **10~20% is the band where the final BTC is not heavily determined by the start date** (best/worst 1.26~1.69×).
  Above it, the BTC count becomes a bet on a timing ability we do not have.

This verdict uses all 14 windows, so it is free of the §10.1 contamination. **But it is still a band, not an optimum.**
The data does not say whether 10% or 20% is better — 10% is more predictable, and 20% is
"the most insurance you can buy without the outcome becoming a lottery". The 15% median, at 1.005, is the only one
above 1.0, but **it is within adjacent-grid noise, so it is not used as evidence** (§4).

### 10.5 Remaining limitation — what this still does not solve

The dispersion verdict measures **cost and dependence** across many samples. **The benefit (the payout in a crash) still rests on
1~2 events.** There is no way to verify the expected value of the insurance with this data.

The real fix is to extend the sample period: 2018 (−84%), 2014 (−85%), 2020-03 (−50%). However,
COIN-M BTCUSD_PERP was listed in 2020-08 and therefore **has no actual funding data** from before then.

> **Tried, and failed (2026-09-11)**: an attempt to use Binance USDT-M `BTCUSDT` (2019-09~) as a proxy
> **failed all three** of the pre-registered gates G1~G3 —
> [docs/strategy_v5_sample_extension.md](strategy_v5_sample_extension.md) §7. Decisively,
> **in about 1 of every 6 settlements the two contracts' funding had opposite signs** (17.1%), and the points of divergence fell,
> of all times, in crash phases (2021-12-04: COIN-M −12bp vs USDT-M +0.6bp). **This route cannot extend
> the sample.** The limitation in this section therefore remains unresolved.
