# 리스크 관리

모든 수치 한도는 `configs/*.yaml` 의 `risk` 섹션(`config/schema.py::RiskLimits`) 한 곳에서만 정의한다.

## 1. 포지션 한도

| 한도 | 값 | 강제 위치 |
|---|---|---|
| 최대 유효 헤지율 | 85% (+3%p 드리프트 허용 후 SAFE_HOLD) | `risk/limits.py::clamp_hedge_ratio`, `validate_order_against_position`, `risk/guards.py::check_hedge_cap` |
| 최소 헤지율 | 0% | 동상 |
| 일 최대 헤지율 변경 | 20%p | `apply_daily_change_cap` |
| 최소 리밸런싱 차이 | 10%p | `rebalance_needed` |
| 선물 롱 | 금지 | `InversePosition(allow_long=False)`, `RiskLimits.allow_long: Literal[False]`, 가드 `LONG_POSITION`, reconciliation `LONG_POSITION` |
| BUY 수량 | ≤ 현재 숏 | `validate_reduce_only_buy`, `BUY_EXCEEDS_SHORT`, 리밸런서 캡 |
| 유효 레버리지 | ≤ 1 | `NOTIONAL_EXCEEDS_EQUITY` |
| 숏 명목가치 | ≤ 총자산 USD | 동상 |
| 미확인 주문 존재 시 | 신규 주문 금지 | `OrderManager.submit` → `DuplicateRiskError` |
| 마틴게일·물타기 | 금지 | 목표는 자산 비율에서만 도출(손실 기반 증액 로직 없음) |

## 2. 계좌 구조 권장

* 자동매매 전용 **서브계정**, 다른 선물 포지션·다른 봇·수동매매 없음.
* BTC 이외 담보 없음. API **출금 권한 비활성화**, **IP 화이트리스트**.
* **One-way Position Mode**(통합 이후 UM/CM 공유 설정임에 주의), Cross Margin 기본(런타임 확인).
* 레버리지 UI 값은 1 로 두되, 통제는 유효 헤지율로 한다.

## 3. 데이터·시간 가드

| 조건 | 임계값 | 결과 |
|---|---|---|
| 마크가격 stale | > 120 s | SAFE_HOLD |
| 완결 캔들 stale | > 26 h | SAFE_HOLD |
| 서버 시간 오차 | > 1000 ms | SAFE_HOLD |
| 계약 사양 변경(contractSize/step/tick) | 불일치 | SAFE_HOLD |

## 4. 상태 일치 가드

* 내부 DB vs 거래소: 포지션 계약수, 평균가(0.1%), 지갑 잔고(1e-5 BTC), 미지 주문, 포지션 모드, 캡 → 하나라도 불일치 시 SAFE_HOLD.
* 주문 후 재확인: 체결 후 거래소 포지션이 기대치와 다르면 SAFE_HOLD.
* 예상외 입출금(income TRANSFER)은 원장에 외부 흐름으로 기록하고 SAFE_HOLD 로 운영자 확인을 요구한다.

## 5. 청산 위험

헤지율 ≤ 85%·레버리지 ≤ 1 이면 단일 숏 포지션의 청산가는 이론상 존재하지 않는다(`docs/inverse_math.md` §7). 그래도 유지증거금/자산 비율이 25% 를 넘으면 SAFE_HOLD 하며, 거래소 `positionRisk.liquidationPrice` 를 모니터링 지표로 기록한다.

## 6. Fail-safe 원칙

장애 시 **숏을 전부 청산하지 않는다**(하락 노출을 갑자기 키울 수 있음).

1. 신규 주문 중단 → 2. 미체결 취소 → 3. 포지션 유지 → 4. 거래소 상태 재조회 → 5. DB 와 비교 → 6. 일치 확인 전까지 SAFE_HOLD → 7. 알림.
SAFE_HOLD 는 DB 에 영속되어 재시작해도 유지되며, `safe-hold --release --ack <문자열>` 로만 해제한다.

## 7. Live 게이트 (전부 만족해야 실주문)

`BIQ_ENABLE_LIVE_TRADING=true` · `live.live_trading_enabled: true` · `--confirm-live` · acknowledgement 3중 일치(`I_ACCEPT_COIN_M_TRADING_RISK`) · API 키 존재·권한 · 시간 동기화 · 포지션 일치 · 미확인 주문 없음 · 헤지율 한도 이내 · 시세 정상 · 계약사양 검증 · One-way · 자산 ≤ `max_initial_equity_btc`(소액 가드).
하나라도 실패하면 **읽기 전용(의도만 기록)** 으로 실행된다.
