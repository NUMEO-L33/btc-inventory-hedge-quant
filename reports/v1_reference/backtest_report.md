# Backtest report — regime_hedge_v1

- Run ID: `v1_reference-20260902T072844Z-c80ee8f8` / run timestamp (UTC): 2026-09-02T07:28:44.680457+00:00
- Code version: 0.1.0 / git: `0f1b16bdf6b1f9d212b129caf77ba156babc0a7c` / config hash: `c80ee8f84832b1b28069796e69f6995aba00709ea3bbb58dbb300dad7b3aa154` / seed: 42
- Period: 2021-03-10 → 2026-09-01 (2002 days, 5.48 years) / starting BTC: 1.0

## 1. Key results (evaluated in BTC terms and USD terms simultaneously)

| Metric | Value |
|---|---|
| Final BTC | 1.1085 |
| Net BTC growth | 0.1085 |
| CAGR in BTC terms | +1.90% |
| Starting USD | 54,956 |
| Final USD | 85,759 |
| CAGR in USD terms | +8.46% |
| HODL final USD | 77,368 |
| USD excess return vs HODL | +10.85% |
| USD maximum drawdown | -47.12% |
| HODL USD maximum drawdown | -76.71% |
| BTC maximum drawdown | -60.30% |
| USD Sharpe (daily, rf=0) | 0.41 |
| Total realized PnL (BTC) | 0.0049 |
| Total unrealized PnL (BTC) | 0.0027 |
| Total funding (BTC, + = received) | 0.1246 |
| Total trading fees (BTC) | -0.0238 |
| Total slippage cost (BTC) | 0.0238 |
| Total number of trades | 243 |
| Total turnover (notional / average equity) | 37.39 |
| Average hedge ratio | +44.22% |
| Maximum hedge ratio | +84.90% |
| Worst consecutive losing days (USD) | 8 |
| Return over the worst losing streak (USD) | -15.76% |
| Worst consecutive losing closing trades | 18 |
| Verdict | PASS |

### BTC PnL decomposition (final BTC − starting BTC = realized + unrealized + funding + fees)

| Component | BTC |
|---|---|
| Hedge timing PnL (realized + unrealized, before fees) | +0.0076 |
| Funding received (+) / paid (−) | +0.1246 |
| Trading fees | -0.0238 |
| Slippage (already reflected in fill prices; for reference) | -0.0238 |
| Net BTC growth | +0.1085 |

Verdict criteria: FAIL if the BTC count did not grow, the USD value trails HODL by 30% or more, or the USD drawdown is worse than HODL's; WEAK if USD trails HODL by 10% or more; PASS otherwise. If only BTC grows while USD falls sharply, that counts as a failure.

## 2. Benchmark strategies

| Strategy | Final BTC | BTC CAGR | Final USD | USD CAGR | USD vs HODL | USD MDD | BTC MDD | Funding (BTC) | Fees (BTC) | Slippage (BTC) | Trades | Avg hedge ratio | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| regime_hedge_v1 | 1.1085 | +1.90% | 85,759 | +8.46% | +10.85% | -47.12% | -60.30% | 0.1246 | -0.0238 | 0.0238 | 243 | +44.22% | PASS |
| hodl | 1.0000 | +0.00% | 77,368 | +6.44% | +0.00% | -76.71% | +0.00% | 0.0000 | 0.0000 | 0.0000 | 0 | +0.00% | BENCHMARK |
| fixed_hedge_100 | 1.1718 | +2.94% | 90,661 | +9.56% | +17.18% | -4.28% | -82.38% | 0.5266 | -0.0007 | 0.0007 | 9 | +96.04% | PASS |
| fixed_hedge_50 | 1.2460 | +4.09% | 96,398 | +10.80% | +24.60% | -48.68% | -55.95% | 0.2462 | -0.0009 | 0.0009 | 11 | +48.94% | PASS |
| ema200_simple | 1.0714 | +1.27% | 82,896 | +7.79% | +7.14% | -50.56% | -59.74% | 0.0507 | -0.0172 | 0.0173 | 165 | +36.22% | PASS |

## 3. Performance by market regime (per the dynamic strategy's confirmed regime)

