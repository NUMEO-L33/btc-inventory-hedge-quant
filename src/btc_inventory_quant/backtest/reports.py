"""Report bundle: CSV/JSON files, Markdown report and PNG charts for a backtest run.

Nothing is ever omitted because it looks bad: every strategy, every period and every chart is
written regardless of the outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, PercentFormatter

from btc_inventory_quant.backtest.engine import BacktestResult
from btc_inventory_quant.backtest.metrics import PerformanceSummary

# Fixed categorical order (validated palette, light mode). Assigned by entity, never cycled.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e5e4df"
SURFACE = "#fcfcfb"


@dataclass(slots=True)
class StrategyRun:
    result: BacktestResult
    summary: PerformanceSummary
    frames: dict[str, pd.DataFrame]

    @property
    def name(self) -> str:
        return self.result.strategy_name


COMPARISON_COLUMNS = [
    "strategy",
    "start_btc",
    "final_btc",
    "net_btc_growth",
    "cagr_btc",
    "start_usd",
    "final_usd",
    "cagr_usd",
    "hodl_final_usd",
    "excess_vs_hodl_usd_pct",
    "max_drawdown_usd",
    "max_drawdown_btc",
    "hodl_max_drawdown_usd",
    "sharpe_usd",
    "total_realized_pnl_btc",
    "total_unrealized_pnl_btc",
    "total_funding_btc",
    "total_fees_btc",
    "total_slippage_cost_btc",
    "total_trades",
    "turnover_total",
    "avg_hedge_ratio",
    "max_hedge_ratio",
    "worst_consecutive_losing_days_usd",
    "worst_consecutive_losing_closes",
    "verdict",
]


def _style_axes(ax: Any, ylabel: str = "", pct: bool = False) -> None:
    ax.set_facecolor(SURFACE)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID)
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.tick_params(colors=INK_2, labelsize=9)
    ax.set_ylabel(ylabel, color=INK_2, fontsize=9)
    if pct:
        ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    else:
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _: f"{v:,.2f}" if abs(v) < 100 else f"{v:,.0f}")
        )


def _figure(title: str, nrows: int = 1, height: float = 4.2) -> tuple[Any, Any]:
    fig, axes = plt.subplots(
        nrows, 1, figsize=(11, height * nrows), sharex=(nrows > 1), facecolor=SURFACE
    )
    fig.suptitle(title, color=INK, fontsize=12, x=0.01, ha="left")
    return fig, axes


def _save(fig: Any, path: Path) -> None:
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path, dpi=130, facecolor=SURFACE)
    plt.close(fig)


def _direct_labels(
    ax: Any, items: list[tuple[pd.Series, str, str]], min_gap_frac: float = 0.05
) -> None:
    """Label each series at its last point; push labels apart vertically so they never overlap."""
    if not items:
        return
    ymin, ymax = ax.get_ylim()
    gap = (ymax - ymin) * min_gap_frac
    ordered = sorted(items, key=lambda it: float(it[0].iloc[-1]))
    placed: list[float] = []
    for series, label, color in ordered:
        y = float(series.iloc[-1])
        if placed and y - placed[-1] < gap:
            y = placed[-1] + gap
        placed.append(y)
        ax.annotate(
            label,
            xy=(series.index[-1], float(series.iloc[-1])),
            xytext=(6, (y - float(series.iloc[-1])) / (ymax - ymin) * ax.bbox.height),
            textcoords="offset points",
            color=color,
            fontsize=8,
            va="center",
        )


def write_charts(main: StrategyRun, benchmarks: list[StrategyRun], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    runs = [main, *benchmarks]
    d = main.result.daily

    # 1. BTC equity curves
    fig, ax = _figure("BTC equity by strategy")
    labels: list[tuple[pd.Series, str, str]] = []
    for i, run in enumerate(runs):
        s = run.result.daily["equity_btc"].astype(float)
        ax.plot(
            s.index, s.values, color=SERIES[i], linewidth=2 if run is main else 1.2, label=run.name
        )
        labels.append((s, run.name, SERIES[i]))
    _style_axes(ax, "BTC")
    _direct_labels(ax, labels)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    p = out_dir / "btc_equity.png"
    _save(fig, p)
    written.append(p)

    # 2. USD equity curves
    fig, ax = _figure("USD equity by strategy")
    for i, run in enumerate(runs):
        s = run.result.daily["equity_usd"].astype(float)
        ax.plot(
            s.index, s.values, color=SERIES[i], linewidth=2 if run is main else 1.2, label=run.name
        )
    hodl = d["hodl_usd"].astype(float)
    ax.plot(hodl.index, hodl.values, color=INK_2, linewidth=1, linestyle="--", label="HODL (1 BTC)")
    _style_axes(ax, "USD")
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    p = out_dir / "usd_equity.png"
    _save(fig, p)
    written.append(p)

    # 3. USD equity relative to HODL (indexed)
    fig, ax = _figure("USD equity relative to HODL")
    labels = []
    for i, run in enumerate(runs):
        rd = run.result.daily
        rel = rd["equity_usd"].astype(float) / rd["hodl_usd"].astype(float)
        ax.plot(
            rel.index,
            rel.values,
            color=SERIES[i],
            linewidth=2 if run is main else 1.2,
            label=run.name,
        )
        labels.append((rel, run.name, SERIES[i]))
    ax.axhline(1.0, color=INK_2, linewidth=1, linestyle="--")
    _style_axes(ax, "multiple")
    _direct_labels(ax, labels)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    p = out_dir / "usd_equity_vs_hodl.png"
    _save(fig, p)
    written.append(p)

    # 4. cumulative BTC growth (equity_btc − initial)
    fig, ax = _figure("Cumulative BTC growth")
    for i, run in enumerate(runs):
        rd = run.result.daily
        g = rd["equity_btc"].astype(float) - rd["hodl_btc"].astype(float)
        ax.plot(
            g.index, g.values, color=SERIES[i], linewidth=2 if run is main else 1.2, label=run.name
        )
    ax.axhline(0.0, color=INK_2, linewidth=1, linestyle="--")
    _style_axes(ax, "BTC")
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    p = out_dir / "cumulative_btc_growth.png"
    _save(fig, p)
    written.append(p)

    # 5. hedge ratio history (main)
    fig, ax = _figure(f"Hedge ratio — {main.name}")
    eff = d["effective_ratio"].astype(float)
    tgt = d["decision_target"].astype(float)
    ax.plot(
        eff.index,
        eff.values,
        color=SERIES[0],
        linewidth=1.6,
        label="effective hedge ratio",
    )
    ax.plot(
        tgt.index,
        tgt.values,
        color=SERIES[1],
        linewidth=1,
        linestyle=":",
        label="capped target",
    )
    _style_axes(ax, "hedge ratio", pct=True)
    ax.set_ylim(0, 1)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    p = out_dir / "hedge_ratio.png"
    _save(fig, p)
    written.append(p)

    # 6. price and hedge ratio (two panels, one axis each — never dual-axis)
    fig, (ax1, ax2) = _figure("BTC mark price and hedge ratio", nrows=2, height=3.2)
    price = d["mark_close"].astype(float)
    ax1.plot(price.index, price.values, color=INK, linewidth=1.2)
    regimes = d["regime"].astype(str)
    colors = {"bull": SERIES[2], "neutral": SERIES[3], "bear": SERIES[1]}
    for regime, color in colors.items():
        mask = regimes == regime
        if mask.any():
            ax1.fill_between(
                price.index,
                price.min(),
                price.max(),
                where=mask.to_numpy(),
                color=color,
                alpha=0.12,
                label=f"{regime} regime",
            )
    _style_axes(ax1, "USD")
    ax1.set_yscale("log")
    ax1.legend(frameon=False, fontsize=8, loc="upper left")
    ax2.plot(eff.index, eff.values, color=SERIES[0], linewidth=1.6)
    _style_axes(ax2, "hedge ratio", pct=True)
    ax2.set_ylim(0, 1)
    p = out_dir / "price_and_hedge_ratio.png"
    _save(fig, p)
    written.append(p)

    # 7. drawdown
    fig, (ax1, ax2) = _figure("Drawdown (USD / BTC)", nrows=2, height=3.0)
    dd = main.frames["drawdown"]
    ax1.fill_between(
        dd.index,
        dd["drawdown_usd"].values,
        0,
        color=SERIES[0],
        alpha=0.35,
        label=f"{main.name} USD",
    )
    ax1.plot(
        dd.index, dd["drawdown_hodl_usd"].values, color=SERIES[1], linewidth=1, label="HODL USD"
    )
    _style_axes(ax1, "USD DD", pct=True)
    ax1.legend(frameon=False, fontsize=8, loc="lower left")
    ax2.fill_between(
        dd.index,
        dd["drawdown_btc"].values,
        0,
        color=SERIES[2],
        alpha=0.35,
        label=f"{main.name} BTC",
    )
    _style_axes(ax2, "BTC DD", pct=True)
    ax2.legend(frameon=False, fontsize=8, loc="lower left")
    p = out_dir / "drawdown.png"
    _save(fig, p)
    written.append(p)

    # 8. cumulative fees, 9. cumulative funding
    for key, title, fname, _col in (
        (
            "fees_btc_today",
            "Cumulative trading fees (BTC)",
            "cumulative_fees.png",
            0,
        ),
        (
            "funding_btc_today",
            "Cumulative funding (BTC, + = received)",
            "cumulative_funding.png",
            0,
        ),
    ):
        fig, ax = _figure(title)
        for i, run in enumerate(runs):
            s = run.result.daily[key].astype(float).cumsum()
            if key == "fees_btc_today":
                s = -s
            ax.plot(
                s.index,
                s.values,
                color=SERIES[i],
                linewidth=2 if run is main else 1.2,
                label=run.name,
            )
        ax.axhline(0.0, color=INK_2, linewidth=1, linestyle="--")
        _style_axes(ax, "BTC")
        ax.legend(frameon=False, fontsize=8, loc="upper left")
        p = out_dir / fname
        _save(fig, p)
        written.append(p)
    return written


def _fmt_pct(v: Any) -> str:
    try:
        return f"{float(v):+.2%}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt(v: Any, nd: int = 4) -> str:
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return "n/a"


def comparison_frame(runs: list[StrategyRun]) -> pd.DataFrame:
    rows = []
    for run in runs:
        v = run.summary.to_dict()
        rows.append({c: v.get(c) for c in COMPARISON_COLUMNS})
    return pd.DataFrame(rows)


def write_markdown_report(
    path: Path,
    main: StrategyRun,
    benchmarks: list[StrategyRun],
    metadata: dict[str, Any],
    config_dump: dict[str, Any],
    data_quality: dict[str, Any],
    sensitivity: pd.DataFrame | None,
    notes: list[str],
) -> None:
    v = main.summary.to_dict()
    runs = [main, *benchmarks]
    lines: list[str] = []
    lines.append(f"# Backtest report — {main.name}")
    lines.append("")
    lines.append(
        f"- Run ID: `{metadata.get('run_id')}` / run timestamp (UTC): {metadata.get('started_at_utc')}"
    )
    lines.append(
        f"- Code version: {metadata.get('code_version')} / git: `{metadata.get('git_commit')}` / config hash: `{metadata.get('config_sha256')}` / seed: {metadata.get('seed')}"
    )
    lines.append(
        f"- Period: {v['start_date']} → {v['end_date']} ({v['days']} days, {v['years']:.2f} years) / starting BTC: {v['start_btc']}"
    )
    lines.append("")
    lines.append("## 1. Key results (evaluated in BTC terms and USD terms simultaneously)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    for label, key, f in (
        ("Final BTC", "final_btc", _fmt),
        ("Net BTC growth", "net_btc_growth", _fmt),
        ("CAGR in BTC terms", "cagr_btc", _fmt_pct),
        ("Starting USD", "start_usd", lambda x: _fmt(x, 0)),
        ("Final USD", "final_usd", lambda x: _fmt(x, 0)),
        ("CAGR in USD terms", "cagr_usd", _fmt_pct),
        ("HODL final USD", "hodl_final_usd", lambda x: _fmt(x, 0)),
        ("USD excess return vs HODL", "excess_vs_hodl_usd_pct", _fmt_pct),
        ("USD maximum drawdown", "max_drawdown_usd", _fmt_pct),
        ("HODL USD maximum drawdown", "hodl_max_drawdown_usd", _fmt_pct),
        ("BTC maximum drawdown", "max_drawdown_btc", _fmt_pct),
        ("USD Sharpe (daily, rf=0)", "sharpe_usd", lambda x: _fmt(x, 2)),
        ("Total realized PnL (BTC)", "total_realized_pnl_btc", _fmt),
        ("Total unrealized PnL (BTC)", "total_unrealized_pnl_btc", _fmt),
        ("Total funding (BTC, + = received)", "total_funding_btc", _fmt),
        ("Total trading fees (BTC)", "total_fees_btc", _fmt),
        ("Total slippage cost (BTC)", "total_slippage_cost_btc", _fmt),
        ("Total number of trades", "total_trades", str),
        ("Total turnover (notional / average equity)", "turnover_total", lambda x: _fmt(x, 2)),
        ("Average hedge ratio", "avg_hedge_ratio", _fmt_pct),
        ("Maximum hedge ratio", "max_hedge_ratio", _fmt_pct),
        ("Worst consecutive losing days (USD)", "worst_consecutive_losing_days_usd", str),
        ("Return over the worst losing streak (USD)", "worst_losing_run_usd_pct", _fmt_pct),
        ("Worst consecutive losing closing trades", "worst_consecutive_losing_closes", str),
        ("Verdict", "verdict", str),
    ):
        lines.append(f"| {label} | {f(v.get(key))} |")
    lines.append("")
    lines.append(
        "### BTC PnL decomposition (final BTC − starting BTC = realized + unrealized + funding + fees)"
    )
    lines.append("")
    lines.append("| Component | BTC |")
    lines.append("|---|---|")
    trading = float(v.get("total_realized_pnl_btc") or 0) + float(
        v.get("total_unrealized_pnl_btc") or 0
    )
    lines.append(f"| Hedge timing PnL (realized + unrealized, before fees) | {trading:+.4f} |")
    lines.append(
        f"| Funding received (+) / paid (−) | {float(v.get('total_funding_btc') or 0):+.4f} |"
    )
    lines.append(f"| Trading fees | {float(v.get('total_fees_btc') or 0):+.4f} |")
    lines.append(
        f"| Slippage (already reflected in fill prices; for reference) | {-float(v.get('total_slippage_cost_btc') or 0):+.4f} |"
    )
    lines.append(f"| Net BTC growth | {float(v.get('net_btc_growth') or 0):+.4f} |")
    lines.append("")
    lines.append(
        "Verdict criteria: FAIL if the BTC count did not grow, the USD value trails HODL by 30% or more, or the USD drawdown is worse than HODL's; WEAK if USD trails HODL by 10% or more; PASS otherwise. If only BTC grows while USD falls sharply, that counts as a failure."
    )
    lines.append("")
    lines.append("## 2. Benchmark strategies")
    lines.append("")
    lines.append(
        "| Strategy | Final BTC | BTC CAGR | Final USD | USD CAGR | USD vs HODL | USD MDD | BTC MDD | Funding (BTC) | Fees (BTC) | Slippage (BTC) | Trades | Avg hedge ratio | Verdict |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for run in runs:
        r = run.summary.to_dict()
        lines.append(
            f"| {run.name} | {_fmt(r['final_btc'])} | {_fmt_pct(r['cagr_btc'])} | {_fmt(r['final_usd'], 0)} | {_fmt_pct(r['cagr_usd'])} | {_fmt_pct(r['excess_vs_hodl_usd_pct'])} | {_fmt_pct(r['max_drawdown_usd'])} | {_fmt_pct(r['max_drawdown_btc'])} | {_fmt(r['total_funding_btc'])} | {_fmt(r['total_fees_btc'])} | {_fmt(r['total_slippage_cost_btc'])} | {r['total_trades']} | {_fmt_pct(r['avg_hedge_ratio'])} | {r['verdict']} |"
        )
    lines.append("")
    lines.append("## 3. Performance by market regime (per the dynamic strategy's confirmed regime)")
    lines.append("")
    lines.append(
        "| Regime | Days | Strategy USD return | Strategy BTC return | HODL USD return | Avg hedge ratio |"
    )
    lines.append("|---|---|---|---|---|---|")
    for regime, rp in (v.get("regime_performance") or {}).items():
        lines.append(
            f"| {regime} | {rp['days']} | {_fmt_pct(rp['strategy_usd_return'])} | {_fmt_pct(rp['strategy_btc_return'])} | {_fmt_pct(rp['hodl_usd_return'])} | {_fmt_pct(rp['avg_hedge_ratio'])} |"
        )
    lines.append("")
    lines.append("## 4. Annual returns")
    lines.append("")
    yearly = main.frames["yearly_returns"]
    lines.append("| Year | Strategy USD | Strategy BTC | HODL USD |")
    lines.append("|---|---|---|---|")
    for ts, row in yearly.iterrows():
        year = pd.Timestamp(str(ts)).year
        lines.append(
            f"| {year} | {_fmt_pct(row['strategy_usd'])} | {_fmt_pct(row['strategy_btc'])} | {_fmt_pct(row['hodl_usd'])} |"
        )
    lines.append("")
    lines.append("## 5. Monthly returns (USD)")
    lines.append("")
    monthly = main.frames["monthly_returns"]
    pivot = monthly["strategy_usd"].to_frame()
    month_index = pd.DatetimeIndex(pivot.index)
    pivot["year"] = month_index.year
    pivot["month"] = month_index.month
    table = pivot.pivot(index="year", columns="month", values="strategy_usd")
    lines.append("| Year | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec |")
    lines.append("|---|" + "---|" * 12)
    for year_key, row_values in table.iterrows():
        year = int(str(year_key))
        vals = dict(row_values)
        cells = [("" if pd.isna(vals.get(m)) else f"{float(vals[m]):+.1%}") for m in range(1, 13)]
        lines.append(f"| {year} | " + " | ".join(cells) + " |")
    lines.append("")
    if sensitivity is not None and len(sensitivity):
        lines.append("## 6. Sensitivity analysis")
        lines.append("")
        lines.append(
            "| Scenario | Final BTC | USD CAGR | USD vs HODL | USD MDD | Fees (BTC) | Funding (BTC) | Trades | Verdict |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for _, srow in sensitivity.iterrows():
            lines.append(
                f"| {srow['scenario']} | {_fmt(srow['final_btc'])} | {_fmt_pct(srow['cagr_usd'])} | {_fmt_pct(srow['excess_vs_hodl_usd_pct'])} | {_fmt_pct(srow['max_drawdown_usd'])} | {_fmt(srow['total_fees_btc'])} | {_fmt(srow['total_funding_btc'])} | {srow['total_trades']} | {srow['verdict']} |"
            )
        lines.append("")
    lines.append("## 7. Charts")
    lines.append("")
    for name in (
        "btc_equity",
        "usd_equity",
        "usd_equity_vs_hodl",
        "cumulative_btc_growth",
        "hedge_ratio",
        "price_and_hedge_ratio",
        "drawdown",
        "cumulative_fees",
        "cumulative_funding",
    ):
        lines.append(f"![{name}](charts/{name}.png)")
    lines.append("")
    lines.append("## 8. Methodology, assumptions and limitations")
    lines.append("")
    lines.append(
        "- Signal: close of the completed UTC daily candle. Execution: next daily candle's open × (1 ± slippage), Taker fee. No signal and fill on the same candle."
    )
    lines.append(
        "- Funding: actual fundingRate history (00/08/16 UTC). The 00:00 funding is settled before that day's fill, the 08/16 h funding after the fill. Before 2023-11 the mark price is approximated from the daily mark OHLC."
    )
    lines.append(
        "- Valuation: unrealized PnL and equity at the Mark Price close. The index price is used for validation."
    )
    lines.append(f"- Costs: {config_dump.get('backtest', {}).get('cost')}")
    lines.append(f"- Risk limits: {config_dump.get('risk')}")
    lines.append(
        "- Contract specification: exchangeInfo snapshot (contract value 100 USD, whole contracts). The history of past specification changes cannot be verified → the current specification is held fixed (a limitation)."
    )
    lines.append(f"- Data quality: {json.dumps(data_quality, ensure_ascii=False)[:1500]}")
    for n in notes:
        lines.append(f"- {n}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_bundle(
    out_dir: Path,
    main: StrategyRun,
    benchmarks: list[StrategyRun],
    metadata: dict[str, Any],
    config_dump: dict[str, Any],
    data_quality: dict[str, Any],
    sensitivity: pd.DataFrame | None = None,
    notes: list[str] | None = None,
    make_charts: bool = True,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    notes = notes or []
    files: dict[str, Path] = {}
    r = main.result
    d = r.daily

    def _csv(name: str, df: pd.DataFrame, index: bool = True) -> None:
        p = out_dir / name
        df.to_csv(p, index=index)
        files[name] = p

    _csv("daily_equity.csv", d)
    _csv("trades.csv", r.trades, index=False)
    _csv("funding_history.csv", r.funding, index=False)
    _csv("fee_history.csv", r.fees, index=False)
    hedge_cols = [
        "regime",
        "raw_target",
        "decision_target",
        "current_ratio_at_open",
        "effective_ratio",
        "ratio_error",
        "short_contracts",
        "short_notional_usd",
        "equity_usd",
    ]
    _csv("hedge_ratio_history.csv", d[hedge_cols])
    _csv("monthly_returns.csv", main.frames["monthly_returns"])
    _csv("yearly_returns.csv", main.frames["yearly_returns"])
    _csv("drawdown.csv", main.frames["drawdown"])
    _csv("ledger.csv", r.ledger, index=False)
    _csv("signals.csv", r.signals)
    comp = comparison_frame([main, *benchmarks])
    _csv("benchmark_comparison.csv", comp, index=False)
    if sensitivity is not None:
        _csv("sensitivity.csv", sensitivity, index=False)

    summary = {
        "generated_at_utc": datetime.now(tz=UTC).isoformat(),
        "metadata": metadata,
        "config": config_dump,
        "data_quality": data_quality,
        "engine_stats": r.stats,
        "main": main.summary.to_dict(),
        "benchmarks": {b.name: b.summary.to_dict() for b in benchmarks},
        "sensitivity": None if sensitivity is None else sensitivity.to_dict(orient="records"),
        "notes": notes,
        "files": {k: str(v) for k, v in files.items()},
    }
    p = out_dir / "summary.json"
    p.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    files["summary.json"] = p

    if make_charts:
        for c in write_charts(main, benchmarks, out_dir / "charts"):
            files[f"charts/{c.name}"] = c
    md = out_dir / "backtest_report.md"
    write_markdown_report(
        md, main, benchmarks, metadata, config_dump, data_quality, sensitivity, notes
    )
    files["backtest_report.md"] = md
    return files
