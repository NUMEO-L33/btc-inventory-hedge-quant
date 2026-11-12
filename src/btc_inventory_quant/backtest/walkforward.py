"""Over-fitting controls (prompt §6): time-ordered 60/20/20 split, limited grid search,
nested walk-forward, plateau (neighbourhood) preference, start-date dependence.

Rules implemented here:
* never shuffle — every split is chronological;
* parameters are selected on the *train* window only and evaluated on the following test window;
* the selection objective prefers stable neighbourhoods over the single best point;
* every experiment (grid, folds, scores) is written to disk with the run metadata (seed, hash).
"""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from btc_inventory_quant.backtest.reports import (
    SERIES,
    StrategyRun,
    _figure,
    _save,
    _style_axes,
)
from btc_inventory_quant.backtest.runner import LoadedMarket, load_market, run_strategy
from btc_inventory_quant.config.loader import RunMetadata
from btc_inventory_quant.config.schema import (
    AppConfig,
    FixedHedgeParams,
    RegimeHedgeParams,
    StrategyConfig,
    StrategyName,
)
from btc_inventory_quant.strategy.benchmarks import build_strategy
from btc_inventory_quant.strategy.regime_hedge import RegimeHedgeV1

# Limited grid: 2 × 3 × 2 × 3 × 2 = 72 combinations (kept deliberately small)
DEFAULT_GRID: dict[str, list[Any]] = {
    "hedge_bull": [Decimal("0.10"), Decimal("0.20")],
    "hedge_neutral": [Decimal("0.30"), Decimal("0.45"), Decimal("0.60")],
    "hedge_bear": [Decimal("0.70"), Decimal("0.85")],
    "regime_confirm_bars": [2, 5, 10],
    "slope_lookback": [5, 10],
}
GRID_KEYS = list(DEFAULT_GRID)


def score_run(v: dict[str, Any]) -> float:
    """Composite objective (documented in docs/backtest_methodology.md):

    0.5 · CAGR_BTC + 0.5 · (CAGR_USD − HODL_CAGR_USD) − 0.25 · max(0, MDD_HODL_USD − MDD_USD)

    i.e. reward BTC growth and USD performance relative to HODL equally, penalise a USD drawdown
    worse than HODL. NaNs score −inf.
    """
    try:
        cagr_btc = float(v["cagr_btc"])
        excess = float(v["cagr_usd"]) - float(v["hodl_cagr_usd"])
        mdd = float(v["max_drawdown_usd"])
        mdd_hodl = float(v["hodl_max_drawdown_usd"])
    except (KeyError, TypeError, ValueError):
        return float("-inf")
    if any(math.isnan(x) for x in (cagr_btc, excess, mdd, mdd_hodl)):
        return float("-inf")
    penalty = max(
        0.0, mdd_hodl - mdd
    )  # mdd values are negative; worse-than-HODL → positive penalty
    return 0.5 * cagr_btc + 0.5 * excess - 0.25 * penalty


def params_from_combo(base: RegimeHedgeParams, combo: dict[str, Any]) -> RegimeHedgeParams:
    return base.model_copy(update=combo)


def grid_combos(
    grid: dict[str, list[Any]] | None = None, max_grid: int | None = None
) -> list[dict[str, Any]]:
    g = grid or DEFAULT_GRID
    keys = list(g)
    combos = [
        dict(zip(keys, values, strict=True)) for values in itertools.product(*(g[k] for k in keys))
    ]
    if max_grid is not None:
        combos = combos[:max_grid]
    return combos


