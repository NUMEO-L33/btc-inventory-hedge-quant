#!/usr/bin/env python3
"""V5 §3 관문 — USDT-M 펀딩 요율이 COIN-M 요율을 대신할 수 있는가.

겹치는 구간(두 심볼 모두 실제 데이터가 있는 기간)에서 두 요율 시계열을 직접 대조한다.
합격 기준 G1~G3 과 그 임계값은 `docs/strategy_v5_sample_extension.md` §3 에 **데이터를 보기
전에** 기록했다. 이 스크립트는 그 기준을 그대로 적용할 뿐이며, 임계값을 인자로 받지 않는다 —
받을 수 있게 만들면 실패했을 때 손이 간다.

G2 메모: 고정 비율 헤지에서는 정산당 펀딩(BTC) = ratio x equity_btc x rate 로 **마크가격이
약분된다**(contracts = ratio x equity x mark / CV, funding = contracts x CV / mark x rate).
따라서 누적 펀딩 비교는 요율 합의 비교와 같고, 마크가격 출처 차이가 끼어들지 않는다.

    python scripts/v5_proxy_gate.py
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from decimal import Decimal
from pathlib import Path

# docs/strategy_v5_sample_extension.md §3 — 사전 등록된 임계값. 여기서만 정의한다.
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
        raise SystemExit("두 시계열에 겹치는 정산 시각이 없다")

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

    print(f"겹치는 정산: {len(common)}건  ({ms(common[0])} ~ {ms(common[-1])} UTC)")
    print(f"COIN-M 전체 {len(c)}건 / USDT-M 전체 {len(u)}건\n")

    print(
        f"G1 요율차 중앙절대편차 : {median_abs:.8f}  (<= {G1_MAX_MEDIAN_ABS_DIFF}) -> {'PASS' if g1 else 'FAIL'}"
    )
    print(
        f"G2 누적펀딩 상대오차   : {rel_err:.4%}      (<= {G2_MAX_REL_ERROR:.0%})   -> {'PASS' if g2 else 'FAIL'}"
    )
    print(
        f"G3 부호 일치율         : {agreement:.4%}      (>= {G3_MIN_SIGN_AGREEMENT:.0%})   -> {'PASS' if g3 else 'FAIL'}"
    )
    print(f"\n판정: {verdict}")

    print("\n참고 수치 (판정에는 쓰지 않는다)")
    print(f"  20% 헤지 누적 펀딩  COIN-M {fund_c:+.6f} BTC / USDT-M {fund_u:+.6f} BTC")
    print(
        f"  요율차 분위수  p50 {abs_diffs[len(abs_diffs) // 2]:.8f}"
        f"  p90 {abs_diffs[int(len(abs_diffs) * 0.9)]:.8f}"
        f"  p99 {abs_diffs[int(len(abs_diffs) * 0.99)]:.8f}"
        f"  max {abs_diffs[-1]:.8f}"
    )
    print(f"  평균 요율  COIN-M {sum_c / len(common):.8f} / USDT-M {sum_u / len(common):.8f}")

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
    print(f"\n기록: {out_dir / 'proxy_gate.json'}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
