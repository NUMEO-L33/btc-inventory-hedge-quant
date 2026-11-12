# Architecture

## 1. Purpose

While holding BTC long-term, make rule-based, gradual adjustments to the **short ratio (0–85%)** of a BTC-margined COIN-M
inverse perpetual. Performance is always evaluated **in BTC quantity** and **in USD value** at the same time.

## 2. Layer structure

```
Strategy  →  Portfolio Engine  →  Risk Engine  →  Execution Engine  →  Exchange Adapter
(target hedge ratio)   (equity, ledger, positions)     (limits, guards, kill switch)   (order management, rebalancing, consistency)   (Binance COIN-M REST+WS / Paper / Replay)
```

| Layer | Package | Responsibility | Never does |
|---|---|---|---|
| Strategy | `strategy/` | Closed daily candle → market state → **raw target hedge ratio** | Orders, exchange calls, equity calculation |
| Portfolio Engine | `domain/` | Inverse math, position average price, ledger (deposits/withdrawals kept separate), Equity (BTC/USD) | Signal decisions |
| Risk Engine | `risk/` | 85% cap, 20%p daily change limit, 10%p minimum difference, no longs, BUY ≤ short, notional ≤ equity, SAFE_HOLD | Sending orders |
| Execution Engine | `execution/` | Target contract count → orders (δ), unique client order id, timeout/duplicate prevention, partial fills, reconciliation | Strategy logic |
| Exchange Adapter | `exchange/` | REST calls and signing, WebSocket streams (user data / markPrice, secondary source), contract spec queries, response validation and normalization, paper exchange | Risk decisions |
| Backtest | `backtest/` | Event loop (close signal → next-open fill), cost and funding models, reports | Referencing future data |
| Runtime | `runtime/` | Orchestration of the backtest / shadow / testnet / live modes, Live gate | — |
| Storage | `storage/` | SQLAlchemy models (orders, fills, ledger, snapshots, state), SQLite/PostgreSQL | — |
| Monitoring | `monitoring/` | Structured JSON logs (masked), metric snapshots, alerts (Telegram/Email/Log) | Causing trading to halt |

Strategy code **never calls the exchange API directly**. Only the execution engine uses the `ExchangeAdapter`
protocol in `exchange/base.py`, and backtest, Shadow, Testnet and Live reuse the same
Strategy/Risk/Execution code, swapping only the adapter.

## 3. Data flow (1 decision per day)

```
[UTC 00:00 daily candle close] → load completed candles only → indicators (EMA20/50/200, ATR20)
   → market state (bull/neutral/bear, 2-bar confirmation) → raw target hedge ratio (+ overbought/oversold adjustment)
   → Risk: clamp[0, 0.85] → daily change ±20%p → no trade if |target−current| < 10%p
   → Portfolio: target contracts = floor(equity_btc × mark × ratio / contract_value)
   → Execution: δ = target − current (SELL: increase short / BUY: reduce-only short decrease, BUY ≤ short)
   → Adapter: order (unique clientOrderId) → confirm fill → re-query position → write to DB → alert
```

The backtest fills the same pipeline at the **next candle's open** and applies slippage, fees and funding.

## 4. State and storage

* Internal state (positions, orders, ledger, hedge ratio history) is recorded in the DB (SQLite by default, PostgreSQL in operation).
* On restart, the exchange state (positions, open orders, balances) is queried, and the normal loop is entered only after
  **reconciliation** with the internal state. On a mismatch, SAFE_HOLD.

## 5. Operating modes

| Mode | Data | Orders | Default |
|---|---|---|---|
| backtest | Historical files | Simulated | ✔ |
| shadow | Real-time public data | Simulated (Paper) — no real orders | ✔ |
| testnet | Demo exchange | Demo orders | Explicit opt-in |
| live | Live exchange | Real orders — only after passing multiple gates | OFF by default |

## 6. Key design decisions

1. **Decimal**: orders, equity, fees, funding and contract counts are Decimal. Indicators are NumPy/pandas float.
2. **Contract spec not hard-coded**: `contractSize`, `stepSize`, `tickSize` are read from exchangeInfo.
3. **Single definition of the 85% cap**: in one place, `config/schema.py::RiskLimits`. The number is not placed anywhere else in the code.
4. **Short only**: the position model blocks a flip to long with an exception, and risk validation rejects BUY > short.
5. **Fail-safe = keep the position**: on failure, stop new orders, cancel open orders, keep the current position (SAFE_HOLD).
6. **Reproducibility**: config hash, code commit, seed and run time are recorded in every report.

## 7. Directory layout

```
src/btc_inventory_quant/
  config/      schema.py(all settings) settings.py(environment variables) loader.py(YAML+hash)
  domain/      inverse_contract.py position.py portfolio.py ledger.py
  data/        market_data.py funding_data.py repositories.py downloader.py
  indicators/  trend.py volatility.py
  strategy/    base.py regime_hedge.py state_machine.py benchmarks.py
  backtest/    engine.py execution_model.py funding_model.py metrics.py reports.py runner.py scenarios.py walkforward.py
  exchange/    base.py models.py binance_coinm.py binance_ws.py paper.py
  execution/   order_manager.py rebalancer.py reconciliation.py policies.py
  risk/        limits.py guards.py kill_switch.py
  monitoring/  logging.py metrics.py alerts.py
  storage/     database.py models.py
  runtime/     app.py live_gate.py trader.py
  cli/         main.py
```