def neighbourhood_scores(results: pd.DataFrame, grid: dict[str, list[Any]]) -> pd.Series:
    """Mean score over combos differing in exactly one parameter by one grid step."""
    keys = [k for k in grid if k in results.columns]
    index = {tuple(str(row[k]) for k in keys): float(row["score"]) for _, row in results.iterrows()}
    out = []
    for _, row in results.iterrows():
        neigh: list[float] = []
        for k in keys:
            values = [str(v) for v in grid[k]]
            pos = values.index(str(row[k]))
            for step in (-1, 1):
                j = pos + step
                if 0 <= j < len(values):
                    key = tuple(values[j] if kk == k else str(row[kk]) for kk in keys)
                    if key in index:
                        neigh.append(index[key])
        out.append(sum(neigh) / len(neigh) if neigh else float("nan"))
    return pd.Series(out, index=results.index)


@dataclass(slots=True)
class GridResult:
    table: pd.DataFrame
    best_combo: dict[str, Any]
    best_row: dict[str, Any]


def run_grid(
    market: LoadedMarket,
    config: AppConfig,
    start: datetime | None,
    end: datetime | None,
    grid: dict[str, list[Any]] | None = None,
    max_grid: int | None = None,
) -> GridResult:
    grid = grid or DEFAULT_GRID
    base = config.strategy.regime_hedge
    rows: list[dict[str, Any]] = []
    for combo in grid_combos(grid, max_grid):
        strat = RegimeHedgeV1(params_from_combo(base, combo))
        run = run_strategy(market, strat, config, start=start, end=end)
        v = run.summary.to_dict()
        rows.append(
            {
                **{k: str(val) for k, val in combo.items()},
                "score": score_run(v),
                "final_btc": v["final_btc"],
                "cagr_btc": v["cagr_btc"],
                "cagr_usd": v["cagr_usd"],
                "hodl_cagr_usd": v["hodl_cagr_usd"],
                "excess_vs_hodl_usd_pct": v["excess_vs_hodl_usd_pct"],
                "max_drawdown_usd": v["max_drawdown_usd"],
                "hodl_max_drawdown_usd": v["hodl_max_drawdown_usd"],
                "total_trades": v["total_trades"],
                "total_fees_btc": v["total_fees_btc"],
                "total_funding_btc": v["total_funding_btc"],
                "verdict": v["verdict"],
            }
        )
    table = pd.DataFrame(rows)
    table["neighbour_score"] = neighbourhood_scores(table, grid)
    table["plateau_score"] = 0.5 * table["score"] + 0.5 * table["neighbour_score"].fillna(
        table["score"]
    )
    table = table.sort_values("plateau_score", ascending=False).reset_index(drop=True)
    best = table.iloc[0]
    combo = {k: _coerce(k, best[k], grid) for k in grid if k in table.columns}
    return GridResult(
        table=table, best_combo=combo, best_row={str(k): v for k, v in best.to_dict().items()}
    )


def _coerce(key: str, value: Any, grid: dict[str, list[Any]]) -> Any:
    for candidate in grid[key]:
        if str(candidate) == str(value):
            return candidate
    raise KeyError(f"{key}={value} not in grid")


@dataclass(slots=True)
class Fold:
    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


def build_folds(
    tradable_start: datetime, data_end: datetime, train_days: int, test_days: int, step_days: int
) -> list[Fold]:
    folds: list[Fold] = []
    i = 0
    t0 = tradable_start
    while True:
        train_end = t0 + timedelta(days=train_days)
        test_start = train_end + timedelta(days=1)
        test_end = min(test_start + timedelta(days=test_days - 1), data_end)
        if test_start >= data_end or (test_end - test_start).days < 60:
            break
        folds.append(Fold(i, t0, train_end, test_start, test_end))
        i += 1
        t0 = t0 + timedelta(days=step_days)
    return folds


@dataclass(slots=True)
class WalkForwardResult:
    folds: pd.DataFrame
    grids: dict[int, pd.DataFrame]
    oos_daily: pd.DataFrame  # concatenated test-window daily rows for adaptive params
    reference_daily: pd.DataFrame  # same windows with the fixed reference params
    comparisons: pd.DataFrame  # per-fold metrics for adaptive / reference / hodl / fixed50
    summary: dict[str, Any] = field(default_factory=dict)


