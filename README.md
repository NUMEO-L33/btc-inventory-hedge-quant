# BTC Inventory Hedge Quant

BTC 를 장기 보유하면서 **BTC 담보 COIN-M Inverse 무기한(BTCUSD_PERP) 숏 비율을 0~85% 사이에서 규칙 기반으로 조절**하는 리서치·실행 시스템입니다.
성과는 항상 **BTC 수량**과 **USD 가치·최대 낙폭**을 동시에 평가하며, BTC 만 늘고 USD 가 무너지는 결과는 성공으로 보지 않습니다.

> **현재 상태(2026-09-02)**: 백테스트·과최적화 검증·Shadow·주문 관리·장애 복구·Live 게이트까지 구현 완료(MVP). 실거래는 **기본 비활성화**이며 다중 게이트를 모두 통과해야만 주문이 나갑니다. 검증 결과 **V1 전략의 타이밍 알파는 확인되지 않았습니다** — 자세한 내용은 [docs/strategy_spec.md](docs/strategy_spec.md) §5 와 `reports/research_v1/research_report.md` 를 보세요.
>
> **V2 연구 결과(2026-09-02)**: 펀딩 가드는 역펀딩 −1bp/8h 의 FAIL 을 PASS 로 복구하지만, 실제 과거 데이터의 개선분은 펀딩 절감이 아닌 방향성 잡음이라 **기본 비활성화**입니다. 같은 실험에서 **동적 레짐(V1)이 정적 고정 헤지보다 모든 축에서 열등함이 확인되어 기각**되었습니다 — [docs/strategy_v2.md](docs/strategy_v2.md).
>
> **V3 연구 결과(2026-09-02)**: 남은 유일한 파라미터인 정적 비율을 0~85% 전수 탐색한 결과 **최적 비율은 존재하지 않습니다.** 낙폭 1%p 당 약 0.33%(180일)의 BTC를 내는 **선형 가격표**이며, 비율 선택은 최적화가 아니라 **위험 선호**입니다. 이 전략은 알파가 아니라 **보험**이고(7폴드 중 하락장 2폴드에서만 이익, 폭등장 폴드에서 BTC −51.5%), 표본에 폭락이 한 번뿐이라 기대가치를 검증할 수 없습니다 — [docs/strategy_v3_static_ratio.md](docs/strategy_v3_static_ratio.md).
>
> **V4 연구 결과(2026-09-02)**: 대조군(무작위·셔플)을 처음 도입한 결과 **다중비교 보정 후 유의미한 것이 하나도 없습니다.** 보정 전 유의했던 2건은 **거래비용 회피 아티팩트**였고(5회 거래 vs 962회), 거래 수를 보존한 공정 대조군에서 `regime_hedge_v1` 의 타이밍은 **자기 신호를 무작위로 재배열한 것의 중앙값보다 못했습니다**(41 퍼센타일). 아울러 이 프로젝트가 보고했던 **모든 승률의 Wilson 95% 구간이 0.5를 포함**하여, V2 §6.1 의 근거 1건을 철회했습니다 — [docs/strategy_v4_control_arms.md](docs/strategy_v4_control_arms.md).

## 1. 핵심 결과 한눈에 (2021-03-10 → 2026-09-01, 실데이터)

| 전략 | 최종 BTC | USD CAGR | USD MDD | 거래 | 판정 |
|---|---|---|---|---|---|
| HODL | 1.000 | +6.4% | −76.7% | 0 | 기준 |
| 고정 50% 숏 | 1.246 | +10.8% | −48.7% | 11 | PASS |
| 고정 100% 숏 | 1.172 | +9.6% | −4.3% | 9 | PASS |
| EMA200 단순 | 1.071 | +7.8% | −50.6% | 165 | PASS |
| 동적 헤지 V1 | 1.109 | +8.5% | −47.1% | 243 | PASS(전기간) / **검증구간 FAIL** |

