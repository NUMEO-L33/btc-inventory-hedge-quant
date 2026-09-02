# 요구사항 추적 매트릭스 (Requirements Audit)

작성일 2026-09-02. 원 요구사항(21개 절)의 항목별 구현 상태를 코드·테스트 근거와 함께 기록한다.
상태: ✅ 구현+자동 테스트 / 🟢 구현(테스트는 상위 테스트로 간접 검증) / 🟡 부분 구현 또는 문서상 한계 / ❌ 미구현.

> 이 매트릭스는 "한 번에 진행하며 느슨해진 부분이 없는가"라는 검토 요청에 따라 작성되었고, 감사에서 발견된 누락 7건(WebSocket, Shadow 펀딩, 시작 즉시 판단, 지표 미기입, 통합 테스트 부재, 문서 불일치, API 권한 검증 범위)은 아래에 **[감사 후 보강]** 으로 표시했다.

## §1 전략의 경제적 구조
| 항목 | 상태 | 근거 |
|---|---|---|
| BTC 기본 자산, COIN-M 숏으로 0~85% 헤지 | ✅ | `risk/limits.py`, `execution/rebalancer.py`, `test_risk_limits_config.py` |
| 100% 헤지 = 합성 달러 구조 | ✅ | `test_inverse_math.py::test_case_1/2`, `test_backtest_engine.py::test_case_1_and_2_reproduced_by_engine` |
| ON/OFF 아닌 점진 조절 | ✅ | 일 20%p 캡·10%p 최소차 (`test_daily_cap_and_min_diff_are_enforced`) |

## §2 Inverse 계약 수학
| 항목 | 상태 | 근거 |
|---|---|---|
| PnL / 미실현 / 목표 명목 / 목표 계약수 / 유효 헤지율 공식 | ✅ | `domain/inverse_contract.py`, `test_inverse_math.py` |
| 계약 사양을 코드에 고정하지 않음 | ✅ | `ContractSpec.from_exchange_info_symbol`, `data/repositories.py::load_contract_spec`, 런타임 `adapter.contract_spec` + `CONTRACT_SPEC_CHANGED` 가드 |
| 주문·자산·수수료·계약수·펀딩은 Decimal | ✅ | 도메인 전 모듈 Decimal; 지표만 float |
| 테스트 1~4 | ✅ | `test_case_1_price_halves…`, `test_case_2…`, `test_case_3_85pct…`, `test_case_4_buy_exceeding_short…` |

## §3 초기 전략 V1
| 항목 | 상태 | 근거 |
|---|---|---|
| 일봉·UTC·완결 캔들만 | ✅ | `data/market_data.py`(close_time < as_of), `runtime/trader.py::completed_candles`, `test_incomplete_last_candle_is_dropped` |
| EMA20/50/200, ATR20 | ✅ | `indicators/`, `test_indicators_are_causal` |
| 상승/하락/중립 판정 규칙 | ✅ | `strategy/regime_hedge.py`, `test_regime_rules_on_trends` |
| 기본 헤지율 10/45/80 | ✅ | `RegimeHedgeParams` 기본값(설정 파일로 변경) |
| 과열 +10%p / 과매도 반등 −10%p / 0~85% | ✅ | `test_overbought_and_oversold_adjustments`, clamp |
| 2봉 확인 | ✅ | `strategy/state_machine.py`, `test_state_machine_requires_two_bars` |
| 일 20%p, 10%p 미만 무거래, 롱 금지, BUY reduce-only, BUY ≤ 숏, 롱 전환 금지 | ✅ | `risk/limits.py`, `domain/position.py`, `test_never_long_and_buy_never_exceeds_short` |
| 파라미터는 초기값·이후 연구 | ✅ | `docs/strategy_spec.md` §6, 4단계 그리드 결과 |