def _segment_metrics(run: StrategyRun) -> dict[str, Any]:
    v = run.summary.to_dict()
    return {
        k: v.get(k)
        for k in (
            "final_btc",
            "net_btc_growth",
            "cagr_btc",
            "cagr_usd",
            "hodl_cagr_usd",
            "excess_vs_hodl_usd_pct",
            "max_drawdown_usd",
            "hodl_max_drawdown_usd",
            "total_trades",
            "total_funding_btc",
            "total_fees_btc",
            "verdict",
        )
    }


def run_walk_forward(
    market: LoadedMarket, config: AppConfig, max_grid: int | None = None
) -> WalkForwardResult:
    frame = market.dataset.frame
    warm = (
        max(config.backtest.warmup_bars, RegimeHedgeV1(config.strategy.regime_hedge).warmup_bars)
        + 1
    )
    tradable_start = frame.index[warm].to_pydatetime()
    data_end = frame.index[-1].to_pydatetime()
    sp = config.backtest.split
    folds = build_folds(
        tradable_start,
        data_end,
        sp.walk_forward_train_days,
        sp.walk_forward_test_days,
        sp.walk_forward_step_days,
    )
    fold_rows: list[dict[str, Any]] = []
    grids: dict[int, pd.DataFrame] = {}
    oos_parts: list[pd.DataFrame] = []
    ref_parts: list[pd.DataFrame] = []
    comp_rows: list[dict[str, Any]] = []
    fixed50 = StrategyConfig(
        name=StrategyName.FIXED_HEDGE, fixed_hedge=FixedHedgeParams(ratio=Decimal("0.5"))
    )
    hodl = StrategyConfig(name=StrategyName.HODL)
    for fold in folds:
        grid = run_grid(market, config, fold.train_start, fold.train_end, max_grid=max_grid)
        grids[fold.index] = grid.table
        chosen = RegimeHedgeV1(params_from_combo(config.strategy.regime_hedge, grid.best_combo))
        adaptive = run_strategy(market, chosen, config, start=fold.test_start, end=fold.test_end)
        reference = run_strategy(
            market,
            RegimeHedgeV1(config.strategy.regime_hedge),
            config,
            start=fold.test_start,
            end=fold.test_end,
        )
        b_hodl = run_strategy(
            market, build_strategy(hodl), config, start=fold.test_start, end=fold.test_end
        )
        b_fixed = run_strategy(
            market, build_strategy(fixed50), config, start=fold.test_start, end=fold.test_end
        )
        fold_rows.append(
            {
                "fold": fold.index,
                "train_start": fold.train_start.date().isoformat(),
                "train_end": fold.train_end.date().isoformat(),
                "test_start": fold.test_start.date().isoformat(),
                "test_end": fold.test_end.date().isoformat(),
                **{f"param_{k}": str(v) for k, v in grid.best_combo.items()},
                "train_score": grid.best_row["score"],
                "train_plateau_score": grid.best_row["plateau_score"],
                "test_score_adaptive": score_run(adaptive.summary.to_dict()),
                "test_score_reference": score_run(reference.summary.to_dict()),
            }
        )
        for name, run in (
            ("adaptive", adaptive),
            ("reference_v1", reference),
            ("hodl", b_hodl),
            ("fixed_hedge_50", b_fixed),
        ):
            comp_rows.append(
                {
                    "fold": fold.index,
                    "strategy": name,
                    "test_start": fold.test_start.date().isoformat(),
                    "test_end": fold.test_end.date().isoformat(),
                    **_segment_metrics(run),
                }
            )
        oos_parts.append(adaptive.result.daily.assign(fold=fold.index))
        ref_parts.append(reference.result.daily.assign(fold=fold.index))
    oos_daily = pd.concat(oos_parts) if oos_parts else pd.DataFrame()
    reference_daily = pd.concat(ref_parts) if ref_parts else pd.DataFrame()
    comparisons = pd.DataFrame(comp_rows)
    summary = _stitch_summary(oos_daily, reference_daily, comparisons)
    return WalkForwardResult(
        pd.DataFrame(fold_rows), grids, oos_daily, reference_daily, comparisons, summary
    )