* V1 의 BTC 순증가 0.109 중 **펀딩 수취가 0.125**, 타이밍 손익은 수수료를 넘지 못함.
* Walk-forward 7폴드 중 BTC 증가 1회 — 다만 Wilson 95% CI [0.026, 0.513] 로 **동전던지기와 구분되지 않습니다**(V4 §2). 펀딩이 −1bp/8h 만 불리해도 FAIL.
* 거래 수를 보존한 공정 대조군에서 V1 의 타이밍은 자기 신호의 무작위 재배열보다 **못했습니다**(41 퍼센타일, V4 §6).
* 일관된 효과는 USD 낙폭 축소와 펀딩 캐리뿐이며 정적 50% 헤지도 같은 효과를 냄.

## 2. 설치

**macOS (Apple Silicon / Intel)** — 한 줄 부트스트랩(Python 3.12 확인·설치, venv, 의존성, 데이터 검증, 테스트):
```bash
cd btc-inventory-hedge-quant
bash scripts/setup_macos.sh
source .venv/bin/activate
```
수동 설치:
```bash
brew install python@3.12                                        # 없을 때만
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                                            # 키는 .env 에만 (커밋 금지)
python -m pytest -q                                             # 119 tests (unit / integration / simulation)
```
Shadow 모드를 로그인 시 자동 실행하려면 `scripts/launchd/com.numeo.btc-inventory-quant.shadow.plist` 의 설치 절차(파일 상단 주석)를 따르세요.

**Windows (PowerShell)**:
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
python -m pytest -q
```

**Linux / Docker**: `python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"` 또는 `docker compose run --rm backtest` / `docker compose up -d shadow`.

## 3. 백테스트 실행

```bash
python -m btc_inventory_quant data verify                       # 원본 데이터 SHA-256 검증
python -m btc_inventory_quant backtest --config configs/backtest.yaml
```
`reports/<run_id>/` 에 `summary.json`, `backtest_report.md`, `trades.csv`, `daily_equity.csv`, `monthly_returns.csv`, `yearly_returns.csv`, `funding_history.csv`, `fee_history.csv`, `hedge_ratio_history.csv`, `benchmark_comparison.csv`, `drawdown.csv`, `sensitivity.csv`, `charts/*.png` 이 생성됩니다.

과최적화 검증(60/20/20 분리, 제한 그리드, Nested walk-forward, 시작일 의존성):
```bash
python -m btc_inventory_quant research --config configs/backtest.yaml walk-forward
```

옵션은 하위 명령 앞/뒤 어느 쪽에 써도 됩니다(`data --raw-dir X verify` = `data verify --raw-dir X`).

전략 파라미터·리스크 한도·비용은 **설정 파일만** 바꾸면 됩니다(`configs/backtest.yaml`).

## 4. 운영 모드

| 모드 | 명령 | 데이터 | 주문 |
|---|---|---|---|
| backtest | `backtest --config configs/backtest.yaml` | 과거 파일 | 시뮬레이션 |
| shadow | `shadow --config configs/shadow.yaml` | 실시간 공개 REST | 페이퍼 계좌(실주문 불가) |
| testnet | `testnet --config configs/testnet.yaml` | 데모 거래소 | 데모 주문 |
| live | `live --config configs/live.yaml --confirm-live` | 실거래소 | **게이트 통과 시에만** |

운영 명령: `status`, `reconcile`, `verify-funding`, `safe-hold [--release --ack I_ACCEPT_COIN_M_TRADING_RISK]`.

Live 게이트(전부 필요): `BIQ_ENABLE_LIVE_TRADING=true` · `live.live_trading_enabled: true` · `--confirm-live` · acknowledgement 3중 일치 · API 키/권한 · 시간 동기화 · 포지션 일치 · 미확인 주문 없음 · 헤지율 한도 · 시세 정상 · 계약사양 검증 · One-way 모드 · 소액 한도.

## 5. 구조

```
Strategy → Portfolio Engine → Risk Engine → Execution Engine → Exchange Adapter
src/btc_inventory_quant/{config,domain,data,indicators,strategy,backtest,exchange,execution,risk,monitoring,storage,runtime,cli}
```
자세한 설명: [docs/architecture.md](docs/architecture.md)

