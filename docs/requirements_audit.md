# Requirements Traceability Matrix (Requirements Audit)

Written 2026-09-02. Records the implementation status of each item of the original requirements (21 sections), with evidence from code and tests.
Status: ✅ implemented + automated test / 🟢 implemented (verified indirectly by higher-level tests) / 🟡 partially implemented or documented limitation / ❌ not implemented.

> This matrix was written in response to a review request asking "did anything get sloppy from doing it all in one pass?", and the 7 omissions found in the audit (WebSocket, Shadow funding, immediate decision at startup, unpopulated metrics, missing integration tests, documentation mismatches, scope of API permission verification) are marked below as **[added after audit]**.

## §1 Economic structure of the strategy
| Item | Status | Evidence |
|---|---|---|
| BTC as the base asset, 0–85% hedge with a COIN-M short | ✅ | `risk/limits.py`, `execution/rebalancer.py`, `test_risk_limits_config.py` |
| 100% hedge = synthetic dollar structure | ✅ | `test_inverse_math.py::test_case_1/2`, `test_backtest_engine.py::test_case_1_and_2_reproduced_by_engine` |
| Gradual adjustment rather than ON/OFF | ✅ | 20pp daily cap, 10pp minimum difference (`test_daily_cap_and_min_diff_are_enforced`) |

## §2 Inverse contract math
| Item | Status | Evidence |
|---|---|---|
| Formulas for PnL / unrealized / target notional / target contract count / effective hedge ratio | ✅ | `domain/inverse_contract.py`, `test_inverse_math.py` |
| Contract specification not hardcoded | ✅ | `ContractSpec.from_exchange_info_symbol`, `data/repositories.py::load_contract_spec`, runtime `adapter.contract_spec` + `CONTRACT_SPEC_CHANGED` guard |
| Orders, assets, fees, contract counts and funding in Decimal | ✅ | Decimal in every domain module; float for indicators only |
| Tests 1–4 | ✅ | `test_case_1_price_halves…`, `test_case_2…`, `test_case_3_85pct…`, `test_case_4_buy_exceeding_short…` |

## §3 Initial strategy V1
| Item | Status | Evidence |
|---|---|---|
| Daily bars, UTC, completed candles only | ✅ | `data/market_data.py` (close_time < as_of), `runtime/trader.py::completed_candles`, `test_incomplete_last_candle_is_dropped` |
| EMA20/50/200, ATR20 | ✅ | `indicators/`, `test_indicators_are_causal` |
| Bull/bear/neutral classification rules | ✅ | `strategy/regime_hedge.py`, `test_regime_rules_on_trends` |
| Base hedge ratios 10/45/80 | ✅ | `RegimeHedgeParams` defaults (changed via the config file) |
| Overheated +10pp / oversold bounce −10pp / 0–85% | ✅ | `test_overbought_and_oversold_adjustments`, clamp |
| 2-bar confirmation | ✅ | `strategy/state_machine.py`, `test_state_machine_requires_two_bars` |
| 20pp per day, no trade below 10pp, no longs, BUY reduce-only, BUY ≤ short, no flip to long | ✅ | `risk/limits.py`, `domain/position.py`, `test_never_long_and_buy_never_exceeds_short` |
| Parameters are initial values, subject to later research | ✅ | `docs/strategy_spec.md` §6, grid results from stage 4 |

## §4 Backtest principles
| Item | Status | Evidence |
|---|---|---|
| No look-ahead; fills at the next candle's open | ✅ | `backtest/engine.py`, `test_signal_executes_on_next_open_never_same_candle`, `test_truncating_future_data_does_not_change_the_past` |
| Separate data for trade price / mark / index / funding / spec / fees | ✅ | `data/market_data.py` (trade·mark·index), `funding_data.py`, `exchange_info.json`, `CostModelConfig` |
| Funding and fee formulas checked against the official docs | ✅ | Fees VERIFIED (cross-checked against the official example). For funding, "÷Mark Price" has not been found in the official text, but it is **VERIFIED (±8.12%) by comparison with an actual settlement on 2026-09-04** — `verify-funding`, `docs/inverse_math.md` §6 |
| Contract specification change history | 🟡 | Cannot be checked → current spec held fixed, limitation stated in `docs/backtest_methodology.md` §7 |
| Maker/Taker/slippage/funding/partial fills/re-placing unfilled orders/rounding | ✅ | `execution_model.py`, `funding_model.py`, `test_missed_rebalance_and_partial_fill_scenarios`, `test_target_contracts_floors…` |
| Conservative Taker default | ✅ | `use_maker: false` |
| 7 sensitivity scenarios (fees ×2, slippage ×3, adverse funding, 1-day delay, missed orders …) | ✅ | `backtest/scenarios.py`, `reports/v1_reference/sensitivity.csv` |

