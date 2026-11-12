# Strategy V5 study — sample extension (pre-registered)

**Written: 2026-09-11, before looking at a single line of the extended data.**
This document is written first so that the criteria cannot be changed after the results are seen. Since in V3 §3 the
objective function was pre-registered and its inconsistency was only discovered afterwards, this time **even the pass criteria
and the way a failure will be reported** are nailed down in advance.

---

## 1. Why do this

The problem V3 §10 exposed: **on the benefit side (payout in a crash) there are effectively only 1–2 samples**. Cost and
start-date dependence were measured over 14 windows, but the only payout events are the 2022 drawdown and the 2024–2025 drawdown.
The expected value of insurance cannot be verified from 1–2 claims.

COIN-M `BTCUSD_PERP` was listed on 2020-08-11 and so **has no real funding data before that.**
Extending the sample requires another source, and the moment the source changes, a new question arises: "can that source
stand in for our contract?" **Unless that question is cleared first, the extension results are meaningless.**

## 2. The extension source and its limits

| | Choice | Reason |
|---|---|---|
| Adopted | **Binance USDT-M `BTCUSDT` futures** (2019-09-08 onward) | Same exchange, **real funding rates**, adds 1 crash (2020-03, −50%) |
| Rejected | Spot + assumed funding (2013 onward) | Gains 3 crashes, but **funding — the only effect that survived V4 — becomes an assumption.** The very question that can be answered changes |
| Rejected | BitMEX XBTUSD (2016 onward) | Closer in being inverse, but it is a **different exchange**, with rates unrelated to our operating account |

**USDT-M is linear (USDT-margined); our contract is inverse (BTC-margined).** The PnL formulas differ.
So all we take from USDT-M is **the price series and the funding-rate series**, and the inverse-contract
formulas are applied unchanged. Whether this substitution is legitimate is the §3 gate.

## 3. Gate — proxy validation (this must pass before going on to §4)

**Over the overlap period 2020-08-11 to 2026-09-01 the two funding-rate series are compared directly.**
Both symbols have real data in this period, so it can be measured without assumptions.

**Pre-registered pass criteria (all three must be met for PASS):**

| # | Criterion | Threshold |
|---|---|---|
| G1 | **Median absolute deviation** of the rate difference per 8-hour settlement | ≤ **0.002%** (0.2bp) per 8h |
| G2 | Relative error of **cumulative funding received** (at a 20% hedge, over the whole overlap period) | ≤ **15%** |
| G3 | Sign agreement rate (share of settlements with the same positive/negative direction) | ≥ **95%** |

Rationale for the thresholds — fixed before seeing the results:
* **G1's 0.2bp**: the adverse-funding stress in V3 §6.1 flipped the verdict at −1bp/8h. If the proxy
  error exceeds 1/5 of that stress, conclusions on the extended period cannot be told apart from proxy error.
* **G2's 15%**: the funding verification measured live (`docs/inverse_math.md` §6) came out VERIFIED at ±8.12%.
  If the proxy error is more than 2× the measurement error we already carry, it cannot be used.
* **G3's 95%**: if the direction differs, whether the hedge is a cost or an income flips. This is not a question of magnitude.

**On FAIL**: §4 is **not run.** We report "USDT-M is not a substitute for COIN-M funding"
as the result and record that the sample cannot be extended by this route. We do not relax the thresholds or
look for other criteria. That is the kind of error V3 §3 made.

## 4. Extension analysis (only if the gate is passed)

1. Re-run the start-date-dependence analysis of `static_ratio_frontier.py` on the extended period
   **2019-09-08 to 2026-09-01** (2-year windows, 91 days apart). The window count goes from 14 to about 20 in all.
2. Recompute the **best/worst ratio** for each hedge ratio with `window_dispersion.py`.

**Pre-registered verdict question — exactly one:**

> When windows containing the 2020-03 crash are added, **is the ordering of the best/worst ratio across hedge ratios
> (monotonically increasing) preserved?**

* **Preserved → the conclusion of V3 §10.4 (the lower the ratio, the lower the dependence on the starting point) has been
  reproduced on a second event as well.** The case for keeping 20% gets stronger.
* **Reversed → §10.4 is withdrawn.** In that case no argument supporting 20% remains, so the ratio
  decision is reverted to "unsupported" and reported as such.

**This study does not look for an "optimal ratio".** V3 concluded there is none, and adding 6 windows does not
change that conclusion. The only thing asked is **whether the existing conclusion holds up on a new event**.

## 5. Limitations disclosed in advance

* Even with 6 more windows, **the number of independent samples is still in single digits**. 7 years ÷ 2-year windows ≈ 3.5.
* The 2020-03 crash was −50% within two days, **different in character from 2022 (−77% over 1 year).** Lumping them together as
  the same "crash" is itself a crude simplification. State this difference explicitly when interpreting the results.
