from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from btc_inventory_quant.backtest.metrics import summarize
from btc_inventory_quant.backtest.reports import StrategyRun, comparison_frame, write_bundle
from btc_inventory_quant.cli.main import build_parser, main
from btc_inventory_quant.config.schema import FixedHedgeParams, RegimeHedgeParams
from btc_inventory_quant.monitoring.logging import mask_mapping, mask_text
from btc_inventory_quant.strategy.benchmarks import FixedHedgeStrategy, HodlStrategy
from btc_inventory_quant.strategy.regime_hedge import RegimeHedgeV1
from tests.helpers import make_dataset, synthetic_prices
from tests.unit.test_backtest_engine import _engine

D = Decimal
EXPECTED_FILES = [
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
EXPECTED_CHARTS = [
    "btc_equity.png",
    "usd_equity.png",
    "usd_equity_vs_hodl.png",
    "cumulative_btc_growth.png",
    "hedge_ratio.png",
    "price_and_hedge_ratio.png",
    "drawdown.png",
    "cumulative_fees.png",
    "cumulative_funding.png",
]


def _run(ds, strat):  # type: ignore[no-untyped-def]
    res = _engine(ds, strat, warmup=60).run()
    summary, frames = summarize(res, D("1"))
    return StrategyRun(result=res, summary=summary, frames=frames)


def test_report_bundle_writes_every_required_file(tmp_path: Path) -> None:
    ds = make_dataset(synthetic_prices(420, seed=21))
    main_run = _run(
        ds, RegimeHedgeV1(RegimeHedgeParams(ema_slow=50, ema_mid=20, ema_fast=10, atr_period=10))
    )
    benches = [
        _run(ds, HodlStrategy()),
        _run(ds, FixedHedgeStrategy(FixedHedgeParams(ratio=D("0.5")))),
    ]
    files = write_bundle(
        tmp_path,
        main_run,
        benches,
        {"run_id": "t", "seed": 1},
        {"risk": {}, "backtest": {"cost": {}}},
        {"market": {}},
        notes=["n"],
    )
    for f in EXPECTED_FILES:
        assert (tmp_path / f).exists(), f
    for c in EXPECTED_CHARTS:
        assert (tmp_path / "charts" / c).exists(), c
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["main"]["strategy"] == "regime_hedge_v1"
    assert set(summary["benchmarks"]) == {"hodl", "fixed_hedge_50"}
    md = (tmp_path / "backtest_report.md").read_text(encoding="utf-8")
    assert "HODL 대비" in md
    assert "fixed_hedge_50" in md
    comp = comparison_frame([main_run, *benches])
    assert list(comp["strategy"]) == ["regime_hedge_v1", "hodl", "fixed_hedge_50"]
    assert "backtest_report.md" in files


def test_cli_parser_exposes_all_commands() -> None:
    parser = build_parser()
    for argv in (
        ["backtest", "--config", "x.yaml"],
        ["research", "--config", "x.yaml", "walk-forward"],
        ["data", "verify"],
        ["shadow", "--config", "x.yaml"],
        ["testnet", "--config", "x.yaml", "--once"],
        ["live", "--config", "x.yaml", "--confirm-live"],
        ["status"],
        ["reconcile"],
        ["safe-hold"],
    ):
        args = parser.parse_args(argv)
        assert args.command == argv[0]
    with pytest.raises(SystemExit):
        parser.parse_args(["unknown"])


def test_cli_data_verify_on_real_snapshot() -> None:
    raw = Path(__file__).parents[2] / "data" / "raw"
    if not (raw / "manifest.json").exists():
        pytest.skip("raw snapshot not present")
    assert main(["data", "--raw-dir", str(raw), "verify"]) == 0


def test_logging_masks_secrets() -> None:
    masked = mask_mapping(
        {
            "apiKey": "ABCDEFGHIJKLMNOP",
            "X-MBX-APIKEY": "ABCDEFGHIJKLMNOP",
            "qty": 3,
            "nested": {"secret": "s3cr3tvalue"},
        }
    )
    assert masked["apiKey"] == "ABC…OP"
    assert masked["X-MBX-APIKEY"] == "ABC…OP"
    assert masked["qty"] == 3
    assert masked["nested"]["secret"] == "s3c…ue"
    text = mask_text(
        "GET /dapi/v1/order?symbol=BTCUSD_PERP&timestamp=1&signature=0123456789abcdef0123 X-MBX-APIKEY: ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ABCD"
    )
    assert "0123456789abcdef0123" not in text
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ABCD" not in text