## §5 Benchmarks
| Item | Status | Evidence |
|---|---|---|
| HODL / 100% / 50% / EMA200 / dynamic | ✅ | `strategy/benchmarks.py`, `backtest/runner.py::default_benchmarks` |
| 21 reported metrics (starting and final BTC … by bull/bear/sideways market) | ✅ | `backtest/metrics.py::summarize` (including regime_performance), `benchmark_comparison.csv` |
| BTC grows but USD collapses → failure verdict | ✅ | `metrics.verdict` (FAIL/WEAK/PASS); the validation-period FAIL is reported as is |
| No hiding of bad results, no arbitrary parameter changes | ✅ | The stage 4 report records every FAIL fold and start date; no parameter changes after OOS |

## §6 Overfitting prevention
| Item | Status | Evidence |
|---|---|---|
| Chronological 60/20/20 + Walk-forward | ✅ | `backtest/walkforward.py`, `test_folds_are_chronological_and_non_overlapping_tests` |
| Limited Grid, Nested WF, parameter minimisation, plateau preference | ✅ | 72 combinations, `neighbourhood_scores`, `test_neighbourhood_scores_average_adjacent_combos` |
| Check for concentration in particular market conditions | ✅ | Start-date dependence and per-regime performance (`research_report.md` §4, §2) |
| Experiment configs and result files saved, seed, run time, code version, config hash | ✅ | `config/loader.py::RunMetadata`, `research_summary.json`, `summary.json` |
| ML excluded | ✅ | — |

## §7 Asset accounting and ledger
| Item | Status | Evidence |
|---|---|---|
| Items recorded separately (starting BTC, deposits/withdrawals, balance, realized, unrealized, fees, funding, Equity BTC/USD, contract count, average price, mark, target/actual hedge ratio and error, order and fill history) | ✅ | `domain/ledger.py`, `domain/portfolio.py::snapshot`, `storage/models.py` (orders, fills, ledger, snapshots, decisions) |
| Deposits/withdrawals not counted as profit | ✅ | `Ledger.net_btc_growth`, `test_external_flows_are_not_trading_pnl`, `test_unexpected_deposit_is_flagged_and_not_counted_as_pnl` |
| Inverse average entry price (non-arithmetic), lot accounting | ✅ | Harmonic mean `combine_entry_price`, FIFO lot audit `test_full_close_lot_pnl_equals_average_pnl` |
| Tests comparing against official exchange results | 🟡 | Fees and notional value cross-checked against official examples; no official example exists for the average price → compared on Testnet against `positionRisk.entryPrice` with a 0.1% tolerance (`Reconciler.compare`) |

## §8 Live trading architecture
| Item | Status | Evidence |
|---|---|---|
| Strategy → Portfolio → Risk → Execution → Adapter separation | ✅ | Package structure; no exchange imports in strategy code |
| Binance COIN-M adapter, latest official docs take precedence | ✅ | `exchange/binance_coinm.py`, `docs/exchange_api_verification.md` (reflects the 2026-06 integration) |
| No guessing at unverified endpoints, unverified items stated explicitly | ✅ | UNVERIFIED items are not used in code (e.g. SAPI permission query); table in §15 of that doc |
| Third-party library verification | ✅ | In-house adapter + tests on official example payloads (`test_binance_adapter.py`) |

