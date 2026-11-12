# Inverse (COIN-M) contract math

Implementation: `src/btc_inventory_quant/domain/inverse_contract.py` (all `Decimal`).
Source: `docs/exchange_api_verification.md` §8~§11 (quotations from the official docs).

## 1. Contract definition

* 1 BTCUSD_PERP contract = **100 USD** (`exchangeInfo.contractSize`; official FAQ: "Each BTC futures contract represents 100 USD"). It is not hard-coded; it is read from exchangeInfo.
* Quantity unit: whole contracts (`quantityPrecision=0`, `LOT_SIZE.stepSize=1`, `MARKET_LOT_SIZE.maxQty=60000`).
* Price unit: `tickSize=0.1`. Margin and settlement asset: BTC.

## 2. Profit and loss

Short of `c` contracts, entry price `Pe`, exit price `Px`, contract value `CV`:

```
PnL_BTC = c × CV × (1/Px − 1/Pe)
```

Unrealized PnL is computed with the **Mark Price**: `UPnL = c × CV × (1/Mark − 1/Pe)`.

Verification cases (unit test `tests/unit/test_inverse_math.py`):

| Case | Calculation | Result |
|---|---|---|
| 1 BTC, entry 100,000, notional 100,000 USD (1,000 contracts), price 50,000 | 1000×100×(1/50000−1/100000) | +1 BTC → total 2 BTC = 100,000 USD |
| Same conditions, price 200,000 | 1000×100×(1/200000−1/100000) | −0.5 BTC → total 0.5 BTC = 100,000 USD |

In other words, a 100% hedge is a **synthetic dollar** that fixes the USD value; profit arises only in the process of lowering the hedge ratio and raising it again.

## 3. Notional value, target contract count, effective hedge ratio

```
short_notional_usd      = |c| × CV
notional_btc            = |c| × CV / price
target_short_notional   = equity_btc × mark × target_ratio
target_contracts        = floor(target_short_notional / CV)   (then rounded down to stepSize)
effective_hedge_ratio   = |c| × CV / (equity_btc × mark)
equity_btc              = wallet_balance_btc + UPnL(mark)
equity_usd              = equity_btc × mark
```

Because of `floor`, the result is always rounded to at or below the target (conservative). Anything smaller than 1 contract (100 USD) is not traded.

## 4. Average entry price (partial entries)

The BTC exposure of an inverse contract is `q × CV / P`, so the average price is a **harmonic mean**.

```
avg_entry = Σ q_i / Σ (q_i / P_i)
```

Example: 100 contracts @ 100,000 + 100 contracts @ 50,000 → 200 / (0.001 + 0.002) = **66,666.67** (the arithmetic mean 75,000 is wrong).
A partial close does not change the average price; realized PnL = `closed × CV × (1/Px − 1/avg)`.
A test confirms that the FIFO-lot method and the average-price method give the same total on a full close.

## 5. Fees (verified against the official FAQ)

```
fee_btc = |c| × CV / trade_price × fee_rate
```
Official example: 1 contract @ 8,800 → 0.01136364 BTC × 0.04% = 0.00000454 BTC (covered by a test).
Default rates (backtest): Taker 0.05%, Maker 0.02% — conservative values. The actual rate is queried with `GET /dapi/v1/commissionRate` (the current VIP0 rate could not be confirmed in the docs).

## 6. Funding fee (**VERIFIED — measured live 2026-09-04, ±8.1% precision**)

```
funding_btc = −sign(c) × |c| × CV / mark_price × funding_rate
```
* Funding rate > 0: longs pay shorts → the short **receives** (+). If < 0: the short pays.
* Settlement times: 00:00 / 08:00 / 16:00 UTC (the per-symbol interval is checked via `fundingInfo` and `premiumIndex.nextFundingTime`).
 * A COIN-M-specific source text in the "÷ Mark Price" form has still not been found in the official docs (the formula combines the fee FAQ's notional-value definition "(number of contracts × contract size)/price" with the funding FAQ's "notional value × rate"). **However, it has been confirmed by comparison with an actual settlement.**

### Measured-live verification (2026-09-04T00:00:00Z, demo environment)

