"""Performance metrics (BTC and USD basis, always both). Float math is fine here: these are
statistics about a finished simulation, never inputs to orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from btc_inventory_quant.backtest.engine import BacktestResult

DAYS_PER_YEAR = 365.25


def _f(x: Any) -> float:
    if x is None:
        return float("nan")
    return float(x)


def cagr(start_value: float, end_value: float, days: int) -> float:
    if start_value <= 0 or end_value <= 0 or days <= 0:
        return float("nan")
    return float((end_value / start_value) ** (DAYS_PER_YEAR / days) - 1.0)


def max_drawdown(series: pd.Series) -> tuple[float, pd.Series]:
    """Returns (max drawdown as a negative fraction, drawdown series)."""
    s = series.astype(float)
    peak = s.cummax()
    dd = s / peak - 1.0
    return float(dd.min()) if len(dd) else float("nan"), dd


def longest_negative_streak(returns: pd.Series) -> tuple[int, float]:
    """(longest run of consecutive negative returns, worst cumulative return over any negative run)."""
    best_len = 0
    worst_cum = 0.0
    cur_len = 0
    cur_cum = 1.0
    for r in returns.dropna().astype(float):
        if r < 0:
            cur_len += 1
            cur_cum *= 1.0 + r
            best_len = max(best_len, cur_len)
            worst_cum = min(worst_cum, cur_cum - 1.0)
        else:
            cur_len = 0
            cur_cum = 1.0
    return best_len, worst_cum


def period_returns(series: pd.Series, freq: str) -> pd.Series:
    s = series.astype(float)
    ends = s.resample(freq).last()
    starts = ends.shift(1)
    starts.iloc[0] = s.iloc[0]
    return ends / starts - 1.0


@dataclass(slots=True)
class PerformanceSummary:
    values: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self.values.items():
            if isinstance(v, Decimal):
                out[k] = str(v)
            elif isinstance(v, float | np.floating):
                out[k] = None if np.isnan(v) else float(v)
            elif isinstance(v, np.integer):
                out[k] = int(v)
            else:
                out[k] = v
        return out


def summarize(
    result: BacktestResult, initial_btc: Decimal
) -> tuple[PerformanceSummary, dict[str, pd.DataFrame]]:
    d = result.daily
    days = len(d)
    start_btc = float(initial_btc)
    start_usd = float(initial_btc) * _f(d["mark_open"].iloc[0])
    equity_btc = d["equity_btc"].astype(float)
    equity_usd = d["equity_usd"].astype(float)
    hodl_usd = d["hodl_usd"].astype(float)
    final_btc = float(equity_btc.iloc[-1])
    final_usd = float(equity_usd.iloc[-1])
    hodl_final_usd = float(hodl_usd.iloc[-1])

    # prepend the starting point so drawdowns include day-1 losses
    first_ts = d.index[0] - pd.Timedelta(days=1)
    eq_usd_full = pd.concat([pd.Series([start_usd], index=[first_ts]), equity_usd])
    eq_btc_full = pd.concat([pd.Series([start_btc], index=[first_ts]), equity_btc])
    hodl_full = pd.concat([pd.Series([start_usd], index=[first_ts]), hodl_usd])
    mdd_usd, dd_usd = max_drawdown(eq_usd_full)
    mdd_btc, dd_btc = max_drawdown(eq_btc_full)
    mdd_hodl, dd_hodl = max_drawdown(hodl_full)

    ret_usd = eq_usd_full.pct_change().dropna()
    ret_btc = eq_btc_full.pct_change().dropna()
    ret_hodl = hodl_full.pct_change().dropna()
    vol_usd = (
        float(ret_usd.std(ddof=0) * np.sqrt(DAYS_PER_YEAR)) if len(ret_usd) > 1 else float("nan")
    )
    sharpe_usd = (
        float(ret_usd.mean() / ret_usd.std(ddof=0) * np.sqrt(DAYS_PER_YEAR))
        if len(ret_usd) > 1 and ret_usd.std(ddof=0) > 0
        else float("nan")
    )
    cagr_usd = cagr(start_usd, final_usd, days)
    cagr_btc = cagr(start_btc, final_btc, days)
    cagr_hodl = cagr(start_usd, hodl_final_usd, days)

    totals = result.portfolio.ledger.totals()
    trades = result.trades
    n_trades = len(trades)
    traded_notional = float(trades["notional_usd"].astype(float).sum()) if n_trades else 0.0
    mean_equity_usd = float(equity_usd.mean())
    turnover = traded_notional / mean_equity_usd if mean_equity_usd > 0 else float("nan")
    years = days / DAYS_PER_YEAR
    closing = trades[(trades["side"] == "BUY")] if n_trades else trades
    streak_trades, _ = (
        longest_negative_streak(closing["realized_pnl_btc"].astype(float))
        if len(closing)
        else (0, 0.0)
    )
    streak_days_usd, worst_run_usd = longest_negative_streak(ret_usd)
    streak_days_btc, worst_run_btc = longest_negative_streak(ret_btc)

    hedge = d["effective_ratio"].astype(float)
    regime_perf = _regime_split(d, ret_usd, ret_btc, ret_hodl)

    monthly = pd.DataFrame(
        {
            "strategy_usd": period_returns(eq_usd_full, "ME"),
            "strategy_btc": period_returns(eq_btc_full, "ME"),
            "hodl_usd": period_returns(hodl_full, "ME"),
        }
    )
    yearly = pd.DataFrame(
        {
            "strategy_usd": period_returns(eq_usd_full, "YE"),
            "strategy_btc": period_returns(eq_btc_full, "YE"),
            "hodl_usd": period_returns(hodl_full, "YE"),
        }
    )
    drawdown = pd.DataFrame(
        {"drawdown_usd": dd_usd, "drawdown_btc": dd_btc, "drawdown_hodl_usd": dd_hodl}
    )

    values: dict[str, Any] = {
        "strategy": result.strategy_name,
        "start_date": d.index[0].date().isoformat(),
        "end_date": d.index[-1].date().isoformat(),
        "days": days,
        "years": years,
        "start_btc": start_btc,
        "final_btc": final_btc,
        "net_btc_growth": final_btc - start_btc,
        "btc_growth_pct": final_btc / start_btc - 1.0,
        "cagr_btc": cagr_btc,
        "start_usd": start_usd,
        "final_usd": final_usd,
        "usd_return_pct": final_usd / start_usd - 1.0,
        "cagr_usd": cagr_usd,
        "hodl_final_usd": hodl_final_usd,
        "hodl_cagr_usd": cagr_hodl,
        "excess_vs_hodl_usd_pct": final_usd / hodl_final_usd - 1.0,
        "excess_vs_hodl_btc_pct": final_btc / start_btc - 1.0,
        "max_drawdown_usd": mdd_usd,
        "max_drawdown_btc": mdd_btc,
        "hodl_max_drawdown_usd": mdd_hodl,
        "annual_volatility_usd": vol_usd,
        "sharpe_usd": sharpe_usd,
        "calmar_usd": (cagr_usd / abs(mdd_usd)) if mdd_usd < 0 else float("nan"),
        "total_realized_pnl_btc": float(totals.realized_pnl),
        "total_unrealized_pnl_btc": float(
            result.portfolio.unrealized_pnl_btc(d["mark_close"].iloc[-1])
        ),
        "total_funding_btc": float(totals.funding),
        "total_fees_btc": float(totals.commission),
        "total_slippage_cost_btc": float(totals.slippage_cost),
        "total_trades": n_trades,
        "total_traded_notional_usd": traded_notional,
        "turnover_total": turnover,
        "turnover_annualized": turnover / years if years > 0 else float("nan"),
        "avg_hedge_ratio": float(hedge.mean()),
        "max_hedge_ratio": float(hedge.max()),
        "min_hedge_ratio": float(hedge.min()),
        "days_hedged_over_50pct": int((hedge > 0.5).sum()),
        "worst_consecutive_losing_days_usd": streak_days_usd,
        "worst_losing_run_usd_pct": worst_run_usd,
        "worst_consecutive_losing_days_btc": streak_days_btc,
        "worst_losing_run_btc_pct": worst_run_btc,
        "worst_consecutive_losing_closes": streak_trades,
        "missed_rebalances": result.stats.get("missed_rebalances", 0),
        "partial_fills": result.stats.get("partial_fills", 0),
        "funding_events_exact_mark": result.stats.get("funding_events_exact_mark", 0),
        "funding_events_kline_8h_mark": result.stats.get("funding_events_kline_8h_mark", 0),
        "funding_events_approximated_mark": result.stats.get("funding_events_approximated_mark", 0),
        "regime_performance": regime_perf,
        "verdict": (
            "BENCHMARK"
            if result.strategy_name == "hodl"
            else verdict(
                final_btc / start_btc - 1.0, final_usd / hodl_final_usd - 1.0, mdd_usd, mdd_hodl
            )
        ),
    }
    frames = {"monthly_returns": monthly, "yearly_returns": yearly, "drawdown": drawdown}
    return PerformanceSummary(values), frames


def _regime_split(
    d: pd.DataFrame, ret_usd: pd.Series, ret_btc: pd.Series, ret_hodl: pd.Series
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    labels = d["regime"].astype(str)
    for regime in ("bull", "neutral", "bear"):
        mask = labels == regime
        idx = d.index[mask]
        if len(idx) == 0:
            continue
        r_usd = ret_usd.reindex(idx).dropna()
        r_btc = ret_btc.reindex(idx).dropna()
        r_h = ret_hodl.reindex(idx).dropna()
        out[regime] = {
            "days": int(mask.sum()),
            "strategy_usd_return": float(np.prod(1.0 + r_usd.to_numpy(dtype=float)) - 1.0),
            "strategy_btc_return": float(np.prod(1.0 + r_btc.to_numpy(dtype=float)) - 1.0),
            "hodl_usd_return": float(np.prod(1.0 + r_h.to_numpy(dtype=float)) - 1.0),
            "avg_hedge_ratio": float(d.loc[mask, "effective_ratio"].astype(float).mean()),
        }
    return out


def verdict(btc_growth: float, usd_excess_vs_hodl: float, mdd_usd: float, mdd_hodl: float) -> str:
    """Both criteria must hold: BTC count must grow AND USD outcome must not be severely worse than HODL.

    * FAIL  : BTC count fell, or USD equity ended more than 30% below HODL, or USD drawdown worse than HODL
    * WEAK  : BTC grew but USD trails HODL by >10% (typical in strong bull markets: hedging costs upside)
    * PASS  : BTC grew and USD outcome within 10% of HODL (or better) with a smaller drawdown
    """
    if btc_growth <= 0 or usd_excess_vs_hodl <= -0.30 or mdd_usd < mdd_hodl - 0.05:
        return "FAIL"
    if usd_excess_vs_hodl < -0.10:
        return "WEAK"
    return "PASS"
