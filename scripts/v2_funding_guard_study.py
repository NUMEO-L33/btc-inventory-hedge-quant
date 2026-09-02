"""V2 step 1 study: does the funding guard add value, and does the dynamic regime add value?

2×2 design — {fixed 50% hedge, regime hedge V1} × {no guard, funding guard} — evaluated on:

* the full tradable period,
* the pre-registered time-ordered splits (research / validation / out-of-sample),
* the nested walk-forward folds (per-fold win rate), and
* a funding-regime split (days when the trailing funding was adverse vs not).

The guard's parameters are NOT tuned here: a single economically motivated setting is used
(21 settlements ≈ 7 days, threshold 0, scale 0.5, cap 45%). A sensitivity table over a small
neighbourhood is printed only to show the result is not a knife-edge — not to pick a winner.

Usage:  python scripts/v2_funding_guard_study.py [--out reports/v2_funding_guard]
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from btc_inventory_quant.backtest.runner import LoadedMarket, load_market, run_strategy
from btc_inventory_quant.backtest.walkforward import build_folds, split_boundaries
from btc_inventory_quant.config.loader import build_run_metadata, load_config
from btc_inventory_quant.config.schema import (
    AppConfig,
    FixedHedgeParams,
    FundingGuardParams,
    StrategyConfig,
    StrategyName,
)
from btc_inventory_quant.strategy.benchmarks import build_strategy
from btc_inventory_quant.strategy.funding_guard import guard_summary

D = Decimal
GUARD = FundingGuardParams(
    enabled=True,
    lookback_periods=21,
    min_periods=21,
    threshold_rate=D("0"),
    scale=D("0.5"),
    max_ratio_when_adverse=D("0.45"),
)
METRICS = (
    "final_btc",
    "net_btc_growth",
    "cagr_btc",
    "final_usd",
    "cagr_usd",
    "hodl_cagr_usd",
    "excess_vs_hodl_usd_pct",
    "max_drawdown_usd",
    "hodl_max_drawdown_usd",
    "total_funding_btc",
    "total_fees_btc",
    "total_trades",
    "avg_hedge_ratio",
    "verdict",
)


def base_configs(cfg: AppConfig) -> dict[str, StrategyConfig]:
    return {
        "hodl": StrategyConfig(name=StrategyName.HODL),
        "fixed_50": StrategyConfig(
            name=StrategyName.FIXED_HEDGE, fixed_hedge=FixedHedgeParams(ratio=D("0.5"))
        ),
        "regime_v1": StrategyConfig(
            name=StrategyName.REGIME_HEDGE_V1, regime_hedge=cfg.strategy.regime_hedge
        ),
    }


def variants(cfg: AppConfig, guard: FundingGuardParams = GUARD) -> dict[str, StrategyConfig]:
    out: dict[str, StrategyConfig] = {}
    for name, sc in base_configs(cfg).items():
        out[name] = sc
        if name != "hodl":  # a guard on a zero hedge is a no-op
            out[f"{name}+guard"] = sc.model_copy(update={"funding_guard": guard})
    return out


def _row(name: str, segment: str, start: Any, end: Any, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy": name,
        "segment": segment,
        "start": start,
        "end": end,
        **{k: summary.get(k) for k in METRICS},
    }


def run_full_and_splits(
    market: LoadedMarket, cfg: AppConfig, specs: dict[str, StrategyConfig]
) -> tuple[pd.DataFrame, dict[str, Any]]:
    bounds = split_boundaries(market, cfg)
    rows: list[dict[str, Any]] = []
    guard_stats: dict[str, Any] = {}
    segments: dict[str, tuple[datetime | None, datetime | None]] = {"full": (None, None), **bounds}
    for name, sc in specs.items():
        strategy = build_strategy(sc)
        for segment, (start, end) in segments.items():
            run = run_strategy(market, strategy, cfg, start=start, end=end)
            v = run.summary.to_dict()
            rows.append(_row(name, segment, v["start_date"], v["end_date"], v))
            if segment == "full" and "+guard" in name:
                guard_stats[name] = guard_summary(run.result.signals)
    return pd.DataFrame(rows), guard_stats


def run_walk_forward(
    market: LoadedMarket, cfg: AppConfig, specs: dict[str, StrategyConfig]
) -> pd.DataFrame:
    from btc_inventory_quant.strategy.regime_hedge import RegimeHedgeV1

    frame = market.dataset.frame
    warm = max(cfg.backtest.warmup_bars, RegimeHedgeV1(cfg.strategy.regime_hedge).warmup_bars) + 1
    sp = cfg.backtest.split
    folds = build_folds(
        frame.index[warm].to_pydatetime(),
        frame.index[-1].to_pydatetime(),
        sp.walk_forward_train_days,
        sp.walk_forward_test_days,
        sp.walk_forward_step_days,
    )
    rows: list[dict[str, Any]] = []
    for fold in folds:
        for name, sc in specs.items():
            run = run_strategy(
                market, build_strategy(sc), cfg, start=fold.test_start, end=fold.test_end
            )
            v = run.summary.to_dict()
            rows.append(
                {
                    "fold": fold.index,
                    **_row(
                        name,
                        f"fold{fold.index}",
                        fold.test_start.date().isoformat(),
                        fold.test_end.date().isoformat(),
                        v,
                    ),
                }
            )
    return pd.DataFrame(rows)


def funding_regime_split(
    market: LoadedMarket, cfg: AppConfig, specs: dict[str, StrategyConfig]
) -> pd.DataFrame:
    """Performance on days the guard classified as adverse vs the rest (same days for all variants)."""
    guarded_name = next((n for n in specs if n.endswith("+guard")), None)
    if guarded_name is None:
        return pd.DataFrame()
    ref = run_strategy(market, build_strategy(specs[guarded_name]), cfg)
    adverse = ref.result.signals["funding_adverse"].astype(bool)
    rows: list[dict[str, Any]] = []
    for name, sc in specs.items():
        run = run_strategy(market, build_strategy(sc), cfg)
        d = run.result.daily
        flags = adverse.reindex(d.index).fillna(False).to_numpy()
        eq = d["equity_btc"].astype(float)
        ret = eq.pct_change().fillna(0.0).to_numpy()
        hodl_ret = d["hodl_usd"].astype(float).pct_change().fillna(0.0).to_numpy()
        for label, mask in (("funding_adverse", flags), ("funding_normal", ~flags)):
            if mask.sum() == 0:
                continue
            rows.append(
                {
                    "strategy": name,
                    "funding_regime": label,
                    "days": int(mask.sum()),
                    "btc_growth": float((1 + ret[mask]).prod() - 1),
                    "hodl_usd_return": float((1 + hodl_ret[mask]).prod() - 1),
                    "avg_hedge_ratio": float(
                        d["effective_ratio"].astype(float).to_numpy()[mask].mean()
                    ),
                }
            )
    return pd.DataFrame(rows)


def guard_sensitivity(market: LoadedMarket, cfg: AppConfig) -> pd.DataFrame:
    """Neighbourhood check: is the result stable around the chosen guard setting?"""
    base = StrategyConfig(
        name=StrategyName.FIXED_HEDGE, fixed_hedge=FixedHedgeParams(ratio=D("0.5"))
    )
    rows: list[dict[str, Any]] = []
    for lookback in (9, 21, 45):
        for scale in (D("0.25"), D("0.5"), D("0.75")):
            guard = GUARD.model_copy(
                update={"lookback_periods": lookback, "min_periods": lookback, "scale": scale}
            )
            sc = base.model_copy(update={"funding_guard": guard})
            v = run_strategy(market, build_strategy(sc), cfg).summary.to_dict()
            rows.append(
                {
                    "lookback_periods": lookback,
                    "scale": str(scale),
                    **{
                        k: v.get(k)
                        for k in (
                            "final_btc",
                            "cagr_usd",
                            "max_drawdown_usd",
                            "total_funding_btc",
                            "total_trades",
                            "verdict",
                        )
                    },
                }
            )
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/backtest.yaml")
    ap.add_argument("--out", default="reports/v2_funding_guard")
    ap.add_argument("--as-of", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    metadata = build_run_metadata(cfg, args.config, run_name="v2_funding_guard")
    as_of = datetime.fromisoformat(args.as_of).replace(tzinfo=UTC) if args.as_of else None
    market = load_market(cfg, as_of=as_of)
    specs = variants(cfg)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    splits, guard_stats = run_full_and_splits(market, cfg, specs)
    splits.to_csv(out / "splits.csv", index=False)
    wf = run_walk_forward(market, cfg, specs)
    wf.to_csv(out / "walk_forward.csv", index=False)
    regimes = funding_regime_split(market, cfg, specs)
    regimes.to_csv(out / "funding_regime_split.csv", index=False)
    sens = guard_sensitivity(market, cfg)
    sens.to_csv(out / "guard_sensitivity.csv", index=False)

    wins = {}
    for name in specs:
        sub = wf[wf["strategy"] == name]
        wins[name] = {
            "folds": len(sub),
            "folds_btc_positive": int((sub["net_btc_growth"].astype(float) > 0).sum()),
            "mean_net_btc_growth": float(sub["net_btc_growth"].astype(float).mean()),
            "mean_max_drawdown_usd": float(sub["max_drawdown_usd"].astype(float).mean()),
        }
    summary = {
        "metadata": metadata.to_dict(),
        "guard_params": {k: str(v) for k, v in GUARD.model_dump().items()},
        "guard_activity": guard_stats,
        "walk_forward_summary": wins,
        "note": "Guard parameters were fixed a priori, not tuned on these results.",
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    pd.set_option("display.width", 220)
    print("\n=== full period & pre-registered splits ===")
    print(
        splits[
            [
                "strategy",
                "segment",
                "final_btc",
                "cagr_usd",
                "max_drawdown_usd",
                "total_funding_btc",
                "total_trades",
                "verdict",
            ]
        ].to_string(index=False)
    )
    print("\n=== walk-forward (per-fold summary) ===")
    print(json.dumps(wins, indent=2))
    print("\n=== funding regime split ===")
    print(regimes.to_string(index=False))
    print("\n=== guard neighbourhood (fixed_50 base) ===")
    print(sens.to_string(index=False))
    print("\n=== guard activity ===")
    print(json.dumps(guard_stats, indent=2))
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