def _chain(daily: pd.DataFrame, col: str) -> float:
    """Compound per-fold growth (each fold restarts at the initial capital)."""
    if daily.empty:
        return float("nan")
    total = 1.0
    for _, grp in daily.groupby("fold"):
        s = grp[col].astype(float)
        start = float(grp["mark_open"].astype(float).iloc[0]) if col == "hodl_usd" else None
        if col == "equity_usd":
            first = float(grp["hodl_btc"].astype(float).iloc[0]) * float(
                grp["mark_open"].astype(float).iloc[0]
            )
            total *= float(s.iloc[-1]) / first
        elif col == "equity_btc":
            total *= float(s.iloc[-1]) / float(grp["hodl_btc"].astype(float).iloc[0])
        elif col == "hodl_usd" and start is not None:
            total *= float(s.iloc[-1]) / (float(grp["hodl_btc"].astype(float).iloc[0]) * start)
    return total - 1.0


def _stitch_summary(
    oos: pd.DataFrame, ref: pd.DataFrame, comparisons: pd.DataFrame
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if oos.empty:
        return out
    out["adaptive_oos_btc_growth"] = _chain(oos, "equity_btc")
    out["adaptive_oos_usd_growth"] = _chain(oos, "equity_usd")
    out["reference_oos_btc_growth"] = _chain(ref, "equity_btc")
    out["reference_oos_usd_growth"] = _chain(ref, "equity_usd")
    out["hodl_oos_usd_growth"] = _chain(oos, "hodl_usd")
    out["folds"] = int(oos["fold"].nunique())
    for name in ("adaptive", "reference_v1", "hodl", "fixed_hedge_50"):
        sub = comparisons[comparisons["strategy"] == name]
        out[f"{name}_folds_btc_positive"] = int((sub["net_btc_growth"].astype(float) > 0).sum())
        out[f"{name}_folds_beat_hodl_usd"] = int(
            (sub["excess_vs_hodl_usd_pct"].astype(float) > 0).sum()
        )
        out[f"{name}_mean_excess_vs_hodl_usd"] = float(
            sub["excess_vs_hodl_usd_pct"].astype(float).mean()
        )
        out[f"{name}_mean_btc_growth"] = float(sub["net_btc_growth"].astype(float).mean())
    return out


# ---------------------------------------------------------------------------
# Static split (60/20/20) and start-date dependence
# ---------------------------------------------------------------------------
def split_boundaries(
    market: LoadedMarket, config: AppConfig
) -> dict[str, tuple[datetime, datetime]]:
    frame = market.dataset.frame
    warm = (
        max(config.backtest.warmup_bars, RegimeHedgeV1(config.strategy.regime_hedge).warmup_bars)
        + 1
    )
    tradable = frame.index[warm:]
    n = len(tradable)
    sp = config.backtest.split
    r_end = int(n * float(sp.research_fraction))
    v_end = int(n * float(sp.research_fraction + sp.validation_fraction))
    return {
        "research": (tradable[0].to_pydatetime(), tradable[r_end - 1].to_pydatetime()),
        "validation": (tradable[r_end].to_pydatetime(), tradable[v_end - 1].to_pydatetime()),
        "out_of_sample": (tradable[v_end].to_pydatetime(), tradable[-1].to_pydatetime()),
    }


def run_split_evaluation(
    market: LoadedMarket,
    config: AppConfig,
    extra_params: dict[str, RegimeHedgeParams] | None = None,
) -> pd.DataFrame:
    bounds = split_boundaries(market, config)
    strategies: dict[str, Any] = {
        "regime_hedge_v1": RegimeHedgeV1(config.strategy.regime_hedge),
        "hodl": build_strategy(StrategyConfig(name=StrategyName.HODL)),
        "fixed_hedge_50": build_strategy(
            StrategyConfig(
                name=StrategyName.FIXED_HEDGE, fixed_hedge=FixedHedgeParams(ratio=Decimal("0.5"))
            )
        ),
        "fixed_hedge_100": build_strategy(
            StrategyConfig(
                name=StrategyName.FIXED_HEDGE,
                fixed_hedge=FixedHedgeParams(ratio=Decimal("1"), bypass_max_hedge_cap=True),
            )
        ),
        "ema200_simple": build_strategy(StrategyConfig(name=StrategyName.EMA200_SIMPLE)),
    }
    for name, params in (extra_params or {}).items():
        strategies[name] = RegimeHedgeV1(params)
    rows = []
    for segment, (start, end) in bounds.items():
        for name, strat in strategies.items():
            run = run_strategy(market, strat, config, start=start, end=end)
            rows.append(
                {
                    "segment": segment,
                    "start": start.date().isoformat(),
                    "end": end.date().isoformat(),
                    "strategy": name,
                    **_segment_metrics(run),
                }
            )
    return pd.DataFrame(rows)


def run_start_date_dependence(
    market: LoadedMarket, config: AppConfig, horizon_days: int = 730, step_days: int = 91
) -> pd.DataFrame:
    frame = market.dataset.frame
    warm = (
        max(config.backtest.warmup_bars, RegimeHedgeV1(config.strategy.regime_hedge).warmup_bars)
        + 1
    )
    first = frame.index[warm].to_pydatetime()
    last = frame.index[-1].to_pydatetime()
    rows = []
    start = first
    strat = RegimeHedgeV1(config.strategy.regime_hedge)
    fixed50 = build_strategy(
        StrategyConfig(
            name=StrategyName.FIXED_HEDGE, fixed_hedge=FixedHedgeParams(ratio=Decimal("0.5"))
        )
    )
    while start + timedelta(days=horizon_days) <= last:
        end = start + timedelta(days=horizon_days)
        for name, s in (("regime_hedge_v1", strat), ("fixed_hedge_50", fixed50)):
            run = run_strategy(market, s, config, start=start, end=end)
            rows.append(
                {
                    "start": start.date().isoformat(),
                    "end": end.date().isoformat(),
                    "strategy": name,
                    **_segment_metrics(run),
                }
            )
        start += timedelta(days=step_days)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Study runner + report
# ---------------------------------------------------------------------------
def run_walk_forward_study(
    config: AppConfig,
    metadata: RunMetadata,
    out_dir: Path | None = None,
    as_of: datetime | None = None,
    max_grid: int | None = None,
    market: LoadedMarket | None = None,
) -> Path:
    market = market or load_market(config, as_of=as_of)
    out = out_dir or (config.backtest.report_dir / metadata.run_id)
    out.mkdir(parents=True, exist_ok=True)

    bounds = split_boundaries(market, config)
    research_grid = run_grid(market, config, *bounds["research"], max_grid=max_grid)
    research_grid.table.to_csv(out / "grid_research.csv", index=False)
    best_params = params_from_combo(config.strategy.regime_hedge, research_grid.best_combo)
    split = run_split_evaluation(
        market, config, extra_params={"regime_hedge_research_best": best_params}
    )
    split.to_csv(out / "split_results.csv", index=False)

    wf = run_walk_forward(market, config, max_grid=max_grid)
    wf.folds.to_csv(out / "walk_forward_folds.csv", index=False)
    wf.comparisons.to_csv(out / "walk_forward_fold_metrics.csv", index=False)
    wf.oos_daily.to_csv(out / "walk_forward_oos_daily.csv")
    wf.reference_daily.to_csv(out / "walk_forward_reference_daily.csv")
    for i, g in wf.grids.items():
        g.to_csv(out / f"grid_fold_{i:02d}.csv", index=False)

    start_dep = run_start_date_dependence(market, config)
    start_dep.to_csv(out / "start_date_dependence.csv", index=False)

    charts_dir = out / "charts"
    charts_dir.mkdir(exist_ok=True)
    _chart_walk_forward(wf, charts_dir / "walk_forward_oos_equity.png")
    _chart_start_dependence(start_dep, charts_dir / "start_date_dependence.png")

    summary = {
        "metadata": metadata.to_dict(),
        "split_boundaries": {
            k: (a.date().isoformat(), b.date().isoformat()) for k, (a, b) in bounds.items()
        },
        "grid": {
            "definition": {k: [str(x) for x in v] for k, v in DEFAULT_GRID.items()},
            "objective": score_run.__doc__,
            "research_best": {k: str(v) for k, v in research_grid.best_combo.items()},
            "research_best_row": _jsonable(research_grid.best_row),
        },
        "walk_forward": wf.summary,
        "folds": wf.folds.to_dict(orient="records"),
        "config_sha256": metadata.config_sha256,
    }
    (out / "research_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    _write_research_report(
        out / "research_report.md", summary, split, wf, start_dep, research_grid.table
    )
    return out


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (
            None
            if isinstance(v, float) and math.isnan(v)
            else (str(v) if isinstance(v, Decimal) else v)
        )
        for k, v in d.items()
    }


