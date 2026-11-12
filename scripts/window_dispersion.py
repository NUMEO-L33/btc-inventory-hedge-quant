#!/usr/bin/env python3
"""How much of a hedge ratio's outcome is decided by WHEN you started?

Why this exists
---------------
The V3 verdict column requires ``btc_growth > 0`` over the FULL period, and full-period BTC
growth in this dataset comes from the 2022 drawdown. So every PASS/FAIL statement in V3 §6.1 —
including the "10~20% survives adverse funding" band that was used to justify the shadow
default — is a function of ONE episode. That is not a rule; it is a single observation wearing
a rule's clothes.

This script judges the ratios by a quantity that uses ALL start-date windows instead: the
dispersion of the final BTC count. It answers a question the pooled result cannot —
*how much does the answer depend on something we cannot predict?* — and the four research
rounds already established that we cannot predict entry timing.

It reads the saved V3 artefacts; it does not re-run the backtest.

    python scripts/window_dispersion.py [--dir reports/v3_static_ratio]
"""

from __future__ import annotations

import argparse
import csv
import statistics as st
from collections import defaultdict
from pathlib import Path

CRASH_MDD = -0.50  # a window "contains a crash" if HODL drew down at least this much inside it


def load(directory: Path) -> tuple[dict[float, dict[str, float]], dict[str, float]]:
    by_ratio: dict[float, dict[str, float]] = defaultdict(dict)
    hodl_mdd: dict[str, float] = {}
    with (directory / "start_date_dependence.csv").open() as fh:
        for row in csv.DictReader(fh):
            by_ratio[float(row["ratio"])][row["start"]] = float(row["final_btc"])
            hodl_mdd[row["start"]] = float(row["hodl_max_drawdown_usd"])
    if not by_ratio:
        raise SystemExit(
            "no rows in start_date_dependence.csv — run static_ratio_frontier.py first"
        )
    return by_ratio, hodl_mdd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="reports/v3_static_ratio")
    args = ap.parse_args()

    by_ratio, hodl_mdd = load(Path(args.dir))
    crash = sorted(s for s, v in hodl_mdd.items() if v <= CRASH_MDD)
    calm = sorted(s for s in hodl_mdd if s not in crash)

    print(
        f"{len(hodl_mdd)} windows / crash windows {len(crash)} / calm windows {len(calm)}  "
        f"(criterion: HODL drawdown inside the window <= {CRASH_MDD:.0%})"
    )
    print(
        "The windows overlap — the number of independent samples is much smaller (2-year windows, 91 days apart).\n"
    )

    print(
        f"{'ratio':>5} | {'worst':>7} {'median':>8} {'best':>7} | {'range':>7} | {'max/min':>9} | "
        f"{'calm median':>11} {'crash median':>11}"
    )
    print("-" * 92)
    for ratio in sorted(by_ratio):
        v = list(by_ratio[ratio].values())
        lo, hi = min(v), max(v)
        spread = f"{hi / lo:9.2f}" if lo > 0 else f"{'inf':>9}"
        print(
            f"{ratio * 100:4.0f}% | {lo:7.3f} {st.median(v):8.3f} {hi:7.3f} | {hi - lo:7.3f} | "
            f"{spread} | {st.median([by_ratio[ratio][s] for s in calm]):11.3f} "
            f"{st.median([by_ratio[ratio][s] for s in crash]):11.3f}"
        )

    print(
        "\nDoes the entry date flip the outcome even at the same ratio and through the same crash:"
    )
    peak, late = "2021-03-10", "2022-03-09"
    if peak in hodl_mdd and late in hodl_mdd:
        for ratio in sorted(by_ratio):
            if ratio == 0 or round(ratio * 100) % 10:
                continue
            a, b = by_ratio[ratio][peak], by_ratio[ratio][late]
            print(
                f"  {ratio * 100:3.0f}%: entry just before the peak {a:.3f}  vs  entry after the decline began {b:.3f}"
                f"   ({a / b:.1f}x)"
            )
        print(
            "\n  It is not the fact that a crash happened but 'was the hedge on before the peak' that decides the outcome."
        )
        print("  That is timing, and V1/V4 concluded that this strategy has no timing edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
