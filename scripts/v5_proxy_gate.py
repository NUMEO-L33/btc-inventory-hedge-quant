#!/usr/bin/env python3
"""V5 §3 gate — can the USDT-M funding rate stand in for the COIN-M rate?

Compares the two rate series directly over the overlap (the period in which both symbols have
real data). The pass criteria G1–G3 and their thresholds were recorded in
`docs/strategy_v5_sample_extension.md` §3 **before looking at the data**. This script only applies
those criteria as written and does not take the thresholds as arguments — if it accepted them, a
failure would tempt someone to change them.

G2 note: for a fixed-ratio hedge, funding per settlement (BTC) = ratio x equity_btc x rate, so
**the mark price cancels out** (contracts = ratio x equity x mark / CV, funding = contracts x CV /
mark x rate). Comparing cumulative funding is therefore the same as comparing the sums of the
rates, and differences in the mark-price source do not enter.

    python scripts/v5_proxy_gate.py
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from decimal import Decimal
from pathlib import Path

# docs/strategy_v5_sample_extension.md §3 — pre-registered thresholds. Defined here and nowhere else.
G1_MAX_MEDIAN_ABS_DIFF = Decimal("0.00002")  # 0.2bp per 8h
G2_MAX_REL_ERROR = Decimal("0.15")
G3_MIN_SIGN_AGREEMENT = Decimal("0.95")
HEDGE_RATIO = Decimal("0.20")


def load_funding(raw_dir: Path) -> dict[int, Decimal]:
    out: dict[int, Decimal] = {}
    pages = sorted((raw_dir / "funding_rate").glob("page_*.json"))
    if not pages:
        raise SystemExit(f"no funding pages under {raw_dir}/funding_rate")
    for page in pages:
        for row in json.loads(page.read_text(encoding="utf-8")):
            out[int(row["fundingTime"])] = Decimal(str(row["fundingRate"]))
    return out


def sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin-m", default="data/raw_new")
    ap.add_argument(
        "--usdt-m", default="/mnt/user-data/uploads/btc-inventory-hedge-quant/data/raw_usdtm"
    )
    ap.add_argument("--out", default="reports/v5_sample_extension")
    args = ap.parse_args()

    coinm = load_funding(Path(args.coin_m))
    usdtm = load_funding(Path(args.usdt_m))

    # Settlement timestamps are not always identical to the millisecond; bucket to the 8h grid.
    def bucket(series: dict[int, Decimal]) -> dict[int, Decimal]:
        step = 8 * 3_600_000
        return {(t // step) * step: r for t, r in series.items()}

    c, u = bucket(coinm), bucket(usdtm)
    common = sorted(set(c) & set(u))
    if not common:
        raise SystemExit("the two series have no settlement times in common")

    diffs = [c[t] - u[t] for t in common]
    abs_diffs = sorted(abs(d) for d in diffs)
    median_abs = Decimal(str(st.median([float(d) for d in abs_diffs])))

    sum_c = sum((c[t] for t in common), Decimal(0))
    sum_u = sum((u[t] for t in common), Decimal(0))
    fund_c = HEDGE_RATIO * sum_c  # BTC per 1 BTC of equity
    fund_u = HEDGE_RATIO * sum_u
    rel_err = abs(fund_u - fund_c) / abs(fund_c) if fund_c else Decimal("Infinity")

    agree = sum(1 for t in common if sign(c[t]) == sign(u[t]))
    agreement = Decimal(agree) / Decimal(len(common))

    g1 = median_abs <= G1_MAX_MEDIAN_ABS_DIFF
    g2 = rel_err <= G2_MAX_REL_ERROR
    g3 = agreement >= G3_MIN_SIGN_AGREEMENT
    verdict = "PASS" if (g1 and g2 and g3) else "FAIL"

    def ms(t: int) -> str:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")

    print(f"Overlapping settlements: {len(common)}  ({ms(common[0])} ~ {ms(common[-1])} UTC)")
    print(f"COIN-M total {len(c)} / USDT-M total {len(u)}\n")

    print(
        f"G1 median abs rate difference       : {median_abs:.8f}  (<= {G1_MAX_MEDIAN_ABS_DIFF}) -> {'PASS' if g1 else 'FAIL'}"
    )
    print(
        f"G2 cumulative funding relative error: {rel_err:.4%}      (<= {G2_MAX_REL_ERROR:.0%})   -> {'PASS' if g2 else 'FAIL'}"
    )
    print(
        f"G3 sign agreement rate              : {agreement:.4%}      (>= {G3_MIN_SIGN_AGREEMENT:.0%})   -> {'PASS' if g3 else 'FAIL'}"
    )
    print(f"\nVerdict: {verdict}")

    print("\nReference figures (not used for the verdict)")
    print(
        f"  cumulative funding at a 20% hedge  COIN-M {fund_c:+.6f} BTC / USDT-M {fund_u:+.6f} BTC"
    )
    print(
        f"  rate-difference quantiles  p50 {abs_diffs[len(abs_diffs) // 2]:.8f}"
        f"  p90 {abs_diffs[int(len(abs_diffs) * 0.9)]:.8f}"
        f"  p99 {abs_diffs[int(len(abs_diffs) * 0.99)]:.8f}"
        f"  max {abs_diffs[-1]:.8f}"
    )
    print(f"  mean rate  COIN-M {sum_c / len(common):.8f} / USDT-M {sum_u / len(common):.8f}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "proxy_gate.json").write_text(
        json.dumps(
            {
                "verdict": verdict,
                "overlap_settlements": len(common),
                "overlap_first_utc": ms(common[0]),
                "overlap_last_utc": ms(common[-1]),
                "G1_median_abs_diff": str(median_abs),
                "G1_threshold": str(G1_MAX_MEDIAN_ABS_DIFF),
                "G1": "PASS" if g1 else "FAIL",
                "G2_relative_error": str(rel_err),
                "G2_threshold": str(G2_MAX_REL_ERROR),
                "G2": "PASS" if g2 else "FAIL",
                "G3_sign_agreement": str(agreement),
                "G3_threshold": str(G3_MIN_SIGN_AGREEMENT),
                "G3": "PASS" if g3 else "FAIL",
                "cumulative_funding_btc_coin_m": str(fund_c),
                "cumulative_funding_btc_usdt_m": str(fund_u),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nWritten: {out_dir / 'proxy_gate.json'}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