def _chart_walk_forward(wf: WalkForwardResult, path: Path) -> None:
    if wf.oos_daily.empty:
        return
    fig, ax = _figure("Walk-forward OOS equity multiples")
    for i, (name, daily) in enumerate(
        (("adaptive", wf.oos_daily), ("reference_v1", wf.reference_daily))
    ):
        parts = []
        for _, grp in daily.groupby("fold"):
            base = float(grp["hodl_btc"].astype(float).iloc[0]) * float(
                grp["mark_open"].astype(float).iloc[0]
            )
            parts.append(grp["equity_usd"].astype(float) / base)
        s = pd.concat(parts)
        ax.plot(s.index, s.values, color=SERIES[i], linewidth=1.4, label=name)
    parts = []
    for _, grp in wf.oos_daily.groupby("fold"):
        base = float(grp["hodl_btc"].astype(float).iloc[0]) * float(
            grp["mark_open"].astype(float).iloc[0]
        )
        parts.append(grp["hodl_usd"].astype(float) / base)
    s = pd.concat(parts)
    ax.plot(s.index, s.values, color=SERIES[2], linewidth=1.0, linestyle="--", label="hodl")
    for f in wf.folds.itertuples():
        ax.axvline(pd.Timestamp(str(f.test_start), tz="UTC"), color="#e5e4df", linewidth=0.8)
    _style_axes(ax, "x")
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    _save(fig, path)


