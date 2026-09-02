"""CLI-level integration tests on the committed real-data snapshot (skipped if absent)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from btc_inventory_quant.cli.main import main

ROOT = Path(__file__).parents[2]
RAW = ROOT / "data" / "raw"
AS_OF = "2026-09-02T05:10:00"
pytestmark = pytest.mark.skipif(
    not (RAW / "manifest.json").exists(), reason="raw snapshot not present"
)


def _config(tmp_path: Path, name: str = "bt.yaml", **overrides: object) -> Path:
    cfg = yaml.safe_load((ROOT / "configs" / "backtest.yaml").read_text(encoding="utf-8"))
    cfg["data"]["raw_dir"] = str(RAW)
    cfg["data"]["exchange_info_path"] = str(RAW / "exchange_info.json")
    cfg["backtest"]["end"] = "2022-12-31"  # short window keeps the test fast
    cfg["backtest"]["sensitivity"] = False
    cfg["backtest"]["report_dir"] = str(tmp_path / "reports")
    for k, v in overrides.items():
        cfg["backtest"][k] = v
    p = tmp_path / name
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


REQUIRED = [
    "summary.json",
    "trades.csv",
    "daily_equity.csv",
    "monthly_returns.csv",
    "yearly_returns.csv",
    "funding_history.csv",
    "fee_history.csv",
    "hedge_ratio_history.csv",
    "benchmark_comparison.csv",
    "drawdown.csv",
    "backtest_report.md",
]


def test_cli_backtest_writes_all_outputs_and_is_reproducible(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    assert (
        main(
            [
                "backtest",
                "--config",
                str(cfg),
                "--as-of",
                AS_OF,
                "--out",
                str(out1),
                "--no-sensitivity",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "backtest",
                "--config",
                str(cfg),
                "--as-of",
                AS_OF,
                "--out",
                str(out2),
                "--no-sensitivity",
            ]
        )
        == 0
    )
    for name in REQUIRED:
        assert (out1 / name).exists(), name
    assert len(list((out1 / "charts").glob("*.png"))) == 9
    s1 = json.loads((out1 / "summary.json").read_text(encoding="utf-8"))
    s2 = json.loads((out2 / "summary.json").read_text(encoding="utf-8"))
    for key in (
        "final_btc",
        "final_usd",
        "max_drawdown_usd",
        "total_trades",
        "total_funding_btc",
        "total_fees_btc",
    ):
        assert s1["main"][key] == s2["main"][key], key
    assert (out1 / "trades.csv").read_bytes() == (out2 / "trades.csv").read_bytes()
    assert (out1 / "daily_equity.csv").read_bytes() == (out2 / "daily_equity.csv").read_bytes()
    assert set(s1["benchmarks"]) == {"hodl", "fixed_hedge_100", "fixed_hedge_50", "ema200_simple"}
    assert s1["main"]["end_date"] == "2022-12-31"
    assert s1["metadata"]["config_sha256"] == s2["metadata"]["config_sha256"]


def test_cli_backtest_costs_reduce_final_btc_on_real_data(tmp_path: Path) -> None:
    base_cfg = _config(tmp_path, "bt_base.yaml")
    expensive_path = _config(
        tmp_path, "bt_expensive.yaml", cost={"taker_fee_rate": "0.0010", "slippage_bps": "15"}
    )
    out_base = tmp_path / "base"
    out_exp = tmp_path / "expensive"
    assert (
        main(
            [
                "backtest",
                "--config",
                str(base_cfg),
                "--as-of",
                AS_OF,
                "--out",
                str(out_base),
                "--no-sensitivity",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "backtest",
                "--config",
                str(expensive_path),
                "--as-of",
                AS_OF,
                "--out",
                str(out_exp),
                "--no-sensitivity",
            ]
        )
        == 0
    )
    b = json.loads((out_base / "summary.json").read_text(encoding="utf-8"))["main"]
    e = json.loads((out_exp / "summary.json").read_text(encoding="utf-8"))["main"]
    assert e["final_btc"] < b["final_btc"]
    assert e["total_fees_btc"] < b["total_fees_btc"]  # more negative


def test_cli_data_quality_and_verify() -> None:
    assert main(["data", "--raw-dir", str(RAW), "verify"]) == 0
    assert main(["data", "verify", "--raw-dir", str(RAW)]) == 0
    assert main(["data", "--raw-dir", str(RAW), "--as-of", AS_OF, "quality"]) == 0