## §9 Operating modes
| Item | Status | Evidence |
|---|---|---|
| backtest / shadow / testnet / live | ✅ | `runtime/app.py::run_mode`, `configs/*.yaml` |
| All 11 Live conditions | ✅ | `runtime/live_gate.py`, `test_live_gate_requires_every_condition` |
| API key permission verification | 🟡 | `account.canTrade` is checked. **Whether withdrawal permission is enabled cannot be queried through the COIN-M API** (SAPI is within the unverified scope) → manual check item in the operations Runbook |
| No real orders immediately on launch | ✅ | Read-only if the gate fails; verified by running the CLI (`live.example.yaml` → prints 12 failed items) |

## §10 Account and risk limits
| Item | Status | Evidence |
|---|---|---|
| Sub-account, withdrawals disabled, IP whitelist, One-way, Cross recommended | 🟢 | `docs/risk_management.md` §2, runtime One-way/margin/leverage guards |
| Control based on the effective hedge ratio, 85%/0%/20pp/10pp, no longs / martingale / averaging down / leverage>1 / notional>assets, no orders while the position is unconfirmed | ✅ | Single definition in `RiskLimits`, `validate_order_against_position`, `DuplicateRiskError`, reconciliation |
| No duplicated hardcoding of 85% | ✅ | `grep -rn "0.85" src` → 1 place, the default in `config/schema.py` |

## §11 Order execution safeguards
| Item | Status | Evidence |
|---|---|---|
| Unique client order ID | ✅ | `OrderManager.new_client_order_id` (regex / 36-char limit respected) |
| Timeout → no resend → query → **check User Data Stream** → accepted or not → retry only if not accepted | ✅ **[added after audit]** | `resolve_uncertain` (NOT_PLACED only on REST -2013 from 2 queries + no stream event), `exchange/binance_ws.py::UserDataStream`, `test_lost_response_after_acceptance_does_not_duplicate`, `test_timeout_before_acceptance…` |
| idempotency / partial fills / cancelling unfilled orders / duplicate prevention / restart recovery / reconciliation | ✅ | 11 tests in `test_order_manager.py`, `test_restart_recovers_position_and_continues` |
| Network reconnection | ✅ **[added after audit]** | REST backoff + automatic WS reconnection (`test_user_data_stream_reconnects_after_disconnect…`) |
| Rate limit / exponential backoff / clock skew / response validation | ✅ | `test_rate_limit_backoff_and_ban`, `test_timestamp_error_resyncs…`, type validation in `_request_list` |
| Preventing opposite-direction orders / BUY reduce-only / re-checking the position after an order | ✅ | `BUY_EXCEEDS_SHORT`, reduce-only enforced in `PaperExchange`, post-trade verification in `Trader._execute` |
| Execution policy interface (Market / Maker-first / TWAP) | ✅ | `execution/policies.py`, per-policy tests |

## §12 Failure handling
| Trigger | Status | Evidence |
|---|---|---|
| 7-step fail-safe (no close-out, position kept) | ✅ | `Trader._engage`, `KillSwitch`, `test_position_mismatch_engages_safe_hold_and_keeps_position` |
| Stale price/mark | ✅ | `check_mark_freshness`, `check_candle_freshness`, `test_stale_mark_price_blocks_trading` |
| Prolonged WebSocket outage | ✅ **[added after audit]** | `check_stream_silence` (`ws_max_silence_seconds`) |
| REST vs WS position mismatch | ✅ **[added after audit]** | `check_ws_position` (ACCOUNT_UPDATE `pa` vs positionRisk) |
| Exchange vs DB mismatch / unknown orders / short>limit / BUY>short / long / balance / repeated rejections / clock skew / spec change / maintenance margin / unexpected deposits/withdrawals | ✅ | `risk/guards.py`, `execution/reconciliation.py`, simulation tests |

