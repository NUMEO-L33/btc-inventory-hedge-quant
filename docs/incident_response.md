# 장애 대응 (Incident Response)

원칙: **숏 전량 청산은 대응이 아니다.** 신규 주문 중단 → 미체결 취소 → 포지션 유지 → 상태 재조회 → 비교 → 일치 확인 → 알림.

## 1. 자동 SAFE_HOLD 트리거

| 트리거 | 감지 위치 | 코드 |
|---|---|---|
| 가격/마크 stale | `Guards.check_mark_freshness` | STALE_MARK |
| 완결 캔들 stale | `check_candle_freshness` | STALE_CANDLES |
| 거래소 시간 오차 | `check_clock` | CLOCK_SKEW |
| 거래소 포지션 ≠ DB | `Reconciler.compare` | POSITION_CONTRACTS / ENTRY_PRICE / WALLET_BALANCE |
| 미지 주문 | `check_unknown_orders`, `OrderManager.recover` | UNKNOWN_ORDERS |
| 숏 > 한도 | `check_hedge_cap` | HEDGE_CAP_EXCEEDED / LEVERAGE_EXCEEDED |
| BUY > 숏 | `validate_order_against_position` (주문 전 차단) | BUY_EXCEEDS_SHORT |
| 롱 전환 | `check_position`, post-trade 검증 | LONG_POSITION |
| 잔고 불일치 | `Reconciler.compare` | WALLET_BALANCE |
| 반복 주문 거부 | `check_rejects` | REPEATED_REJECTS |
| 계약 사양 변경 | `check_contract_spec` | CONTRACT_SPEC_CHANGED |
| 유지증거금 급증 | `check_margin_buffer` | MARGIN_RATIO |
| 예상외 입출금 | `check_unexpected_transfers` | UNEXPECTED_TRANSFER |
| 주문 결과 불명 | `OrderManager.resolve_uncertain` | order_unresolved |
| 연속 접속 실패(판단일 기준 3일 연속 — 같은 날의 재시도는 1회로 센다) | `Trader.cycle` | exchange_unavailable |
| 알 수 없는 예외 | `Trader.cycle` | unexpected error |

## 2. 대응 절차

### 2.1 SAFE_HOLD 알림 수신
1. `status` 로 원인 확인 (`safe_hold_reason`, `recent_events`).
2. 거래소 UI 에서 포지션·미체결 주문·최근 체결·입출금을 확인.
3. `reconcile` 실행 → 차이 목록 확보.

### 2.2 원인별 조치
* **STALE / CLOCK / 접속 실패**: 네트워크·NTP 확인. 복구되면 `reconcile` OK 확인 후 해제.
* **POSITION_CONTRACTS / WALLET_BALANCE**: 수동 거래·입출금·청산 여부 확인. 원인이 파악되면 (a) 수동 거래분을 원장에 반영하거나 (b) DB 상태를 삭제하고 거래소 상태를 재채택(첫 시작 절차: `app_state.portfolio` 키 삭제 후 재시작 → `adopted_from_exchange`).
* **UNKNOWN_ORDERS**: 이미 취소됨. 누가 냈는지 확인. 다른 봇/수동 거래 차단.
* **order_unresolved**: 거래소에서 `origClientOrderId` 로 주문 상태 확인. 체결됐다면 DB 주문을 `FILLED` 로 갱신하고 `recover` 재실행. 없다면 `NOT_PLACED`.
* **UNEXPECTED_TRANSFER**: 입출금 의도 확인. 원장에는 이미 외부 흐름으로 기록됨(수익 아님).
* **CONTRACT_SPEC_CHANGED**: 새 사양으로 수학 검증 후 `data/raw/exchange_info.json` 갱신 및 테스트 통과 후 재개.
* **HEDGE_CAP_EXCEEDED**: 가격 급등으로 자산이 줄어 비율이 오른 경우 → 해제 후 다음 사이클의 리밸런싱(BUY reduce-only)이 처리. 수동 개입 필요 없음.

### 2.3 해제
```bash
python -m btc_inventory_quant reconcile --config configs/live.yaml    # 반드시 ok=true
python -m btc_inventory_quant safe-hold --config configs/live.yaml --release --ack I_ACCEPT_COIN_M_TRADING_RISK
```

## 3. 재시작 복구 (프로세스 크래시·서버 재부팅)

시작 시 자동 수행: 시간 동기화 → 계약 사양 확인 → 미해결 DB 주문 조회(`recover`) → 거래소 스냅샷 → DB 와 비교 → 불일치 시 SAFE_HOLD. 정상이면 `reconciled` 로 계속 진행. 중복 주문은 write-ahead 기록과 `UNCERTAIN → 조회` 절차로 방지된다(`tests/simulation/test_order_manager.py`).

## 4. 거래소 장애 (5xx, 점검, 429/418)

* GET 은 지수 백오프 재시도, 주문은 재시도하지 않고 조회로 확정.
* 418(밴) 은 즉시 중단·알림. 밴 기간 동안 사이클은 `error` 로 기록되고 3회 연속이면 SAFE_HOLD.
* 점검 종료 후 `reconcile` → 해제.

## 5. 알림 채널 장애

알림 실패는 로깅만 하고 거래 루프를 중단시키지 않는다(`Alerter._send_one`). 실패 횟수는 `Alerter.failures` 에 누적된다.

## 6. 데이터 이상

* 펀딩 레코드 누락(예: 2026-06-30 08:00)은 백테스트에서 "정산 없음"으로 처리하고 품질 보고서에 기록한다.
* 마크/인덱스 괴리 > 2% 일은 품질 노트에 남기며 백테스트에서 제외하지 않는다.