def _chart_start_dependence(df: pd.DataFrame, path: Path) -> None:
    if df.empty:
        return
    fig, (ax1, ax2) = _figure(
        "Start-date dependence (2-year horizon): net BTC growth / USD max drawdown",
        nrows=2,
        height=3.0,
    )
    hodl_drawn = False
    for i, name in enumerate(("regime_hedge_v1", "fixed_hedge_50")):
        sub = df[df["strategy"] == name]
        x = pd.to_datetime(sub["start"])
        ax1.plot(
            x,
            sub["net_btc_growth"].astype(float).values,
            color=SERIES[i],
            marker="o",
            markersize=4,
            linewidth=1.2,
            label=name,
        )
        ax2.plot(
            x,
            sub["max_drawdown_usd"].astype(float).values,
            color=SERIES[i],
            marker="o",
            markersize=4,
            linewidth=1.2,
            label=name,
        )
        if not hodl_drawn:
            ax2.plot(
                x,
                sub["hodl_max_drawdown_usd"].astype(float).values,
                color=SERIES[2],
                linestyle="--",
                linewidth=1.0,
                label="hodl",
            )
            hodl_drawn = True
    ax1.axhline(0, color="#52514e", linewidth=1, linestyle="--")
    _style_axes(ax1, "net BTC growth")
    _style_axes(ax2, "USD max drawdown", pct=True)
    ax1.legend(frameon=False, fontsize=8, loc="upper right")
    ax2.legend(frameon=False, fontsize=8, loc="lower right")
    _save(fig, path)