## §13 Tech stack
| Item | Status | Evidence |
|---|---|---|
| Python 3.12, Pydantic Settings, HTTPX, WebSocket client, pandas/NumPy, SQLAlchemy, PostgreSQL/SQLite, Alembic, Pytest, Ruff, Mypy, Docker, Compose, JSON logging | ✅ | `pyproject.toml`, `alembic.ini`+`migrations/`, `Dockerfile`, `docker-compose.yml`; websockets actually used, **[added after audit]** |
| Docker image build verification | 🟡 | No Docker daemon in the development environment → build unverified (syntax checked only) |
| Code in English / docs in Korean (all docs translated to English on 2026-11-12 at the owner's request) / keys not committed / .env.example / log masking | ✅ | `.gitignore`, `.env.example`, `monitoring/logging.py`, `test_logging_masks_secrets` |

## §14 Project structure
| Item | Status | Evidence |
|---|---|---|
| Required directories and files | ✅ | Same as the required list, plus `runtime/`, `backtest/{runner,scenarios,walkforward}.py`, `exchange/{paper,binance_ws}.py`; the listing in `docs/architecture.md` corrected to match the actual tree **[added after audit]** |
| Preserve existing code | — | No existing repository (empty workspace) |

## §15 CLI
| Command | Status |
|---|---|
| backtest / shadow / testnet / live --confirm-live / status / reconcile / safe-hold | ✅ (`test_cli_parser_exposes_all_commands`, verified by running) |
| Additional: research walk-forward/sensitivity, data download/verify/quality | ✅ |

## §16 Monitoring
| Item | Status | Evidence |
|---|---|---|
| 22 metrics | ✅ **[added after audit]** | `monitoring/metrics.py::MetricsSnapshot`; added population of `last_price` (ticker/price), `daily_pnl_btc/usd`, `websocket_connected` |
| 11 immediate alerts | ✅ | order_submitting/filled/partial/failed, safe_hold_engaged (covering mismatch, limits, data, WS, deposits/withdrawals, long risk), system_start/stop, live_gate |
| Prolonged partial fill | 🟡 | Alert on the unfilled remainder when the policy ends (`order_partial`); Maker-first wait time capped by `maker_wait_seconds` |
| Telegram/Email, alerting failures do not halt trading | ✅ | `monitoring/alerts.py` (thread isolation, exceptions swallowed) |

## §17 Development order · §18 Required tests · §19 Reports · §20 Completion criteria
| Item | Status | Evidence |
|---|---|---|
| 7-stage order; per-stage reporting of tests/types/lint/examples/problems/risks | 🟢 | 8 per-stage commits + reports to the user; the "list of changed files" was given only for the early stages → replaced by the git commit history |
| §18: 24 required tests | ✅ | All present (see the §2–§12 tables for the item-by-item mapping), 119 tests in total |
| §19: 11 report files + 9 charts | ✅ | `reports/v1_reference/`, `test_report_bundle_writes_every_required_file`, `test_cli_backtest_writes_all_outputs_and_is_reproducible` |
| §20: 15 completion criteria | ✅/🟡 | 2 unverified: smoke test connecting to the real exchange (environment constraint), Docker build |

## §21 Way of working — checklist for a good result
| Check | Result |
|---|---|
| Look-ahead | Blocked structurally + 2 tests |
| Missing fees/funding | Included; present in the sensitivity table |
| Inverse math and contract quantities | Cross-checked against official examples |
| Survivorship bias | Single symbol (BTCUSD_PERP), entire period since listing used; not applicable |
| Overfitting / tuning and evaluating on the same data | Chronological split and WF; no changes after OOS |
| Dependence on a particular period | **Confirmed (depends on the 2022 bear market)** — stated in the report |
| Unrealistic fill prices | Next open + slippage + Taker |
| Rounding error | Contracts rounded down to integers, tested |

## V2 research (added afterwards, 2026-09-02)
| Item | Status | Evidence |
|---|---|---|
| Causality of funding features (no look-ahead) | ✅ | `data/funding_features.py`, `tests/unit/test_funding_features.py` (14 tests: future data has no effect, no leakage from the next day's settlement) |
| Live/backtest signal equivalence | ✅ | `tests/simulation/test_trader_simulation.py::test_trader_runs_the_funding_guard_with_the_same_features_as_the_backtest` (rtol 1e-12) |
| Original principles unchanged (no longs, 85% cap, reduce-only, 20pp/10pp) | ✅ | The guard only ever **decreases** `raw_target`; risk and execution layers unchanged — `docs/strategy_v2.md` §1 |
| Pre-registered evaluation rules followed (no retrofitting to OOS) | ✅ | `docs/strategy_v2.md` §5, full-period performance excluded from the grounds for adoption |
| Verdict: funding guard | 🟡 OFF by default | Restoring −1bp FAIL→PASS succeeded, but only 0.2% of the actual gain is attributable to funding → judged to be directional noise |
| Verdict: dynamic regime V1 | ❌ Rejected | Static `fixed_50` comes out ahead on every axis: full period, OOS, walk-forward, trade count — `docs/strategy_v2.md` §6.1 |
| Mark-price approximation at funding settlement eliminated | ✅ | `data/settlement_marks.py` — uses the open of the 8h mark bars downloaded with `--include-8h-mark` as the 2nd-priority source (share approximated: 53.2% → 0%), 10 tests in `tests/unit/test_settlement_marks.py`. Without the dataset, behaviour is **exactly the same** as before |
| Honest reporting of that change's impact on the results | ✅ | Measured on 3,530 settlements in the period where the approximation applied (2020-2023): cumulative funding error **+0.2352%** (the approximation overstated it). Backtest delta `fixed_50` −0.11%, `regime_v1` −0.97%. Verdicts and walk-forward win rates unchanged, OOS figures exactly identical — `docs/backtest_methodology.md` §6-2 |
| Self-correction of the wrong initial impact estimate | ✅ | Estimated at +0.0096% from only the calm period that had true values (2023-11+), but the actual figure was **25×** that. Stated in the doc as a ⚠️ correction block, not hidden |

## V4 control-arm tests (added afterwards, 2026-09-02)
| Item | Status | Evidence |
|---|---|---|
| No confidence intervals on win rates → an error resulted | ✅ Fixed | The Wilson 95% CI of every reported win rate contains 0.5. 1 supporting argument in `docs/strategy_v2.md` §6.1 **withdrawn**. `backtest/statistics.py` + 17 tests |
| No control arm (null) | ✅ Introduced | `strategy/controls.py` — bar shuffle, block shuffle, uniform, coin flip. `scripts/control_arm_study.py` with 200 seeds |
| No multiple-comparison correction | ✅ Introduced | Šidák; 15 tests → α=0.00341. **0 significant after correction** |
| Cost confound in the control arm itself found and fixed | ✅ | The bar shuffle traded 6.61× as much as the real arm (1,607 vs 243) → 69% of the gap was fees. Corrected to 1.09× with the run-level `BlockShuffleControl` |
| Verdict against the fair control arm | ❌ No timing information | `regime_v1` final BTC at the 41st percentile (p=0.592) — worse than the median of random rearrangements of its own signal |
| Power limitation admitted | ✅ Stated | With 200 seeds the p floor is 0.00498 > corrected α 0.00341 → **corrected significance undetectable in principle**. `docs/strategy_v4_control_arms.md` §4 |

## Remaining unverified items and limitations (summary)
1. ~~Smoke test with a real connection to the demo/real server not yet run~~ → **completed 2026-09-02** (order submission and fill, WS streams, `docs/exchange_api_verification.md` §16).
2. ~~Funding notional-value formula: official COIN-M text PARTIAL~~ → **2026-09-04 measured live, VERIFIED (±8.12%)**. The official text is still not found, but the comparison against an actual settlement is complete — `docs/inverse_math.md` §6. A small error within 8% is not ruled out (improves with a larger position).
3. API key withdrawal permission is checked manually.
4. Docker image build unverified.
5. Current VIP0 fee rate and BNB discount not confirmed (queried at runtime).
6. ~~WebSocket verified only by tests against a local server~~ → **2026-09-02 connection to the real server confirmed** (`user_data`, `mark_price`).
6-1. **New UNVERIFIED**: the demo does not provide `fundingRate` history, and demo and production rates differ (2.9× observed). Not stated in the official docs.
7. V2–V4 results rest on 1 snapshot, 2020-08-11 to 2026-09-01 (2,213 daily bars) — a single market cycle, 1 crash sample.
8. The V4 control-arm tests are underpowered at 200 seeds (§V4). This is "no evidence", not "no effect".