| Item | Value |
|---|---|
| Rate (captured via `premiumIndex` 828 seconds before settlement) | 0.00010000 |
| Mark price | 81,206.99154563 |
| Contracts | −1 (short) |
| **Model value** | **0.0000001231421** |
| **Actual settlement amount from the exchange** | **0.00000012** |
| Difference | −3.14e-9 |
| Quantization unit of the exchange's reports | 1e-8 |
| Verdict | difference < unit → **VERIFIED** |

Reproduction: `python -m btc_inventory_quant verify-funding --config configs/testnet.yaml --snapshot-rate` (just before settlement) → after settlement, `verify-funding --window-hours 12`.

**The precision of this verification is ±8.12%.** The exchange reports `FUNDING_FEE` truncated at 8 decimal places, and the funding amount on a 1-contract position is 1.23e-7 BTC, so the truncation error accounts for 8% of the total. What has therefore been confirmed is:

* ✅ **The structure of the formula** — `÷ mark_price` is correct (without it the result would be off by a factor of 77,000; with multiplication instead, by a factor of 6e9)
* ✅ **The sign convention** — the short receives when the rate > 0
* ✅ **Application of the 100 USD contract value**
* ⚠️ **Small errors within 8% cannot be ruled out** (e.g., use of the index instead of the mark, a slight difference in the mark at the settlement instant)

To tighten this, enlarge the position — the funding amount grows in proportion to the number of contracts while the reporting unit stays fixed at 1e-8; with 8 contracts the precision is ±1.0%, with 100 contracts ±0.08%.

**Caution**: the demo environment settles funding but does not publish its `fundingRate` history (`docs/exchange_api_verification.md` §16). Plugging in the production rate (0.00317% at the time), which differs from the demo's own rate (0.01%), yields a 2.9× MISMATCH — you must capture the same environment's rate in advance with `--snapshot-rate`.
* Backtest approximation: from 2023-11-01 onward the funding record's `markPrice` is used; before that, the mark price from that day's daily candle is used (00:00 takes the open; 08:00/16:00 are linearly interpolated between open and close). The error is within a few % of the funding amount (funding itself is ~0.01% of notional).

## 7. Maintenance margin and liquidation (PARTIAL)

```
maint_margin_btc = |c| × CV / price × MMR − cum
```
Official FAQ example: 100 contracts @ 25,000, MMR 0.4% → 0.0016 BTC (covered by a test).

Liquidation price estimate for a single short, Cross margin, and no other positions:

```
wallet + c·CV·(1/P − 1/Pe) = c·CV/P·MMR − cum
→ P_liq = c·CV·(1 − MMR) / (c·CV/Pe − wallet − cum)
```
If the denominator ≤ 0 (i.e., `wallet ≥ c·CV/Pe`, effective leverage ≤ 1), **no liquidation price exists**.
This strategy runs with hedge ratio ≤ 85% and effective leverage ≤ 1 and is therefore, in theory, never liquidated no matter how far the price rises:

```
equity(P) − MM(P) = E0(1−h) + (h·E0·P0/P)(1 − MMR) > 0   (h < 1)
```
However, accumulated negative (−) funding and fees reduce the wallet, so in practice the exchange's `positionRisk.liquidationPrice` is monitored as the primary source and this estimate is used as a secondary one.

## 8. Price rise under an 85% hedge

850 contracts (85,000 USD) @ 100,000, price → ∞: loss cap = 0.85 BTC, equity → 0.15 BTC. The sign of the position does not change with price moves (the contract count is unchanged), and the effective hedge ratio decreases to below 1; a BUY order must always satisfy `qty ≤ current short`, and if it exceeds the short: `PositionFlipError` / `RiskLimitViolation(BUY_EXCEEDS_SHORT)`.

## 9. Rounding rules

* Contract count: always rounded down (`ROUND_DOWN`).
* BTC amounts for display/ledger: 8 decimal places (`ROUND_HALF_EVEN`); internal calculations keep full precision.
* If a value measured live on the exchange (fill fees, funding) differs from the internal calculation by 1 satoshi or more, a reconciliation warning is raised.