| Regime | Days | Strategy USD return | Strategy BTC return | HODL USD return | Avg hedge ratio |
|---|---|---|---|---|---|
| bull | 864 | +117.48% | -9.31% | +139.81% | +14.36% |
| neutral | 377 | -40.68% | +54.30% | -61.55% | +46.18% |
| bear | 761 | +20.96% | -20.79% | +52.70% | +77.14% |

## 4. Annual returns

| Year | Strategy USD | Strategy BTC | HODL USD |
|---|---|---|---|
| 2021 | -13.24% | +3.22% | -15.95% |
| 2022 | -27.03% | +103.91% | -64.21% |
| 2023 | +61.83% | -36.75% | +155.85% |
| 2024 | +62.80% | -26.29% | +120.86% |
| 2025 | -1.75% | +4.89% | -6.33% |
| 2026 | -4.78% | +7.70% | -11.58% |

## 5. Monthly returns (USD)

| Year | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2021 |  |  | +7.1% | -5.3% | -24.2% | -1.4% | +4.6% | +13.8% | -7.2% | +24.3% | -5.2% | -12.1% |
| 2022 | -2.8% | +3.6% | +1.5% | -10.0% | -2.6% | -9.5% | +4.8% | -6.2% | -0.4% | +0.9% | -8.6% | -0.4% |
| 2023 | +18.2% | +0.1% | +7.3% | +2.6% | -6.5% | +7.5% | -3.5% | -8.8% | +0.3% | +17.6% | +7.6% | +10.7% |
| 2024 | -0.8% | +36.4% | +14.5% | -13.4% | +6.7% | -4.2% | -1.2% | -15.4% | +1.8% | +9.1% | +31.3% | -2.7% |
| 2025 | +6.6% | -11.6% | -6.4% | +6.3% | +9.9% | +2.5% | +7.4% | -2.9% | +0.9% | -5.1% | -6.2% | -0.6% |
| 2026 | -3.6% | -4.5% | +0.6% | +4.1% | -1.4% | -5.6% | +1.5% | +5.9% | -1.2% |  |  |  |

## 6. Sensitivity analysis

| Scenario | Final BTC | USD CAGR | USD vs HODL | USD MDD | Fees (BTC) | Funding (BTC) | Trades | Verdict |
|---|---|---|---|---|---|---|---|---|
| base | 1.1085 | +8.46% | +10.85% | -47.12% | -0.0238 | 0.1246 | 243 | PASS |
| fees_x2 | 1.0885 | +8.10% | +8.85% | -47.42% | -0.0476 | 0.1231 | 247 | PASS |
| slippage_x3 | 1.0712 | +7.78% | +7.12% | -47.73% | -0.0239 | 0.1221 | 251 | PASS |
| adverse_funding_-1bp_per_8h | 0.8461 | +3.24% | -15.39% | -52.36% | -0.0209 | -0.2007 | 241 | FAIL |
| adverse_funding_-3bp_per_8h | 0.5001 | -6.20% | -49.99% | -60.75% | -0.0163 | -0.6503 | 244 | FAIL |
| signal_delay_1d | 1.1853 | +9.44% | +18.53% | -46.68% | -0.0242 | 0.1283 | 241 | PASS |
| missed_rebalance_#1 | 1.1005 | +8.31% | +10.05% | -47.63% | -0.0240 | 0.1234 | 249 | PASS |
| missed_rebalance_#5 | 1.0974 | +8.26% | +9.74% | -47.56% | -0.0236 | 0.1237 | 243 | PASS |
| missed_every_10th | 1.1441 | +9.09% | +14.41% | -47.15% | -0.0237 | 0.1285 | 239 | PASS |
| partial_fills_50pct_p0.5 | 1.1579 | +9.32% | +15.79% | -46.56% | -0.0211 | 0.1300 | 317 | PASS |
| fees_x2_slip_x3_delay_1d | 1.0894 | +7.77% | +8.94% | -48.12% | -0.0453 | 0.1246 | 232 | PASS |
| maker_fills | 1.1201 | +8.66% | +12.01% | -47.04% | -0.0096 | 0.1250 | 247 | PASS |

## 7. Charts

