from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise

import numpy as np
import pandas as pd

from btc_inventory_quant.backtest.walkforward import (
    DEFAULT_GRID,
    build_folds,
    grid_combos,
    neighbourhood_scores,
    score_run,
)

D = Decimal


def test_grid_is_small_and_deterministic() -> None:
    combos = grid_combos()
    assert len(combos) == 72
    assert combos == grid_combos()
    assert len(grid_combos(max_grid=5)) == 5
    assert set(combos[0]) == set(DEFAULT_GRID)


def test_folds_are_chronological_and_non_overlapping_tests() -> None:
    start = datetime(2021, 3, 10, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    folds = build_folds(start, end, train_days=730, test_days=180, step_days=180)
    assert len(folds) >= 5
    for a, b in pairwise(folds):
        assert a.train_start < a.train_end < a.test_start <= a.test_end
        assert b.test_start > a.test_end  # test windows never overlap
        assert b.train_start > a.train_start
    # the test window always follows the train window (no leakage of future data into training)
    assert all(f.test_start > f.train_end for f in folds)
    assert folds[-1].test_end <= end


def test_score_prefers_btc_growth_and_usd_excess_and_penalises_worse_drawdown() -> None:
    base = {
        "cagr_btc": 0.05,
        "cagr_usd": 0.10,
        "hodl_cagr_usd": 0.08,
        "max_drawdown_usd": -0.3,
        "hodl_max_drawdown_usd": -0.5,
    }
    s0 = score_run(base)
    assert s0 == np.isclose(s0, 0.5 * 0.05 + 0.5 * 0.02) or np.isclose(s0, 0.5 * 0.05 + 0.5 * 0.02)
    worse_dd = dict(base, max_drawdown_usd=-0.6)
    assert score_run(worse_dd) < s0
    more_btc = dict(base, cagr_btc=0.10)
    assert score_run(more_btc) > s0
    assert score_run({"cagr_btc": float("nan")}) == float("-inf")


def test_neighbourhood_scores_average_adjacent_combos() -> None:
    grid = {"a": [1, 2, 3], "b": ["x", "y"]}
    rows = []
    for a in grid["a"]:
        for b in grid["b"]:
            rows.append({"a": str(a), "b": b, "score": float(a) + (0.5 if b == "y" else 0.0)})
    df = pd.DataFrame(rows)
    ns = neighbourhood_scores(df, grid)
    # combo (a=2, b=x): neighbours a=1/x (1.0), a=3/x (3.0), a=2/y (2.5) → mean 2.1667
    idx = df[(df["a"] == "2") & (df["b"] == "x")].index[0]
    assert np.isclose(ns.loc[idx], (1.0 + 3.0 + 2.5) / 3)
    # corner (a=1, b=x): neighbours a=2/x (2.0), a=1/y (1.5)
    idx0 = df[(df["a"] == "1") & (df["b"] == "x")].index[0]
    assert np.isclose(ns.loc[idx0], (2.0 + 1.5) / 2)
