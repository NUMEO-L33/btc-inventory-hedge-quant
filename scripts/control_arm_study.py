"""V4: does any of this beat a null? Control arms and empirical p-values.

Everything reported so far compared strategies against HODL and against each other. Nothing was
compared against a WORTHLESS strategy, so no result could be called distinguishable from luck.
This study supplies the missing baseline.

PRE-REGISTERED DESIGN (fixed before any result was looked at)
-------------------------------------------------------------
Arms under test   hodl, fixed_hedge 20% (the shipped shadow default), fixed_hedge 50%,
                  regime_hedge_v1 (the V2-rejected dynamic strategy).

Controls          For each arm, N seeds of:
                  * shuffled  — that arm's OWN targets permuted in time (identical marginal
                                distribution, timing destroyed). The decisive control.
                  * uniform   — U(0, 2·mean exposure), clipped at the 85% cap.
                  * coin_flip — {0, 2·mean exposure} with p=0.5.
                  All pass through the same risk pipeline as the real arm.

Statistics        full-period final_btc; mean score_run over the 7 walk-forward test folds;
                  full-period max_drawdown_usd.

Judgment rule     One-sided empirical p = (1 + #{control >= observed}) / (1 + N).
                  * p > 0.05                      -> indistinguishable from the null.
                  * The V3 sweep tested 18 ratios, so any claim about a CHOSEN ratio is additionally
                    held to the Sidak-corrected level for a family of 18 (alpha ~= 0.00285).
                  * A shuffled-control p > 0.05 means the arm's TIMING carries no information,
                    whatever its full-period number looks like.

Reporting rule    Every win rate is reported with a Wilson 95% interval, never as a bare fraction.

Usage:  python scripts/control_arm_study.py --config configs/backtest.yaml --seeds 200
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from btc_inventory_quant.backtest.runner import LoadedMarket, load_market, run_strategy
from btc_inventory_quant.backtest.statistics import (
    empirical_p_value,
    percentile_rank,
    sidak_alpha,
    wilson_interval,
)
from btc_inventory_quant.backtest.walkforward import build_folds, score_run
from btc_inventory_quant.config.loader import build_run_metadata, load_config
from btc_inventory_quant.config.schema import (
    AppConfig,
    FixedHedgeParams,
    StrategyConfig,
    StrategyName,
)
from btc_inventory_quant.strategy.base import Strategy
from btc_inventory_quant.strategy.benchmarks import build_strategy
from btc_inventory_quant.strategy.controls import (
    CoinFlipControl,
    ShuffledTimingControl,
    UniformRandomControl,
)

D = Decimal
V3_FAMILY_SIZE = 18  # the V3 sweep tested 18 ratios; a chosen ratio is held to this family
ALPHA = 0.05


def arms(cfg: AppConfig) -> dict[str, StrategyConfig]:
    return {
        "hodl": StrategyConfig(name=StrategyName.HODL),
        "fixed_20": StrategyConfig(
            name=StrategyName.FIXED_HEDGE, fixed_hedge=FixedHedgeParams(ratio=D("0.20"))
        ),
        "regime_v1": StrategyConfig(
            name=StrategyName.REGIME_HEDGE_V1, regime_hedge=cfg.strategy.regime_hedge
        ),
    }


def _folds(market: LoadedMarket, cfg: AppConfig) -> list[Any]:
    frame = market.dataset.frame
    sp = cfg.backtest.split
    return build_folds(
        frame.index[cfg.backtest.warmup_bars + 1].to_pydatetime(),
        frame.index[-1].to_pydatetime(),
        sp.walk_forward_train_days,
        sp.walk_forward_test_days,
        sp.walk_forward_step_days,
    )


def evaluate(
    market: LoadedMarket, cfg: AppConfig, strategy: Strategy, folds: list[Any]
) -> dict[str, Any]:
    """Full-period metrics + the walk-forward ranking statistic for one strategy instance."""
    full = run_strategy(market, strategy, cfg).summary.to_dict()
    scores, wins = [], 0
    for fold in folds:
        v = run_strategy(
            market, strategy, cfg, start=fold.test_start, end=fold.test_end
        ).summary.to_dict()
        scores.append(score_run(v))
        wins += 1 if float(v["net_btc_growth"]) > 0 else 0
    return {
        "final_btc": float(full["final_btc"]),
        "max_drawdown_usd": float(full["max_drawdown_usd"]),
        "avg_hedge_ratio": float(full.get("avg_hedge_ratio") or 0.0),
        "wf_mean_score": float(np.mean(scores)),
        "wf_folds_positive": wins,
        "wf_folds": len(folds),
    }


def run_controls(
    market: LoadedMarket,
    cfg: AppConfig,
    base: Strategy,
    mean_exposure: float,
    folds: list[Any],
    seeds: int,
    warmup: int,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    # A static strategy emits one constant target, so permuting it in time is the identity —
    # the shuffled control is degenerate there and is skipped rather than reported as p=1.0.
    base_targets = base.compute(market.dataset.frame)["raw_target"].dropna()
    shuffle_is_meaningful = base_targets.nunique() > 1
    for seed in range(seeds):
        controls: list[tuple[str, Strategy]] = [
            ("uniform", UniformRandomControl(mean_exposure, seed, warmup=warmup)),
            ("coin_flip", CoinFlipControl(mean_exposure, seed, warmup=warmup)),
        ]
        if shuffle_is_meaningful:
            controls.insert(0, ("shuffled", ShuffledTimingControl(base, seed)))
        for kind, ctrl in controls:
            rows.append({"control": kind, "seed": seed, **evaluate(market, cfg, ctrl, folds)})
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/backtest.yaml")
    ap.add_argument("--out", default="reports/v4_control_arms")
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--as-of", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    metadata = build_run_metadata(cfg, args.config, run_name="v4_control_arms")
    as_of = datetime.fromisoformat(args.as_of).replace(tzinfo=UTC) if args.as_of else None
    market = load_market(cfg, as_of=as_of)
    folds = _folds(market, cfg)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    alpha_family = sidak_alpha(ALPHA, V3_FAMILY_SIZE)
    observed_rows: list[dict[str, Any]] = []
    verdicts: dict[str, Any] = {}
    all_controls: list[pd.DataFrame] = []

    for name, sc in arms(cfg).items():
        strategy = build_strategy(sc)
        obs = evaluate(market, cfg, strategy, folds)
        observed_rows.append({"arm": name, **obs})
        if name == "hodl":
            verdicts[name] = {"observed": obs, "note": "benchmark; no control needed"}
            continue

        controls = run_controls(
            market,
            cfg,
            strategy,
            obs["avg_hedge_ratio"],
            folds,
            args.seeds,
            cfg.backtest.warmup_bars,
        )
        controls.insert(0, "arm", name)
        all_controls.append(controls)

        per_control: dict[str, Any] = {}
        for kind, sub in controls.groupby("control"):
            entry: dict[str, Any] = {}
            for stat, higher_better in (
                ("final_btc", True),
                ("wf_mean_score", True),
                ("max_drawdown_usd", True),  # less negative is better
            ):
                null = sub[stat].astype(float).tolist()
                p = empirical_p_value(obs[stat], null, higher_is_better=higher_better)
                entry[stat] = {
                    "observed": obs[stat],
                    "null_mean": float(np.mean(null)),
                    "null_p05": float(np.percentile(null, 5)),
                    "null_p95": float(np.percentile(null, 95)),
                    "percentile_rank": percentile_rank(obs[stat], null),
                    "p_value": p,
                    "significant_at_0.05": bool(p <= ALPHA),
                    "significant_after_family_of_18": bool(p <= alpha_family),
                }
            per_control[str(kind)] = entry
        wilson = wilson_interval(obs["wf_folds_positive"], obs["wf_folds"])
        verdicts[name] = {
            "observed": obs,
            "wf_win_rate": wilson.to_dict(),
            "controls": per_control,
            "shuffled_control_applicable": "shuffled" in per_control,
            "note": None
            if "shuffled" in per_control
            else "static target: shuffling in time is the identity, so the timing control does not apply",
        }

    observed = pd.DataFrame(observed_rows)
    observed.to_csv(out / "observed.csv", index=False)
    if all_controls:
        pd.concat(all_controls).to_csv(out / "control_draws.csv", index=False)

    summary = {
        "metadata": metadata.to_dict(),
        "design": {
            "seeds": args.seeds,
            "alpha": ALPHA,
            "family_size": V3_FAMILY_SIZE,
            "alpha_family_sidak": alpha_family,
            "controls": ["shuffled", "uniform", "coin_flip"],
            "statistics": ["final_btc", "wf_mean_score", "max_drawdown_usd"],
            "pre_registered": True,
        },
        "verdicts": verdicts,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    print(f"\nseeds={args.seeds}   alpha={ALPHA}   family-corrected alpha={alpha_family:.5f}\n")
    print("=== observed arms ===")
    print(observed.to_string(index=False))
    for name, v in verdicts.items():
        if "controls" not in v:
            continue
        print(f"\n=== {name} vs controls ===")
        print(
            f"  walk-forward win rate: {wilson_interval(v['observed']['wf_folds_positive'], v['observed']['wf_folds'])}"
        )
        for kind, entry in v["controls"].items():
            print(f"  -- control: {kind}")
            for stat, e in entry.items():
                flag = "SIGNIFICANT" if e["significant_at_0.05"] else "not distinguishable"
                print(
                    f"     {stat:<18} obs={e['observed']:+.4f}  null mean={e['null_mean']:+.4f} "
                    f"[p05 {e['null_p05']:+.4f}, p95 {e['null_p95']:+.4f}]  "
                    f"pct={e['percentile_rank']:5.1f}  p={e['p_value']:.4f}  {flag}"
                )
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
