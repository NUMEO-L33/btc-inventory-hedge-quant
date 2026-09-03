"""Command line interface.

python -m btc_inventory_quant backtest --config configs/backtest.yaml
python -m btc_inventory_quant research walk-forward --config configs/backtest.yaml
python -m btc_inventory_quant research sensitivity --config configs/backtest.yaml
python -m btc_inventory_quant data --raw-dir data/raw download
python -m btc_inventory_quant data --raw-dir data/raw verify
python -m btc_inventory_quant shadow --config configs/shadow.yaml
python -m btc_inventory_quant testnet --config configs/testnet.yaml
python -m btc_inventory_quant live --config configs/live.yaml --confirm-live
python -m btc_inventory_quant status | reconcile | safe-hold
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from btc_inventory_quant import __version__
from btc_inventory_quant.config.loader import build_run_metadata, load_config
from btc_inventory_quant.config.schema import RunMode
from btc_inventory_quant.monitoring.logging import configure_logging, get_logger

log = get_logger(__name__)


def _parse_as_of(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def cmd_backtest(args: argparse.Namespace) -> int:
    from btc_inventory_quant.backtest.runner import run_backtest
    from btc_inventory_quant.backtest.scenarios import run_sensitivity

    config = load_config(args.config)
    if config.mode is not RunMode.BACKTEST:
        log.warning("config mode is %s; forcing backtest", config.mode)
    metadata = build_run_metadata(config, args.config)
    sens = None
    if config.backtest.sensitivity and not args.no_sensitivity:
        sens = run_sensitivity(config, as_of=_parse_as_of(args.as_of))
    out, main, benchmarks = run_backtest(
        config,
        metadata,
        out_dir=Path(args.out) if args.out else None,
        sensitivity=sens,
        as_of=_parse_as_of(args.as_of),
    )
    v = main.summary.to_dict()
    print(f"run_id={metadata.run_id}")
    print(f"report: {out / 'backtest_report.md'}")
    print(
        f"{main.name}: final_btc={v['final_btc']:.4f} cagr_btc={v['cagr_btc']:+.2%} final_usd={v['final_usd']:,.0f} "
        f"cagr_usd={v['cagr_usd']:+.2%} mdd_usd={v['max_drawdown_usd']:.1%} vs_hodl={v['excess_vs_hodl_usd_pct']:+.1%} verdict={v['verdict']}"
    )
    for b in benchmarks:
        bv = b.summary.to_dict()
        print(
            f"  {b.name}: final_btc={bv['final_btc']:.4f} final_usd={bv['final_usd']:,.0f} mdd_usd={bv['max_drawdown_usd']:.1%} verdict={bv['verdict']}"
        )
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    from btc_inventory_quant.backtest.walkforward import run_walk_forward_study

    if not args.config:
        print("error: --config is required (e.g. --config configs/backtest.yaml)", file=sys.stderr)
        return 2
    config = load_config(args.config)
    metadata = build_run_metadata(config, args.config, run_name="research")
    if args.research_cmd == "walk-forward":
        out = run_walk_forward_study(
            config,
            metadata,
            out_dir=Path(args.out) if args.out else None,
            as_of=_parse_as_of(args.as_of),
            max_grid=args.max_grid,
        )
        print(f"walk-forward study written to {out}")
        return 0
    if args.research_cmd == "sensitivity":
        from btc_inventory_quant.backtest.scenarios import run_sensitivity

        df = run_sensitivity(config, as_of=_parse_as_of(args.as_of))
        print(df.to_string(index=False))
        return 0
    return 2


def cmd_data(args: argparse.Namespace) -> int:
    from btc_inventory_quant.data.downloader import BinanceCoinMPublicDownloader, verify_manifest

    raw = Path(args.raw_dir)
    if args.data_cmd == "download":
        dl = BinanceCoinMPublicDownloader(
            raw, symbol=args.symbol, pair=args.pair, base_url=args.base_url
        )
        manifest = dl.download_all(include_8h_mark=args.include_8h_mark)
        for name, ds in manifest["datasets"].items():
            print(
                f"{name}: rows={ds['total_rows']} {ds['first_date']} -> {ds['last_date']} pages={len(ds['pages'])}"
            )
        return 0
    if args.data_cmd == "verify":
        problems = verify_manifest(raw)
        if problems:
            for p in problems:
                print(p)
            return 1
        print("manifest OK: all pages match their SHA-256")
        return 0
    if args.data_cmd == "quality":
        from btc_inventory_quant.backtest.runner import load_market
        from btc_inventory_quant.config.schema import AppConfig

        market = load_market(AppConfig(), as_of=_parse_as_of(args.as_of))
        print(json.dumps(market.quality, indent=2, ensure_ascii=False))
        return 0
    return 2


def cmd_run_mode(args: argparse.Namespace) -> int:
    from btc_inventory_quant.runtime.app import run_mode

    return int(
        run_mode(
            mode=RunMode(args.command),
            config_path=args.config,
            confirm_live=getattr(args, "confirm_live", False),
            once=getattr(args, "once", False),
            max_cycles=getattr(args, "max_cycles", None),
        )
    )


def cmd_ops(args: argparse.Namespace) -> int:
    from btc_inventory_quant.runtime.app import ops_command

    return int(
        ops_command(
            args.command,
            config_path=args.config,
            release=getattr(args, "release", False),
            ack=getattr(args, "ack", "") or "",
            window_hours=getattr(args, "window_hours", 48),
            contracts=getattr(args, "contracts", "") or "",
            allow_production_rates=getattr(args, "allow_production_rates", False),
            snapshot_rate=getattr(args, "snapshot_rate", False),
        )
    )


def _opt_kwargs(suppress: bool, **kwargs: Any) -> dict[str, Any]:
    """Options are accepted both before and after the sub-command.

    The sub-parser copy uses ``argparse.SUPPRESS`` as its default so that, when the flag is not
    repeated there, argparse leaves the attribute unset and the value parsed by the parent
    survives (see CPython's ``_SubParsersAction.__call__``).
    """
    if suppress:
        kwargs["default"] = argparse.SUPPRESS
        kwargs.pop("required", None)
    return kwargs


def _add_research_options(p: argparse.ArgumentParser, suppress: bool = False) -> None:
    # not argparse-required: it may be given after the sub-command, so it is checked in cmd_research
    p.add_argument("--config", **_opt_kwargs(suppress, default=None))
    p.add_argument("--out", **_opt_kwargs(suppress, default=None))
    p.add_argument("--as-of", **_opt_kwargs(suppress, default=None))
    p.add_argument("--max-grid", type=int, **_opt_kwargs(suppress, default=None))


def _add_data_options(p: argparse.ArgumentParser, suppress: bool = False) -> None:
    p.add_argument("--raw-dir", **_opt_kwargs(suppress, default="data/raw"))
    p.add_argument("--symbol", **_opt_kwargs(suppress, default="BTCUSD_PERP"))
    p.add_argument("--pair", **_opt_kwargs(suppress, default="BTCUSD"))
    p.add_argument("--base-url", **_opt_kwargs(suppress, default="https://dapi.binance.com"))
    p.add_argument("--include-8h-mark", action="store_true", **_opt_kwargs(suppress, default=False))
    p.add_argument("--as-of", **_opt_kwargs(suppress, default=None))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="btc_inventory_quant", description="BTC Inventory Hedge Quant")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--log-level", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="run a backtest with benchmarks and reports")
    bt.add_argument("--config", required=True)
    bt.add_argument("--out", default=None)
    bt.add_argument(
        "--as-of", default=None, help="ISO datetime; candles closing after this are ignored"
    )
    bt.add_argument("--no-sensitivity", action="store_true")
    bt.set_defaults(func=cmd_backtest)

    rs = sub.add_parser("research", help="walk-forward / sensitivity studies")
    _add_research_options(rs)
    rs_sub = rs.add_subparsers(dest="research_cmd", required=True)
    for name in ("walk-forward", "sensitivity"):
        _add_research_options(rs_sub.add_parser(name), suppress=True)
    rs.set_defaults(func=cmd_research)

    dt = sub.add_parser("data", help="download / verify public market data")
    _add_data_options(dt)
    dt_sub = dt.add_subparsers(dest="data_cmd", required=True)
    for name in ("download", "verify", "quality"):
        _add_data_options(dt_sub.add_parser(name), suppress=True)
    dt.set_defaults(func=cmd_data)

    for mode in ("shadow", "testnet", "live"):
        m = sub.add_parser(mode, help=f"run the trading loop in {mode} mode")
        m.add_argument("--config", required=True)
        m.add_argument("--once", action="store_true", help="run a single decision cycle and exit")
        m.add_argument("--max-cycles", type=int, default=None)
        if mode == "live":
            m.add_argument(
                "--confirm-live",
                action="store_true",
                help="required (together with env + config gates) to send real orders",
            )
        m.set_defaults(func=cmd_run_mode)

    for op in ("status", "reconcile", "safe-hold", "verify-funding"):
        o = sub.add_parser(op)
        o.add_argument("--config", default="configs/shadow.yaml")
        if op == "verify-funding":
            o.add_argument(
                "--window-hours",
                type=int,
                default=48,
                help="how far back to look for settled FUNDING_FEE records",
            )
            o.add_argument(
                "--contracts",
                default="",
                help="position size held during the window (signed; negative = short). "
                "Omit to read the current exchange position, which assumes it never changed.",
            )
            o.add_argument(
                "--snapshot-rate",
                action="store_true",
                help="capture the rate THIS environment will apply at the next settlement "
                "(premiumIndex) and store it for a later verify-funding run. Run it within ~10 "
                "minutes of 00:00/08:00/16:00 UTC. Needed where the environment publishes no "
                "funding history, as the demo host does not.",
            )
            o.add_argument(
                "--allow-production-rates",
                action="store_true",
                help="if the configured environment publishes no funding history (the demo host "
                "does not), take the public rates from production. Only sound while both price "
                "the instrument alike, which the report measures and states.",
            )
        if op == "safe-hold":
            o.add_argument(
                "--release", action="store_true", help="release SAFE_HOLD (requires --ack)"
            )
            o.add_argument(
                "--ack",
                default="",
                help="acknowledgement string (must equal live.expected_acknowledgement)",
            )
        o.set_defaults(func=cmd_ops)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(level=args.log_level)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