## §4 백테스트 원칙
| 항목 | 상태 | 근거 |
|---|---|---|
| look-ahead 금지, 다음 캔들 시가 체결 | ✅ | `backtest/engine.py`, `test_signal_executes_on_next_open_never_same_candle`, `test_truncating_future_data_does_not_change_the_past` |
| 체결가/마크/인덱스/펀딩/사양/수수료 데이터 분리 | ✅ | `data/market_data.py`(trade·mark·index), `funding_data.py`, `exchange_info.json`, `CostModelConfig` |
| 펀딩·수수료 공식 공식 문서 확인 | 🟡 | 수수료 VERIFIED(공식 예시 검산 테스트), 펀딩 "÷Mark Price" COIN-M 원문 미발견 → PARTIAL, `Reconciler.verify_funding_formula` 로 실측 역검증 |
| 계약 사양 변경 이력 | 🟡 | 확인 불가 → 현재 사양 고정, `docs/backtest_methodology.md` §7 한계 명시 |
| Maker/Taker/슬리피지/펀딩/부분체결/미체결 재주문/반올림 | ✅ | `execution_model.py`, `funding_model.py`, `test_missed_rebalance_and_partial_fill_scenarios`, `test_target_contracts_floors…` |
| 보수적 Taker 기본 | ✅ | `use_maker: false` |
| 민감도 7종(수수료×2, 슬리피지×3, 불리한 펀딩, 1일 지연, 주문 누락 …) | ✅ | `backtest/scenarios.py`, `reports/v1_reference/sensitivity.csv` |

## §5 비교 기준
| 항목 | 상태 | 근거 |
|---|---|---|
| HODL / 100% / 50% / EMA200 / 동적 | ✅ | `strategy/benchmarks.py`, `backtest/runner.py::default_benchmarks` |
| 보고 지표 21종 (시작·최종 BTC … 상승/하락/횡보별) | ✅ | `backtest/metrics.py::summarize` (regime_performance 포함), `benchmark_comparison.csv` |
| BTC 증가·USD 붕괴 → 실패 판정 | ✅ | `metrics.verdict` (FAIL/WEAK/PASS), 검증구간 FAIL 그대로 보고 |
| 나쁜 결과 은폐·임의 파라미터 변경 금지 | ✅ | 4단계 보고서에 FAIL 폴드·시작일 전부 기록, OOS 후 파라미터 변경 없음 |

## §6 과최적화 방지
| 항목 | 상태 | 근거 |
|---|---|---|
| 시간순 60/20/20 + Walk-forward | ✅ | `backtest/walkforward.py`, `test_folds_are_chronological_and_non_overlapping_tests` |
| 제한 Grid, Nested WF, 파라미터 최소화, plateau 선호 | ✅ | 72조합, `neighbourhood_scores`, `test_neighbourhood_scores_average_adjacent_combos` |
| 특정 장세 집중 확인 | ✅ | 시작일 의존성·상태별 성과 (`research_report.md` §4, §2) |
| 실험 설정·결과 파일 저장, seed, 실행시각·코드버전·설정 해시 | ✅ | `config/loader.py::RunMetadata`, `research_summary.json`, `summary.json` |
| ML 제외 | ✅ | — |

## §7 자산 회계·원장
| 항목 | 상태 | 근거 |
|---|---|---|
| 항목 분리 기록(시작 BTC, 입출금, 잔액, 실현, 미실현, 수수료, 펀딩, Equity BTC/USD, 계약수, 평균가, 마크, 목표/실제 헤지율·오차, 주문·체결 이력) | ✅ | `domain/ledger.py`, `domain/portfolio.py::snapshot`, `storage/models.py`(orders, fills, ledger, snapshots, decisions) |
| 입출금을 수익으로 계산하지 않음 | ✅ | `Ledger.net_btc_growth`, `test_external_flows_are_not_trading_pnl`, `test_unexpected_deposit_is_flagged_and_not_counted_as_pnl` |
| Inverse 평균 진입가(비산술), lot 회계 | ✅ | 조화평균 `combine_entry_price`, FIFO lot 감사 `test_full_close_lot_pnl_equals_average_pnl` |
| 공식 거래소 결과와 비교 테스트 | 🟡 | 수수료·명목가치는 공식 예시로 검산; 평균가는 공식 예시 부재 → Testnet 에서 `positionRisk.entryPrice` 와 0.1% 허용오차 비교(`Reconciler.compare`) |

## §8 실거래 구조
| 항목 | 상태 | 근거 |
|---|---|---|
| Strategy → Portfolio → Risk → Execution → Adapter 분리 | ✅ | 패키지 구조, 전략 코드에 거래소 import 없음 |
| Binance COIN-M 어댑터, 최신 공식 문서 우선 | ✅ | `exchange/binance_coinm.py`, `docs/exchange_api_verification.md`(2026-06 통합 반영) |
| 미검증 엔드포인트 추정 금지, 미검증 항목 명시 | ✅ | UNVERIFIED 항목은 코드에서 미사용(예: SAPI 권한 조회), 문서 §15 표 |
| 제3자 라이브러리 검증 | ✅ | 자체 어댑터 + 공식 예시 payload 테스트 (`test_binance_adapter.py`) |