![btc_equity](charts/btc_equity.png)
![usd_equity](charts/usd_equity.png)
![usd_equity_vs_hodl](charts/usd_equity_vs_hodl.png)
![cumulative_btc_growth](charts/cumulative_btc_growth.png)
![hedge_ratio](charts/hedge_ratio.png)
![price_and_hedge_ratio](charts/price_and_hedge_ratio.png)
![drawdown](charts/drawdown.png)
![cumulative_fees](charts/cumulative_fees.png)
![cumulative_funding](charts/cumulative_funding.png)

## 8. Methodology, assumptions and limitations

- Signal: close of the completed UTC daily candle. Execution: next daily candle's open × (1 ± slippage), Taker fee. No signal and fill on the same candle.
- Funding: actual fundingRate history (00/08/16 UTC). The 00:00 funding is settled before that day's fill, the 08/16 h funding after the fill. Before 2023-11 the mark price is approximated from the daily mark OHLC.
- Valuation: unrealized PnL and equity at the Mark Price close. The index price is used for validation.
- Costs: {'taker_fee_rate': '0.0005', 'maker_fee_rate': '0.0002', 'use_maker': False, 'slippage_bps': '5', 'fee_multiplier': '1', 'slippage_multiplier': '1', 'funding_adverse_bps_per_period': '0', 'signal_delay_days': 0, 'partial_fill_ratio': '1', 'partial_fill_probability': '0', 'missed_rebalance_every_n': 0, 'missed_rebalance_indices': [], 'reorder_missed_next_day': True}
- Risk limits: {'max_hedge_ratio': '0.85', 'min_hedge_ratio': '0.00', 'max_daily_hedge_change': '0.20', 'min_rebalance_diff': '0.10', 'allow_long': False, 'max_effective_leverage': '1.0', 'max_short_notional_to_equity': '1.0', 'max_hedge_ratio_tolerance': '0.03', 'stale_price_max_age_seconds': 120, 'stale_mark_max_age_seconds': 120, 'max_consecutive_order_rejects': 3, 'max_clock_skew_ms': 1000, 'min_margin_ratio_buffer': '0.25', 'max_unexpected_balance_diff_btc': '0.00001'}
- Contract specification: exchangeInfo snapshot (contract value 100 USD, whole contracts). The history of past specification changes cannot be verified → the current specification is held fixed (a limitation).
- Data quality: {"market": {"klines_1d": {"dataset": "klines_1d", "rows": 2213, "first_open_time": "2020-08-11T00:00:00+00:00", "last_open_time": "2026-09-01T00:00:00+00:00", "gaps": [], "duplicates": 0, "ohlc_violations": 0, "dropped_incomplete": 1, "notes": []}, "mark_price_klines_1d": {"dataset": "mark_price_klines_1d", "rows": 2213, "first_open_time": "2020-08-11T00:00:00+00:00", "last_open_time": "2026-09-01T00:00:00+00:00", "gaps": [], "duplicates": 0, "ohlc_violations": 0, "dropped_incomplete": 1, "notes": []}, "index_price_klines_1d": {"dataset": "index_price_klines_1d", "rows": 2223, "first_open_time": "2020-08-01T00:00:00+00:00", "last_open_time": "2026-09-01T00:00:00+00:00", "gaps": [], "duplicates": 0, "ohlc_violations": 0, "dropped_incomplete": 1, "notes": []}}, "funding": {"rows": 6640, "first": "2020-08-10T16:00:00+00:00", "last": "2026-09-02T00:00:00+00:00", "irregular_intervals": ["2026-06-30T00:00:00+00:00 -> 2026-06-30T16:00:00+00:00 (16.00 h)"], "rows_with_mark_price": 3107, "min_rate": "-0.00300000", "max_rate": "0.00186012"}, "notes": [], "contract_spec_source": "data/raw/exchange_info.json (serverTime=1788317330289)"}
- The funding notional formula (contracts × contract value ÷ mark price) is PARTIAL because no COIN-M-specific source text for it was found in the official documentation; it is to be back-verified against measured live Shadow/Testnet data.
- Slippage and partial fills are modelled values; actual execution quality needs to be recalibrated with live trading data.