def _pct(v: Any) -> str:
    try:
        return f"{float(v):+.2%}"
    except (TypeError, ValueError):
        return "n/a"


def _write_research_report(
    path: Path,
    summary: dict[str, Any],
    split: pd.DataFrame,
    wf: WalkForwardResult,
    start_dep: pd.DataFrame,
    grid: pd.DataFrame,
) -> None:
    lines: list[str] = []
    lines.append(
        "# Overfitting validation report (Split / Grid / Walk-forward / start-date dependence)"
    )
    lines.append("")
    md = summary["metadata"]
    lines.append(
        f"- run: `{md['run_id']}` / git `{md['git_commit']}` / config `{md['config_sha256'][:12]}` / seed {md['seed']}"
    )
    b = summary["split_boundaries"]
    lines.append(
        f"- Chronological split: research {b['research'][0]}→{b['research'][1]} / validation {b['validation'][0]}→{b['validation'][1]} / OOS {b['out_of_sample'][0]}→{b['out_of_sample'][1]}"
    )
    lines.append(
        "- Principles: no random shuffling; parameters are selected on the research/training windows only; no going back to revise parameters after seeing the OOS results (any change made after this report invalidates the OOS)."
    )
    lines.append("")
    lines.append("## 1. Research-segment Grid Search (72 combinations, top 10 by plateau score)")
    lines.append("")
    lines.append("Objective: " + " ".join(str(summary["grid"]["objective"]).split()))
    lines.append("")
    cols = [
        *GRID_KEYS,
        "score",
        "neighbour_score",
        "plateau_score",
        "final_btc",
        "cagr_usd",
        "excess_vs_hodl_usd_pct",
        "max_drawdown_usd",
        "total_trades",
    ]
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("|" + "---|" * len(cols))
    for _, r in grid.head(10).iterrows():
        lines.append("| " + " | ".join(_cell(r[c]) for c in cols) + " |")
    ref = grid[
        (grid["hedge_bull"] == "0.10")
        & (grid["hedge_neutral"] == "0.45")
        & (grid["hedge_bear"] == "0.85")
        & (grid["regime_confirm_bars"] == "2")
        & (grid["slope_lookback"] == "5")
    ]
    if len(ref):
        lines.append("")
        lines.append(
            f"- Research-segment rank of the reference V1 (0.10/0.45/0.85, 2-day confirmation, 5-day slope): {int(ref.index[0]) + 1} / {len(grid)} (plateau score {float(ref['plateau_score'].iloc[0]):.4f})"
        )
    lines.append("")
    lines.append("## 2. Performance by segment (research / validation / OOS)")
    lines.append("")
    lines.append(
        "| Segment | Strategy | Final BTC | BTC CAGR | USD CAGR | HODL USD CAGR | USD vs HODL | USD MDD | HODL MDD | Trades | Funding | Verdict |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in split.iterrows():
        lines.append(
            f"| {r['segment']} | {r['strategy']} | {float(r['final_btc']):.4f} | {_pct(r['cagr_btc'])} | {_pct(r['cagr_usd'])} | {_pct(r['hodl_cagr_usd'])} | {_pct(r['excess_vs_hodl_usd_pct'])} | {_pct(r['max_drawdown_usd'])} | {_pct(r['hodl_max_drawdown_usd'])} | {r['total_trades']} | {float(r['total_funding_btc']):.4f} | {r['verdict']} |"
        )
    lines.append("")
    lines.append(
        "## 3. Nested Walk-forward (train 730 days → test 180 days, stepping forward 180 days)"
    )
    lines.append("")
    lines.append(
        "| Fold | Test window | Selected parameters (bull/neutral/bear/confirm/slope) | Train score | Test score (adaptive) | Test score (fixed V1) |"
    )
    lines.append("|---|---|---|---|---|---|")
    for _, r in wf.folds.iterrows():
        params = "/".join(str(r[f"param_{k}"]) for k in GRID_KEYS)
        lines.append(
            f"| {r['fold']} | {r['test_start']}→{r['test_end']} | {params} | {float(r['train_score']):.4f} | {float(r['test_score_adaptive']):.4f} | {float(r['test_score_reference']):.4f} |"
        )
    lines.append("")
    lines.append(
        "| Fold | Strategy | Final BTC | USD vs HODL | USD MDD | HODL MDD | Trades | Verdict |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for _, r in wf.comparisons.iterrows():
        lines.append(
            f"| {r['fold']} | {r['strategy']} | {float(r['final_btc']):.4f} | {_pct(r['excess_vs_hodl_usd_pct'])} | {_pct(r['max_drawdown_usd'])} | {_pct(r['hodl_max_drawdown_usd'])} | {r['total_trades']} | {r['verdict']} |"
        )
    lines.append("")
    s = summary["walk_forward"]
    if s:
        lines.append(
            f"- Chained folds (each fold restarts at 1 BTC, compounded) BTC growth: adaptive {_pct(s.get('adaptive_oos_btc_growth'))} / fixed V1 {_pct(s.get('reference_oos_btc_growth'))}"
        )
        lines.append(
            f"- Chained-fold USD growth: adaptive {_pct(s.get('adaptive_oos_usd_growth'))} / fixed V1 {_pct(s.get('reference_oos_usd_growth'))} / HODL {_pct(s.get('hodl_oos_usd_growth'))}"
        )
        for name in ("adaptive", "reference_v1", "fixed_hedge_50"):
            lines.append(
                f"- {name}: folds with BTC growth {s.get(f'{name}_folds_btc_positive')}/{s.get('folds')}, folds beating HODL (USD) {s.get(f'{name}_folds_beat_hodl_usd')}/{s.get('folds')}, mean USD vs HODL {_pct(s.get(f'{name}_mean_excess_vs_hodl_usd'))}, mean BTC growth {float(s.get(f'{name}_mean_btc_growth') or 0):+.4f}"
            )
    lines.append("")
    lines.append("## 4. Start-date dependence (2-year holds, start dates 91 days apart)")
    lines.append("")
    for name in ("regime_hedge_v1", "fixed_hedge_50"):
        sub = start_dep[start_dep["strategy"] == name]
        if len(sub):
            btc = sub["net_btc_growth"].astype(float)
            usd = sub["excess_vs_hodl_usd_pct"].astype(float)
            lines.append(
                f"- {name}: of {len(sub)} start dates, BTC grew in {(btc > 0).sum()} and HODL (USD) was beaten in {(usd > 0).sum()}; median BTC growth {btc.median():+.4f} (min {btc.min():+.4f}, max {btc.max():+.4f}); median USD vs HODL {usd.median():+.2%} (min {usd.min():+.2%}, max {usd.max():+.2%})"
            )
    lines.append("")
    lines.append("![walk_forward](charts/walk_forward_oos_equity.png)")
    lines.append("")
    lines.append("![start_date](charts/start_date_dependence.png)")
    lines.append("")
    lines.append("## 5. Interpretation guidelines")
    lines.append("")
    lines.append(
        "- If the adaptive parameters are not consistently better than the fixed V1 out of sample, the grid search only adds overfitting risk → keep the fixed parameters."
    )
    lines.append(
        "- If BTC shrinks or the strategy trails HODL (USD) in half or more of the folds, the strategy is taken to have no timing alpha (only the funding carry remains)."
    )
    lines.append(
        "- If the sign of the result flips depending on the start date, the performance depends on a specific period (e.g. the 2022 bear market)."
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _cell(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)
