"""Compare two raw-data snapshots on identical strategies and print the honest delta.

Use this after re-downloading data (e.g. with ``--include-8h-mark``) to see exactly what the new
snapshot changes, *before* trusting any number from it. Nothing is tuned and no parameter is
touched — the same strategies are run twice, once per snapshot.

Usage:
    python scripts/compare_snapshots.py --old data/raw --new data/raw_new
    python scripts/compare_snapshots.py --old data/raw --new data/raw_new --out reports/snapshot_diff
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from btc_inventory_quant.backtest.runner import load_market, run_strategy
from btc_inventory_quant.config.loader import load_config
from btc_inventory_quant.config.schema import (
    AppConfig,
    FixedHedgeParams,
    StrategyConfig,
    StrategyName,
)
from btc_inventory_quant.strategy.benchmarks import build_strategy

D = Decimal
COMPARE = (
    "final_btc",
    "net_btc_growth",
    "cagr_usd",
    "max_drawdown_usd",
    "total_funding_btc",
    "total_fees_btc",
    "total_trades",
    "funding_events_exact_mark",
    "funding_events_kline_8h_mark",
    "funding_events_approximated_mark",
)


def strategies(cfg: AppConfig) -> dict[str, StrategyConfig]:
    return {
        "hodl": StrategyConfig(name=StrategyName.HODL),
        "fixed_50": StrategyConfig(
            name=StrategyName.FIXED_HEDGE, fixed_hedge=FixedHedgeParams(ratio=D("0.5"))
        ),
        "regime_v1": StrategyConfig(
            name=StrategyName.REGIME_HEDGE_V1, regime_hedge=cfg.strategy.regime_hedge
        ),
    }


def run_snapshot(cfg: AppConfig, raw_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    local = cfg.model_copy(update={"data": cfg.data.model_copy(update={"raw_dir": raw_dir})})
    market = load_market(local)
    out = {
        name: run_strategy(market, build_strategy(sc), local).summary.to_dict()
        for name, sc in strategies(local).items()
    }
    meta = {
        "raw_dir": str(raw_dir),
        "bars": len(market.dataset),
        "start": market.dataset.start.isoformat(),
        "end": market.dataset.end.isoformat(),
        "funding_rows": market.quality["funding"]["rows"],
        "settlement_marks": market.quality["settlement_marks"],
    }
    return out, meta


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.10f}" if abs(v) < 1000 else f"{v:,.2f}"
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/backtest.yaml")
    ap.add_argument("--old", required=True, help="baseline raw-data directory")
    ap.add_argument("--new", required=True, help="new raw-data directory")
    ap.add_argument("--out", default=None, help="optional directory for a JSON record")
    args = ap.parse_args()

    cfg = load_config(args.config)
    old, old_meta = run_snapshot(cfg, Path(args.old))
    new, new_meta = run_snapshot(cfg, Path(args.new))

    print("=== snapshots ===")
    for label, meta in (("old", old_meta), ("new", new_meta)):
        print(f"{label}: {meta['raw_dir']}")
        print(
            f"     bars={meta['bars']}  {meta['start'][:10]} -> {meta['end'][:10]}  "
            f"funding_rows={meta['funding_rows']}"
        )
        print(f"     settlement marks: {meta['settlement_marks']}")

    print("\n=== per-strategy delta (new - old) ===")
    rows: list[dict[str, Any]] = []
    for name in old:
        print(f"\n--- {name} ---")
        print(f"{'metric':<34}{'old':>22}{'new':>22}{'delta':>22}")
        for key in COMPARE:
            a, b = old[name].get(key), new[name].get(key)
            if a is None and b is None:
                continue
            if isinstance(a, int | float) and isinstance(b, int | float):
                delta: Any = b - a
                pct = f"  ({(b - a) / a * 100:+.4f}%)" if a else ""
            else:
                delta, pct = ("same" if a == b else "CHANGED"), ""
            print(f"{key:<34}{_fmt(a):>22}{_fmt(b):>22}{_fmt(delta):>22}{pct}")
            rows.append({"strategy": name, "metric": key, "old": a, "new": b, "delta": delta})

    verdicts = {n: (old[n].get("verdict"), new[n].get("verdict")) for n in old}
    flipped = {n: v for n, v in verdicts.items() if v[0] != v[1]}
    print("\n=== verdicts ===")
    for n, (a, b) in verdicts.items():
        print(f"{n:<16} {a} -> {b}{'   *** CHANGED ***' if a != b else ''}")
    print(
        "\nNo verdict changed — the new snapshot does not alter any conclusion."
        if not flipped
        else f"\n*** {len(flipped)} verdict(s) changed: {list(flipped)} — re-read the reports before trusting them. ***"
    )

    if args.out:
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "snapshot_diff.json").write_text(
            json.dumps(
                {"old": old_meta, "new": new_meta, "rows": rows, "verdicts": verdicts},
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"\nwritten to {outdir / 'snapshot_diff.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
