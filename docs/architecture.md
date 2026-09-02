# 아키텍처 (Architecture)

## 1. 목적

BTC를 장기 보유하면서 BTC 담보 COIN-M Inverse 무기한 선물의 **숏 비율(0~85%)** 을 규칙 기반으로
점진 조절한다. 성과는 항상 **BTC 수량 기준**과 **USD 가치 기준**을 동시에 평가한다.

## 2. 계층 구조

```
Strategy  →  Portfolio Engine  →  Risk Engine  →  Execution Engine  →  Exchange Adapter
(목표 헤지율)   (자산·원장·포지션)     (한도·가드·킬스위치)   (주문 관리·리밸런스·정합성)   (Binance COIN-M REST+WS / Paper / Replay)
```

| 계층 | 패키지 | 책임 | 절대 하지 않는 것 |
|---|---|---|---|
| Strategy | `strategy/` | 종료된 일봉 → 시장 상태 → **원시 목표 헤지율** | 주문, 거래소 호출, 자산 계산 |
| Portfolio Engine | `domain/` | Inverse 수학, 포지션 평균가, 원장(입출금 분리), Equity(BTC/USD) | 신호 판단 |
| Risk Engine | `risk/` | 85% 캡, 일 20%p 변경 한도, 10%p 최소 차이, 롱 금지, BUY ≤ 숏, 명목가치 ≤ 자산, SAFE_HOLD | 주문 전송 |
| Execution Engine | `execution/` | 목표 계약수 → 주문(δ), 고유 client order id, 타임아웃/중복 방지, 부분체결, reconciliation | 전략 로직 |
| Exchange Adapter | `exchange/` | REST 호출·서명, WebSocket 스트림(user data / markPrice, 보조 소스), 계약 사양 조회, 응답 검증·정규화, 페이퍼 거래소 | 리스크 판단 |
| Backtest | `backtest/` | 이벤트 루프(종가 신호 → 다음 시가 체결), 비용·펀딩 모델, 보고서 | 미래 데이터 참조 |
| Runtime | `runtime/` | backtest / shadow / testnet / live 모드 오케스트레이션, Live 게이트 | — |
| Storage | `storage/` | SQLAlchemy 모델(주문·체결·원장·스냅샷·상태), SQLite/PostgreSQL | — |
| Monitoring | `monitoring/` | 구조화 JSON 로그(마스킹), 메트릭 스냅샷, 알림(Telegram/Email/Log) | 거래 중단 유발 |

전략 코드는 거래소 API를 **직접 호출하지 않는다**. `exchange/base.py`의 `ExchangeAdapter`
프로토콜만 실행 엔진이 사용하며, 백테스트·Shadow·Testnet·Live 는 어댑터만 바꿔 동일한
Strategy/Risk/Execution 코드를 재사용한다.

## 3. 데이터 흐름 (일 1회 판단)

```
[UTC 00:00 일봉 종료] → 완결 캔들만 로드 → 지표(EMA20/50/200, ATR20)
   → 시장 상태(상승/중립/하락, 2봉 확인) → 원시 목표 헤지율(+과열/과매도 보정)
   → Risk: clamp[0, 0.85] → 일 변경 ±20%p → |목표−현재| < 10%p 이면 거래 없음
   → Portfolio: 목표 계약수 = floor(equity_btc × mark × ratio / contract_value)
   → Execution: δ = 목표 − 현재 (SELL: 숏 증가 / BUY: reduce-only 숏 감소, BUY ≤ 숏)
   → Adapter: 주문(고유 clientOrderId) → 체결 확인 → 포지션 재조회 → DB 기록 → 알림
```

백테스트는 같은 파이프라인을 **다음 캔들 시가**에 체결시키고 슬리피지·수수료·펀딩을 반영한다.

## 4. 상태와 저장

* 내부 상태(포지션·주문·원장·헤지율 이력)는 DB(SQLite 기본, PostgreSQL 운영)에 기록한다.
* 재시작 시 거래소 상태(포지션, 미체결 주문, 잔고)를 조회해 내부 상태와 **reconciliation** 후에만
  정상 루프로 진입한다. 불일치 시 SAFE_HOLD.

## 5. 운영 모드

| 모드 | 데이터 | 주문 | 기본 |
|---|---|---|---|
| backtest | 과거 파일 | 시뮬레이션 | ✔ |
| shadow | 실시간 공개 데이터 | 시뮬레이션(Paper) — 실제 주문 없음 | ✔ |
| testnet | 데모 거래소 | 데모 주문 | 명시적 선택 |
| live | 실거래소 | 실제 주문 — 다중 게이트 통과 시에만 | 기본 OFF |

## 6. 주요 설계 결정

1. **Decimal**: 주문·자산·수수료·펀딩·계약수는 Decimal. 지표는 NumPy/pandas float.
2. **계약 사양 비고정**: `contractSize`, `stepSize`, `tickSize` 는 exchangeInfo에서 읽는다.
3. **85% 캡 단일 정의**: `config/schema.py::RiskLimits` 한 곳. 코드 다른 곳에 숫자를 두지 않는다.
4. **숏 전용**: 포지션 모델이 롱 전환을 예외로 차단하고, 리스크 검증이 BUY > 숏을 거부한다.
5. **Fail-safe = 포지션 유지**: 장애 시 신규 주문 중단·미체결 취소·현재 포지션 유지(SAFE_HOLD).
6. **재현성**: 설정 해시·코드 커밋·seed·실행 시각을 모든 보고서에 기록한다.

## 7. 디렉터리

```
src/btc_inventory_quant/
  config/      schema.py(모든 설정) settings.py(환경변수) loader.py(YAML+해시)
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