## §9 운영 모드
| 항목 | 상태 | 근거 |
|---|---|---|
| backtest / shadow / testnet / live | ✅ | `runtime/app.py::run_mode`, `configs/*.yaml` |
| Live 조건 11개 전부 | ✅ | `runtime/live_gate.py`, `test_live_gate_requires_every_condition` |
| API 키 권한 검증 | 🟡 | `account.canTrade` 확인. **출금 권한 여부는 COIN-M API 로 조회 불가**(SAPI 는 미검증 범위) → 운영 Runbook 수동 확인 항목 |
| 실행 즉시 실주문 금지 | ✅ | 게이트 실패 시 읽기 전용, CLI 실행 검증(`live.example.yaml` → 12개 실패 항목 출력) |

## §10 계좌·리스크 제한
| 항목 | 상태 | 근거 |
|---|---|---|
| 서브계정·출금 금지·IP 화이트리스트·One-way·Cross 권장 | 🟢 | `docs/risk_management.md` §2, 런타임 One-way/margin/leverage 가드 |
| 유효 헤지율 기준 통제, 85%/0%/20%p/10%p, 롱·마틴게일·물타기·레버리지>1·명목>자산 금지, 미확인 포지션 시 주문 금지 | ✅ | `RiskLimits` 단일 정의, `validate_order_against_position`, `DuplicateRiskError`, reconciliation |
| 85% 중복 하드코딩 금지 | ✅ | `grep -rn "0.85" src` → `config/schema.py` 기본값 1곳 |

## §11 주문 실행 안전장치
| 항목 | 상태 | 근거 |
|---|---|---|
| 고유 client order ID | ✅ | `OrderManager.new_client_order_id` (regex/36자 준수) |
| 타임아웃 → 재전송 금지 → 조회 → **User Data Stream 확인** → 접수 여부 → 미접수 시만 재시도 | ✅ **[감사 후 보강]** | `resolve_uncertain`(REST -2013 2회 + 스트림 이벤트 없음일 때만 NOT_PLACED), `exchange/binance_ws.py::UserDataStream`, `test_lost_response_after_acceptance_does_not_duplicate`, `test_timeout_before_acceptance…` |
| idempotency / 부분체결 / 미체결 취소 / 중복 방지 / 재시작 복구 / reconciliation | ✅ | `test_order_manager.py` 11개, `test_restart_recovers_position_and_continues` |
| 네트워크 재연결 | ✅ **[감사 후 보강]** | REST 백오프 + WS 자동 재연결(`test_user_data_stream_reconnects_after_disconnect…`) |
| Rate limit / 지수 backoff / 시간 오차 / 응답 검증 | ✅ | `test_rate_limit_backoff_and_ban`, `test_timestamp_error_resyncs…`, `_request_list` 타입 검증 |
| 반대 방향 주문 방지 / BUY reduce-only / 주문 후 포지션 재확인 | ✅ | `BUY_EXCEEDS_SHORT`, `PaperExchange` reduce-only 강제, `Trader._execute` post-trade 검증 |
| 실행 정책 인터페이스(Market / Maker-first / TWAP) | ✅ | `execution/policies.py`, 정책별 테스트 |

## §12 장애 대응
| 트리거 | 상태 | 근거 |
|---|---|---|
| Fail-safe 7단계(청산 금지, 포지션 유지) | ✅ | `Trader._engage`, `KillSwitch`, `test_position_mismatch_engages_safe_hold_and_keeps_position` |
| 가격/마크 stale | ✅ | `check_mark_freshness`, `check_candle_freshness`, `test_stale_mark_price_blocks_trading` |
| WebSocket 장시간 중단 | ✅ **[감사 후 보강]** | `check_stream_silence` (`ws_max_silence_seconds`) |
| REST vs WS 포지션 불일치 | ✅ **[감사 후 보강]** | `check_ws_position` (ACCOUNT_UPDATE `pa` vs positionRisk) |
| 거래소 vs DB 불일치 / 미지 주문 / 숏>한도 / BUY>숏 / 롱 / 잔액 / 반복 거부 / 시간 오차 / 사양 변경 / 유지증거금 / 예상외 입출금 | ✅ | `risk/guards.py`, `execution/reconciliation.py`, 시뮬레이션 테스트 |

