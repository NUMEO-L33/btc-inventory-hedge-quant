# Demo connection and funding formula verification procedure

Purpose: **resolve the 4 unverified items with a single real connection.** Item ① in particular is the key one.

| # | Item | Current status | With this procedure |
|---|---|---|---|
| ① | **Funding amount formula** (`÷ Mark Price`) | **PARTIAL** — the COIN-M source text does not state the denominator | Compare against 1 actual settlement |
| ② | Real connection to the exchange | **0 times** | `--once` smoke test |
| ③ | Actual fee rate (VIP tier) | Documented values in use | Query `commissionRate` |
| ④ | WebSocket against the real server | Local mocks only | Check during continuous operation |

> **Why ① matters**: the only effect that survived four rounds of research is **funding carry** (`docs/strategy_v4_control_arms.md`). Everything else was indistinguishable from random. Yet the formula that computes that carry is unverified. **If it is wrong, all of V1–V4 has to be recomputed.** So this is not a formality; it is a check of the foundations.

---

## 0. Security — read this first

* **Keep the API Key and Secret only in `.env`.** Never put them in chat, screenshots or commits. `.env` is in `.gitignore`.
* **Never mix demo keys and real-account keys.** The variable names differ (`BIQ_BINANCE_DEMO_*` vs `BIQ_BINANCE_*`).
* This is not the stage for creating a real-account key. **This procedure needs only a demo key.**
* If live trading comes up for discussion later, then: a dedicated sub-account · **withdrawal permission disabled** · IP whitelist. Withdrawal permission cannot be queried through the COIN-M API, so it must be checked directly in the exchange UI.

## 1. Issue a demo key

1. Log in to Binance → open **Binance Demo Trading**
2. In API Key Management, `[Create API]`
3. Permissions: **enable Futures**. Withdrawals do not exist in the demo in the first place.
4. Copy the Key and Secret (the Secret is shown only once, at this point)

> **UNVERIFIED warning**: **no official statement was found that explicitly says** the same demo key also applies to COIN-M (`demo-dapi.binance.com`) (`docs/exchange_api_verification.md` §13). If step 2 fails, this assumption is wrong, so do not force a workaround — report the result.

## 2. Configuration and smoke test

```
cd ~/Downloads/btc-inventory-hedge-quant
source .venv/bin/activate
```

Edit `.env` (if it does not exist, `cp .env.example .env`):

```
BIQ_BINANCE_DEMO_API_KEY=your_issued_key
BIQ_BINANCE_DEMO_API_SECRET=your_issued_secret
```

Check the connection:

```
python -m btc_inventory_quant testnet --config configs/testnet.yaml --once
```

What to look for in the startup report:

| Item | Expected | Meaning |
|---|---|---|
| `contract_spec` | `contractSize: 100` | ③ Contract spec confirmed on the real server |
| `commission` | `actual_taker` vs `assumed_taker`, `taker_drift_pct`, `backtest_assumption_ok` | ③ resolved — automatically compared with the backtest assumption |
| `position_mode` | `ONE_WAY` | If not, change it in the exchange UI |
| `account.canTrade` | `true` | Key permissions are fine |

**If it fails, stop here and report the error message.** In particular, `-2015` (permissions/IP), connection refused (host problem) and `-1022` (signature) each have a different cause.

## 3. Open a position and hold it through a settlement

Funding settlement happens at **00:00 / 08:00 / 16:00 UTC**. A record is created only if a position is open at that time.

The simplest way is to open a **small BTCUSD_PERP short** in the demo UI (e.g., 10 contracts = 1,000 USD notional) and hold it past the next settlement time. The size does not matter — **only the ratio has to match, so the smaller the better.**

It is important **not to change** the position size until the settlement time. If it changes, it becomes ambiguous which position to use in the calculation (if you had to change it, specify it explicitly with `--contracts` in step 4).

Settlement times in Korea time: **09:00 / 17:00 / 01:00 (KST)**.

## 4. Run the verification

After the settlement time has passed:

```
python -m btc_inventory_quant verify-funding --config configs/testnet.yaml --window-hours 24
```

If you changed the position in between, specify the contract count at the time of settlement directly (negative for a short):

```
python -m btc_inventory_quant verify-funding --config configs/testnet.yaml --contracts -10
```

### How to read the result

```json
{
  "verdict": "VERIFIED",
  "samples": 1,
  "mean_ratio": "1.0000...",
  "resolves_partial": true,
  "settlements": [{ "actual_btc": "...", "computed_btc": "...", "ratio": "1.000..." }]
}
```

| verdict | Meaning | Next |
|---|---|---|
| `VERIFIED` | The model value matches the exchange's actual value to within 0.5% | **① resolved.** Update the docs from PARTIAL → VERIFIED |
| `MISMATCH` | Mismatch | **The formula or the mark price source is wrong.** `ratio` is the clue (e.g., 2.0 means the denominator is half; a value not near 1.0 means the denominator itself is different). **V1–V4 must be recomputed** |
| `MIXED` | Only some match | The position size may have differed between settlements. Re-run with `--contracts` |
| `NO_DATA` | No settlement record | Either no settlement time was crossed or the window is too short. Retry with `--window-hours 72` |

`ratio` is **actual ÷ model**. A value of 1.0 means our formula is correct.

## 5. When reporting the result

Just paste the entire JSON as is. **Keys and secrets are not included in the output** (log masking is applied). The account balance is not included either — only settlement amounts, rates, mark prices and contract counts appear.

If `VERIFIED`, I update 3 documents (`inverse_math.md` §6, `exchange_api_verification.md`, `requirements_audit.md`) and remove the item from the unverified list.
If `MISMATCH`, I back out the actual denominator from `ratio`, fix the formula, and re-run all of the backtests.

## 6. Cleanup

Once the verification is done, the demo position may be closed. If you keep it, ④ (WebSocket against the real server) can also be observed, so leaving `testnet` running continuously for a few days resolves that one as a bonus.

```
python -m btc_inventory_quant testnet --config configs/testnet.yaml     # run continuously
python -m btc_inventory_quant status --config configs/testnet.yaml      # check status
```

## 7. What this procedure does not do

* **It does not trade live.** It is demo-only, and the live gates are, separately, all switched off.
* **It does not solve the problem of having only 1 crash sample.** That requires data from before 2020 and is unrelated to this procedure.
* **It is not evidence that the strategy works.** The V4 conclusion (indistinguishable from random) stands. This only checks **whether the cost model is right**.