* In the 2019-09 to 2020-08 period **the COIN-M contract did not exist.** Results for that period are the counterfactual "if
  this contract had existed back then", and do not mean that trades could actually have been executed.
* Even if G1–G3 are passed by the proxy, that is **agreement over the overlap period**. There is no guarantee it would be
  the same in the 2019–2020 period, and this cannot be verified.

## 6. Recording the results

Recorded in `reports/v5_sample_extension/` and in §7 of this document. **Even if the study stops at the gate, §7 is still
written.** A study that was not run is also a result.

## 7. Results — gate FAIL, §4 was not run (2026-09-11)

Reproduce: `python scripts/v5_proxy_gate.py` · Record: `reports/v5_sample_extension/proxy_gate.json`

Data: USDT-M `BTCUSDT` funding, 7,674 records (2019-09-10 to 2026-09-11); COIN-M `BTCUSD_PERP`, 6,641 records.
Overlapping settlements: **6,641** (2020-08-10 16:00 to 2026-09-02 08:00 UTC).

| Criterion | Threshold | Measured | Verdict |
|---|---|---|---|
| G1 median absolute deviation of the rate difference | ≤ 0.00002 | **0.00003310** (exceeds it by a factor of 1.66) | **FAIL** |
| G2 relative error of cumulative funding | ≤ 15% | **18.05%** | **FAIL** |
| G3 sign agreement rate | ≥ 95% | **82.88%** | **FAIL** |

**All three failed. Per §3's pre-registration, the §4 extension analysis was not run, and the thresholds were not touched.**

### 7.1 Why it failed — not a near miss on the thresholds

G3 is the biggest failure. **In about 1 settlement in 6 (17.1%, 1,137 settlements) the two contracts had opposite funding signs.**
That means there were that many settlements in which the same short position receives on one side and pays on the other.
Of the mismatches, 38% are harmless cases where both rates are close to 0; the rest are not.

| Settlement time (UTC) | COIN-M | USDT-M |
|---|---:|---:|
| 2020-09-04 00:00 | **−0.001423** | +0.000100 |
| 2021-12-04 08:00 | **−0.001209** | +0.000063 |
| 2023-03-13 00:00 | −0.000790 | +0.000100 |

**Of all places, the divergence occurs in stress regimes.** 2021-12-04 was the day of a liquidation cascade, and on it
the COIN-M rate spiked to −12bp while USDT-M stayed at +0.6bp. The two are similar in normal times but
**diverge widely in crashes** — yet the very purpose of this extension study was to add crash samples.
The proxy is least usable exactly where it is most needed.

Nor is the direction simple noise: 18.6% of COIN-M settlements are negative (shorts pay), versus 13.9% for USDT-M.
The mean rate is COIN-M 0.0000871 / USDT-M 0.0001028, i.e. USDT-M is about 18% more generous.
This means BTC-margined and USDT-margined contracts are markets with different participants and different basis dynamics.

### 7.2 What the conclusion is

* **USDT-M is not a substitute for COIN-M funding.** The sample cannot be extended by this route.
* As a side finding, **it is confirmed that using only COIN-M's own data in V1–V4 was the right choice.**
  Had the proxy been used, the funding carry — the only effect that survived V4 — would have been overestimated by 18%.
* **The fact that the COIN-M contract was listed in 2020-08 cannot be circumvented.** Real funding before then
  does not exist, and there is no way to manufacture it. This is not a problem to be solved but a **limitation**.
* Therefore **the limitation in V3 §10.5 remains as it was**: there are still only 1–2 benefit samples (payouts in a crash),
  and there is no way to verify the expected value of the insurance with this data.

### 7.3 The remaining option — a separate pre-registration is required

The verdict of V3 §10.4 (the lower the ratio, the less final BTC depends on the starting point) is about
**dispersion**, and against the cross-window spread of 0.545 BTC at 20%, cumulative funding over the same period
is about 0.04 BTC, **roughly 7%**. In other words, that verdict is driven mostly by the **price path**. That being
so, with funding set to 0 or a fixed stress value, one can ask
**whether the dispersion ordering holds in the crashes of 2020-03 · 2018 · 2014 on the price path alone**.

**But that is not this document's question.** Changing the question after failing the gate and tacking it on is
exactly retrofitting. Doing it requires **a fresh pre-registration in a separate document**, which must first nail
down at least the following: (a) what value funding is fixed at, and the rationale; (b) how the fixed value will
reflect the fact observed in 7.1 that "in a crash the COIN-M rate spikes as far as −12bp"; (c) an explicit statement
that price-path-only results will not be read as actual strategy performance.