## §13 기술 스택
| 항목 | 상태 | 근거 |
|---|---|---|
| Python 3.12, Pydantic Settings, HTTPX, WebSocket client, pandas/NumPy, SQLAlchemy, PostgreSQL/SQLite, Alembic, Pytest, Ruff, Mypy, Docker, Compose, JSON 로깅 | ✅ | `pyproject.toml`, `alembic.ini`+`migrations/`, `Dockerfile`, `docker-compose.yml`; websockets 는 **[감사 후 보강]** 으로 실제 사용 |
| Docker 이미지 빌드 검증 | 🟡 | 개발 환경에 Docker 데몬 없음 → 빌드 미검증(구문만 확인) |
| 코드 영어 / 문서 한국어 / 키 미커밋 / .env.example / 로그 마스킹 | ✅ | `.gitignore`, `.env.example`, `monitoring/logging.py`, `test_logging_masks_secrets` |

## §14 프로젝트 구조
| 항목 | 상태 | 근거 |
|---|---|---|
| 요구 디렉터리·파일 | ✅ | 요구 목록과 동일 + `runtime/`, `backtest/{runner,scenarios,walkforward}.py`, `exchange/{paper,binance_ws}.py` 추가; `docs/architecture.md` 목록 실제와 일치하도록 수정 **[감사 후 보강]** |
| 기존 코드 보존 | — | 기존 저장소 없음(빈 작업공간) |

## §15 CLI
| 명령 | 상태 |
|---|---|
| backtest / shadow / testnet / live --confirm-live / status / reconcile / safe-hold | ✅ (`test_cli_parser_exposes_all_commands`, 실행 검증) |
| 추가: research walk-forward/sensitivity, data download/verify/quality | ✅ |

## §16 모니터링
| 항목 | 상태 | 근거 |
|---|---|---|
| 지표 22종 | ✅ **[감사 후 보강]** | `monitoring/metrics.py::MetricsSnapshot`; `last_price`(ticker/price), `daily_pnl_btc/usd`, `websocket_connected` 기입 추가 |
| 즉시 알림 11종 | ✅ | order_submitting/filled/partial/failed, safe_hold_engaged(불일치·한도·데이터·WS·입출금·롱 위험 포함), system_start/stop, live_gate |
| 부분체결 장기 지속 | 🟡 | 정책 종료 시 미체결 잔량 알림(`order_partial`); Maker-first 대기시간은 `maker_wait_seconds` 로 상한 |
| Telegram/Email, 알림 장애가 거래를 중단시키지 않음 | ✅ | `monitoring/alerts.py`(스레드 격리·예외 흡수) |

## §17 개발 순서 · §18 필수 테스트 · §19 보고서 · §20 완료 기준
| 항목 | 상태 | 근거 |
|---|---|---|
| 7단계 순서, 단계별 테스트/타입/린트/예제/문제/위험 보고 | 🟢 | 단계별 커밋 8개 + 사용자 보고; "변경 파일 목록"은 초기 단계만 명시 → git 커밋 이력으로 대체 |
| §18 필수 테스트 24종 | ✅ | 모두 존재(항목별 대응은 §2~§12 표 참조), 총 119개 테스트 |
| §19 보고서 파일 11종 + 그래프 9종 | ✅ | `reports/v1_reference/`, `test_report_bundle_writes_every_required_file`, `test_cli_backtest_writes_all_outputs_and_is_reproducible` |
| §20 완료 기준 15항 | ✅/🟡 | 미검증 2건: 실거래소 접속 스모크(환경 제약), Docker 빌드 |

## §21 작업 방식 — 좋은 결과 검사 목록
| 검사 | 결과 |
|---|---|
| Look-ahead | 구조적 차단 + 테스트 2종 |
| 수수료·펀딩 누락 | 반영, 민감도 표에 존재 |
| Inverse 수학·계약 수량 | 공식 예시 검산 |
| 생존자 편향 | 단일 심볼(BTCUSD_PERP)·상장 이후 전 구간 사용, 해당 없음 |
| 과최적화·동일 데이터 튜닝/평가 | 시간순 분리·WF, OOS 후 변경 없음 |
| 특정 기간 의존 | **확인됨(2022 하락장 의존)** — 보고서에 명시 |
| 비현실적 체결가 | 다음 시가 + 슬리피지 + Taker |
| 반올림 오차 | 정수 계약 내림, 테스트 |