## 6. 문서

| 문서 | 내용 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 계층·데이터 흐름·모드 |
| [docs/strategy_spec.md](docs/strategy_spec.md) | V1 규칙·파라미터·검증 결과·정직한 결론 |
| [docs/strategy_v2.md](docs/strategy_v2.md) | V2 펀딩 가드 연구 — 2×2 실험, 손익 귀속 분해, **동적 레짐 기각** 판정 |
| [docs/strategy_v3_static_ratio.md](docs/strategy_v3_static_ratio.md) | V3 정적 비율 프론티어 — **최적 비율 부재** 판정, 보험 구조 해부, 강건 구간 10~20% |
| [docs/strategy_v4_control_arms.md](docs/strategy_v4_control_arms.md) | V4 대조군 검정 — **무작위 대비 유의성 0건**, 승률 주장 철회, 거래비용 아티팩트 해부 |
| [docs/inverse_math.md](docs/inverse_math.md) | Inverse 계약 수학(손익·평균가·수수료·펀딩·청산) |
| [docs/backtest_methodology.md](docs/backtest_methodology.md) | 데이터·체결 타임라인·비용·지표·과최적화 방지 |
| [docs/risk_management.md](docs/risk_management.md) | 한도·가드·Fail-safe·Live 게이트 |
| [docs/operations_runbook.md](docs/operations_runbook.md) | 설치·백테스트·Shadow·Testnet·Live·점검 |
| [docs/demo_verification_runbook.md](docs/demo_verification_runbook.md) | **Demo 접속 절차 + 펀딩 공식 검증** — 미검증 4건 해소 |
| [docs/incident_response.md](docs/incident_response.md) | SAFE_HOLD 트리거·대응·재시작 복구 |
| [docs/exchange_api_verification.md](docs/exchange_api_verification.md) | Binance COIN-M 공식 문서 검증(VERIFIED/PARTIAL/UNVERIFIED) |
| [docs/requirements_audit.md](docs/requirements_audit.md) | 요구사항 21개 절 → 구현·테스트 추적 매트릭스, 감사 후 보강 내역 |

## 7. 알려진 한계 · 미검증 항목

* 개발 환경에서 Binance 접속이 차단되어 **데모/실서버 실접속 스모크 테스트는 미실행**(어댑터는 공식 문서·모의 HTTP 로 검증). 사용자 환경에서 `testnet --once` 로 확인 필요.
* 펀딩 금액 공식의 COIN-M 전용 원문(÷ Mark Price)은 공식 문서에서 찾지 못함(PARTIAL). Reconciliation 의 `verify_funding_formula` 로 실측 역검증.
* 현재 VIP0 수수료율·COIN-M BNB 할인 여부 미확인 → 런타임 `commissionRate` 조회, 백테스트는 보수적 0.05%/0.02%.
* 계약 사양 변경 이력 미반영(현재 사양 고정), 일봉 해상도 근사(펀딩 시각 마크가격), 슬리피지·부분체결은 모델값.
* WebSocket 스트림(user data / markPrice)은 로컬 서버 테스트로만 검증(실서버 미검증). REST 가 항상 1차 소스이며 스트림은 보조 확인·침묵 감지용.
* 머신러닝 없음(V1 범위 제외).

## 8. 보안

API 키·시크릿은 `.env` 에만 두고 커밋하지 않습니다(`.env.example` 만 제공). 로그는 API Key·Secret·Signature·인증 헤더·listenKey 를 자동 마스킹합니다. 출금 권한 없는 키와 IP 화이트리스트, 전용 서브계정을 권장합니다.

## 9. 면책

이 소프트웨어는 연구·교육 목적이며 수익을 보장하지 않습니다. 백테스트 결과는 과거 데이터에 기반하며 미래 성과를 담보하지 않습니다. 실거래의 모든 책임은 사용자에게 있습니다.
