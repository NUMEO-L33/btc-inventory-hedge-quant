"""V3 study: the static hedge ratio is now the ONLY parameter — what should it be?

Why this study exists
---------------------
V2 rejected the dynamic regime classifier: static ``fixed_50`` beat ``regime_hedge_v1`` on the
full period, on the out-of-sample segment, on walk-forward fold wins and on trade count
(``docs/strategy_v2.md`` §6.1). That leaves exactly one free parameter — the static ratio — and
that parameter has never been searched. The 72-combination grid in ``reports/research_v1/`` swept
the *regime* parameters (bull/neutral/bear); ``fixed_50`` was only ever a benchmark, so the number
50 has no evidence behind it.

PRE-REGISTERED DESIGN (fixed before any result was looked at)
-------------------------------------------------------------
Grid            ratio ∈ {0.00, 0.05, …, 0.85}, 18 points, 5%p apart. 0.85 is the hard risk cap.

Judgment rule   1. The ranking statistic is the mean of ``score_run`` over the WALK-FORWARD test
                   folds — never the full period. (``score_run`` is the objective already
                   documented in docs/backtest_methodology.md: 0.5·CAGR_BTC + 0.5·excess_vs_HODL
                   − 0.25·drawdown-worse-than-HODL penalty.)
                2. PLATEAU OVER PEAK: each ratio is re-scored as the mean of itself and its two
                   neighbours (±5%p). A lone spike loses to a broad shelf on purpose.
                3. A winner sitting on a grid BOUNDARY (0.00 or 0.85) is not a winner — it means
                   the optimum is outside the tested range and the grid must be reported as
                   inconclusive rather than silently clipped.
                4. The deliverable is the FRONTIER (BTC count vs USD max drawdown), not a single
                   number. Where the frontier is flat, the honest answer is "this choice is a risk
                   preference, not an optimisation result" — and it will be reported that way.

Robustness      Every ratio is additionally run on the pre-registered splits, on start-date
                dependence (2-year holds, 91-day steps) and under adverse funding (−1bp/−3bp per
                8h), because adverse funding is the only scenario that ever flipped this strategy
                to FAIL.

Nothing here is tuned on the out-of-sample segment; OOS numbers are reported but are not part of
the ranking statistic.

Usage:  python scripts/static_ratio_frontier.py --config configs/backtest.yaml
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from btc_inventory_quant.backtest.runner import LoadedMarket, load_market, run_strategy
from btc_inventory_quant.backtest.walkforward import build_folds, score_run, split_boundaries
from btc_inventory_quant.config.loader import build_run_metadata, load_config
from btc_inventory_quant.config.schema import (
    AppConfig,
    FixedHedgeParams,
    StrategyConfig,
    StrategyName,
)
from btc_inventory_quant.strategy.benchmarks import build_strategy

D = Decimal
RATIOS: tuple[Decimal, ...] = tuple(D(str(round(0.05 * i, 2))) for i in range(18))  # 0.00 … 0.85
METRICS = (
    "final_btc",
    "net_btc_growth",
    "cagr_btc",
    "cagr_usd",
    "hodl_cagr_usd",
    "excess_vs_hodl_usd_pct",
    "max_drawdown_usd",
    "hodl_max_drawdown_usd",
    "total_funding_btc",
    "total_fees_btc",
    "total_trades",
    "verdict",
)


def spec_for(ratio: Decimal) -> StrategyConfig:
    return StrategyConfig(name=StrategyName.FIXED_HEDGE, fixed_hedge=FixedHedgeParams(ratio=ratio))


def _row(ratio: Decimal, segment: str, v: dict[str, Any]) -> dict[str, Any]:
    return {
        "ratio": float(ratio),
        "segment": segment,
        **{k: v.get(k) for k in METRICS},
        "score": score_run(v),
    }


def sweep_segments(market: LoadedMarket, cfg: AppConfig) -> pd.DataFrame:
    bounds = split_boundaries(market, cfg)
    segments: dict[str, tuple[datetime | None, datetime | None]] = {"full": (None, None), **bounds}
    rows: list[dict[str, Any]] = []
    for ratio in RATIOS:
        strategy = build_strategy(spec_for(ratio))
        for segment, (start, end) in segments.items():
            v = run_strategy(market, strategy, cfg, start=start, end=end).summary.to_dict()
            rows.append(_row(ratio, segment, v))
    return pd.DataFrame(rows)


def sweep_walk_forward(market: LoadedMarket, cfg: AppConfig) -> pd.DataFrame:
    frame = market.dataset.frame
    warm = cfg.backtest.warmup_bars + 1
    sp = cfg.backtest.split
    folds = build_folds(
        frame.index[warm].to_pydatetime(),
        frame.index[-1].to_pydatetime(),
        sp.walk_forward_train_days,
        sp.walk_forward_test_days,
        sp.walk_forward_step_days,
    )
    rows: list[dict[str, Any]] = []
    for ratio in RATIOS:
        strategy = build_strategy(spec_for(ratio))
        for fold in folds:
            v = run_strategy(
                market, strategy, cfg, start=fold.test_start, end=fold.test_end
            ).summary.to_dict()
            rows.append({"fold": fold.index, **_row(ratio, f"fold{fold.index}", v)})
    return pd.DataFrame(rows)


def sweep_start_dates(
    market: LoadedMarket, cfg: AppConfig, horizon_days: int = 730, step_days: int = 91
) -> pd.DataFrame:
    frame = market.dataset.frame
    first = frame.index[cfg.backtest.warmup_bars + 1].to_pydatetime()
    last = frame.index[-1].to_pydatetime()
    rows: list[dict[str, Any]] = []
    for ratio in RATIOS:
        strategy = build_strategy(spec_for(ratio))
        start = first
        while start + timedelta(days=horizon_days) <= last:
            end = start + timedelta(days=horizon_days)
            v = run_strategy(market, strategy, cfg, start=start, end=end).summary.to_dict()
            rows.append({"start": start.date().isoformat(), **_row(ratio, "start_date", v)})
            start += timedelta(days=step_days)
    return pd.DataFrame(rows)


def sweep_adverse_funding(market: LoadedMarket, cfg: AppConfig) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for bps, label in ((D("0"), "base"), (D("1"), "adverse_-1bp"), (D("3"), "adverse_-3bp")):
        cost = cfg.backtest.cost.model_copy(update={"funding_adverse_bps_per_period": bps})
        for ratio in RATIOS:
            v = run_strategy(
                market, build_strategy(spec_for(ratio)), cfg, cost=cost
            ).summary.to_dict()
            rows.append(_row(ratio, label, v))
    return pd.DataFrame(rows)


def plateau_ranking(wf: pd.DataFrame) -> pd.DataFrame:
    """Rule 1 + 2: mean walk-forward score, then re-scored as the mean with its ±5%p neighbours."""
    mean_score = wf.groupby("ratio")["score"].mean().sort_index()
    folds_positive = (
        wf.assign(win=wf["net_btc_growth"].astype(float) > 0).groupby("ratio")["win"].sum()
    )
    values = mean_score.to_numpy(dtype=float)
    padded = np.concatenate([[values[0]], values, [values[-1]]])
    plateau = np.array([padded[i : i + 3].mean() for i in range(len(values))])
    return pd.DataFrame(
        {
            "ratio": mean_score.index,
            "wf_mean_score": values,
            "plateau_score": plateau,
            "wf_folds_btc_positive": folds_positive.reindex(mean_score.index).to_numpy(),
            "wf_mean_final_btc": wf.groupby("ratio")["final_btc"].mean().reindex(mean_score.index),
            "wf_mean_mdd_usd": wf.groupby("ratio")["max_drawdown_usd"]
            .mean()
            .reindex(mean_score.index),
        }
    ).reset_index(drop=True)


def chart_frontier(full: pd.DataFrame, ranking: pd.DataFrame, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from btc_inventory_quant.backtest.reports import L

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.2))
    f = full.sort_values("ratio")
    x = f["max_drawdown_usd"].astype(float) * 100
    y = f["final_btc"].astype(float)
    ax1.plot(x, y, "-o", color="#2a6f97", markersize=4)
    for _, r in f.iterrows():
        if round(r["ratio"] * 100) % 20 == 0 or r["ratio"] in (0.85,):
            ax1.annotate(
                f"{r['ratio'] * 100:.0f}%",
                (float(r["max_drawdown_usd"]) * 100, float(r["final_btc"])),
                textcoords="offset points",
                xytext=(6, 4),
                fontsize=9,
            )
    ax1.set_xlabel(L("USD 최대낙폭 (%)", "USD max drawdown (%)"))
    ax1.set_ylabel(L("최종 BTC 수량", "final BTC"))
    ax1.set_title(L("정적 헤지 프론티어 (전체 기간)", "Static hedge frontier (full period)"))
    ax1.grid(alpha=0.3)

    r = ranking.sort_values("ratio")
    ax2.plot(
        r["ratio"] * 100,
        r["wf_mean_score"],
        "-o",
        label=L("WF 평균 점수", "WF mean score"),
        markersize=4,
    )
    ax2.plot(
        r["ratio"] * 100,
        r["plateau_score"],
        "-s",
        label=L("평지 점수 (±5%p 평균)", "plateau score"),
        markersize=4,
    )
    ax2.set_xlabel(L("정적 헤지 비율 (%)", "static hedge ratio (%)"))
    ax2.set_ylabel(L("점수", "score"))
    ax2.set_title(L("워크포워드 판정 (전체기간 아님)", "Walk-forward ranking (not full period)"))
    ax2.legend()
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/backtest.yaml")
    ap.add_argument("--out", default="reports/v3_static_ratio")
    ap.add_argument("--as-of", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    metadata = build_run_metadata(cfg, args.config, run_name="v3_static_ratio")
    as_of = datetime.fromisoformat(args.as_of).replace(tzinfo=UTC) if args.as_of else None
    market = load_market(cfg, as_of=as_of)
    out = Path(args.out)
    (out / "charts").mkdir(parents=True, exist_ok=True)

    segments = sweep_segments(market, cfg)
    segments.to_csv(out / "sweep_segments.csv", index=False)
    wf = sweep_walk_forward(market, cfg)
    wf.to_csv(out / "walk_forward.csv", index=False)
    start_dates = sweep_start_dates(market, cfg)
    start_dates.to_csv(out / "start_date_dependence.csv", index=False)
    adverse = sweep_adverse_funding(market, cfg)
    adverse.to_csv(out / "adverse_funding.csv", index=False)

    ranking = plateau_ranking(wf)
    ranking.to_csv(out / "ranking.csv", index=False)
    full = segments[segments["segment"] == "full"].reset_index(drop=True)
    full.to_csv(out / "frontier_full_period.csv", index=False)
    chart_frontier(full, ranking, out / "charts" / "static_ratio_frontier.png")

    best = ranking.loc[ranking["plateau_score"].idxmax()]
    boundary = bool(best["ratio"] in (float(RATIOS[0]), float(RATIOS[-1])))
    sd = start_dates.groupby("ratio").agg(
        windows=("final_btc", "size"),
        share_btc_positive=("net_btc_growth", lambda s: float((s.astype(float) > 0).mean())),
        worst_final_btc=("final_btc", "min"),
    )
    sd.to_csv(out / "start_date_summary.csv")

    summary = {
        "metadata": metadata.to_dict(),
        "design": {
            "grid": [float(r) for r in RATIOS],
            "ranking_statistic": "mean score_run over walk-forward test folds",
            "plateau_rule": "score re-averaged with +/-5pp neighbours",
            "boundary_rule": "a winner at 0.00 or 0.85 is reported as inconclusive",
            "pre_registered": True,
        },
        "plateau_winner": {
            "ratio": float(best["ratio"]),
            "wf_mean_score": float(best["wf_mean_score"]),
            "plateau_score": float(best["plateau_score"]),
            "wf_folds_btc_positive": int(best["wf_folds_btc_positive"]),
            "on_grid_boundary": boundary,
        },
        "data": market.quality["settlement_marks"],
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    pd.set_option("display.width", 220)
    print("\n=== full period frontier ===")
    print(
        full[
            [
                "ratio",
                "final_btc",
                "cagr_usd",
                "max_drawdown_usd",
                "total_funding_btc",
                "total_trades",
                "verdict",
            ]
        ].to_string(index=False)
    )
    print("\n=== walk-forward ranking (the judgment statistic) ===")
    print(ranking.to_string(index=False))
    print("\n=== start-date dependence (2y holds) ===")
    print(sd.to_string())
    print("\n=== plateau winner ===")
    print(json.dumps(summary["plateau_winner"], indent=2))
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