## V2 연구 (사후 추가, 2026-09-02)
| 항목 | 상태 | 근거 |
|---|---|---|
| 펀딩 피처 인과성(look-ahead 금지) | ✅ | `data/funding_features.py`, `tests/unit/test_funding_features.py` (14종, 미래 데이터 무영향·다음날 정산 누출 없음) |
| 라이브/백테스트 신호 동등성 | ✅ | `tests/simulation/test_trader_simulation.py::test_trader_runs_the_funding_guard_with_the_same_features_as_the_backtest` (rtol 1e-12) |
| 원래 원칙 불변(롱 금지·85% 캡·reduce-only·20%p/10%p) | ✅ | 가드는 `raw_target` 을 **감소**만 시킴, 리스크·실행 계층 무변경 — `docs/strategy_v2.md` §1 |
| 사전 등록 평가 규칙 준수(OOS 되돌려 맞추기 금지) | ✅ | `docs/strategy_v2.md` §5, 채택 근거에서 전체 기간 성적 배제 |
| 판정: 펀딩 가드 | 🟡 기본 OFF | −1bp FAIL→PASS 복구는 성공, 그러나 실이득의 0.2%만 펀딩 귀속 → 방향성 잡음으로 판정 |
| 판정: 동적 레짐 V1 | ❌ 기각 | 정적 `fixed_50` 이 전체·OOS·워크포워드·거래수 모든 축에서 우위 — `docs/strategy_v2.md` §6.1 |
| 펀딩 정산 시점 마크가격 근사 해소 | ✅ | `data/settlement_marks.py` — `--include-8h-mark` 로 받은 8h 마크 봉의 시가를 2순위 소스로 사용(근사 53.2% → 0%), `tests/unit/test_settlement_marks.py` 10종. 데이터셋 없으면 이전 동작과 **완전 동일** |
| 위 변경의 결과 영향 정직 보고 | ✅ | 근사 적용 구간(2020-2023) 3,530건 실측: 누적 펀딩 오차 **+0.2352%**(근사가 과대계상). 백테스트 델타 `fixed_50` −0.11%, `regime_v1` −0.97%. 판정·워크포워드 승률 불변, OOS 숫자 완전 동일 — `docs/backtest_methodology.md` §6-2 |
| 최초 영향 추정 오류의 자기정정 | ✅ | 참값이 있던 잔잔한 구간(2023-11+)만으로 +0.0096% 라 추정했으나 실제는 **25배**. 문서에 ⚠️ 정정 블록으로 명시, 은폐하지 않음 |

## V4 대조군 검정 (사후 추가, 2026-09-02)
| 항목 | 상태 | 근거 |
|---|---|---|
| 승률에 신뢰구간 부재 → 오류 발생 | ✅ 수정 | 보고한 모든 승률의 Wilson 95% CI 가 0.5 포함. `docs/strategy_v2.md` §6.1 근거 1건 **철회**. `backtest/statistics.py` + 테스트 17종 |
| 대조군(null) 부재 | ✅ 도입 | `strategy/controls.py` — 봉셔플·블록셔플·균등·동전. `scripts/control_arm_study.py` 시드 200 |
| 다중비교 보정 부재 | ✅ 도입 | Šidák; 15개 검정 → α=0.00341. **보정 후 유의 0건** |
| 대조군 자체의 비용 교란 발견·수정 | ✅ | 봉셔플이 실제보다 6.61배 거래(1,607 vs 243) → 격차의 69%가 수수료. 런 단위 `BlockShuffleControl` 로 1.09배로 교정 |
| 공정 대조군 판정 | ❌ 타이밍 정보 없음 | `regime_v1` 최종 BTC 41 퍼센타일(p=0.592) — 자기 신호 무작위 재배열의 중앙값보다 못함 |
| 검정력 한계 자백 | ✅ 명시 | 시드 200의 p 하한 0.00498 > 보정 α 0.00341 → **원리적으로 보정 유의 탐지 불가**. `docs/strategy_v4_control_arms.md` §4 |

## 남은 미검증·한계 (요약)
1. 데모/실서버 실접속 스모크 테스트 미실행(개발 환경 네트워크 제약).
2. 펀딩 명목가치 공식 COIN-M 원문 PARTIAL → 실측 역검증 필요.
3. API 키 출금 권한은 수동 확인.
4. Docker 이미지 빌드 미검증.
5. 현재 VIP0 수수료율·BNB 할인 미확인(런타임 조회).
6. WebSocket 은 로컬 서버 테스트로만 검증(실서버 미검증).
7. V2~V4 결과는 2020-08-11~2026-09-01 스냅샷 1개(2,213 일봉)에 기반 — 단일 시장 사이클, 폭락 표본 1개.
8. V4 대조군 검정은 시드 200으로 검정력이 부족하다(§V4). "효과 없음"이 아니라 "증거 없음"이다.
