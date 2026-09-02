# 운영 Runbook

## 0. 사전 준비

```bash
python3.12 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                          # 키 입력, 절대 커밋 금지
python -m btc_inventory_quant data verify                     # 원본 데이터 체크섬
python -m pytest -q                                           # 106+ 테스트
```

## 1. 백테스트

```bash
python -m btc_inventory_quant backtest --config configs/backtest.yaml
python -m btc_inventory_quant research --config configs/backtest.yaml walk-forward
python -m btc_inventory_quant research --config configs/backtest.yaml sensitivity
```
결과: `reports/<run_id>/backtest_report.md`, `summary.json`, CSV, `charts/`.

데이터 갱신(Binance 접속 가능한 머신에서):
```bash
python -m btc_inventory_quant data download --raw-dir data/raw          # 전체 재수집 + manifest(sha256)
python -m btc_inventory_quant data download --raw-dir data/raw --include-8h-mark   # 펀딩 시각 마크가격 정밀화(선택)
```

## 2. Shadow (실데이터 + 가상계좌, 실주문 불가)

```bash
python -m btc_inventory_quant shadow --config configs/shadow.yaml            # 매일 00:05 UTC 판단, 상시 실행
python -m btc_inventory_quant shadow --config configs/shadow.yaml --once     # 1회 사이클
python -m btc_inventory_quant status --config configs/shadow.yaml
```
확인 항목: `state/metrics_shadow.json` 의 헤지율·자산, DB `decisions` 테이블, 알림 로그. 최소 2~4주 관찰 후 다음 단계.

## 3. Testnet(데모)

1. Binance Demo Trading 에서 API 키 발급(공식 FAQ 절차; COIN-M 데모 적용 여부는 PARTIAL → 첫 실행 보고서로 확인).
2. `.env` 에 `BIQ_BINANCE_DEMO_API_KEY/SECRET` 입력.
3. 계정 설정: One-way 모드, BTCUSD_PERP Cross, 레버리지 1.
4. `python -m btc_inventory_quant testnet --config configs/testnet.yaml --once` → 시작 보고서에서 `contract_spec`, `order_recovery`, `portfolio` 항목 확인.
5. 상시 실행 후 `reconcile` 로 상태 일치 확인, `income(FUNDING_FEE)` 로 펀딩 공식 역검증 결과 확인(`docs/inverse_math.md` §6 PARTIAL 해소).

## 4. Live (소액)

1. `configs/live.example.yaml` → `configs/live.yaml`(git 무시) 복사, `live_trading_enabled: true`, `acknowledgement: I_ACCEPT_COIN_M_TRADING_RISK`.
2. `.env`: `BIQ_ENABLE_LIVE_TRADING=true`, `BIQ_LIVE_ACKNOWLEDGEMENT=I_ACCEPT_COIN_M_TRADING_RISK`, 프로덕션 키(출금 불가, IP 화이트리스트).
3. 서브계정 자산 ≤ `max_initial_equity_btc`(기본 0.05 BTC).
4. `python -m btc_inventory_quant live --config configs/live.yaml --confirm-live`
   * 게이트 실패 시 로그 `LIVE GATE FAILED` 와 실패 항목이 출력되고 읽기 전용으로 동작한다.
5. 첫 주문 후 `reconcile` 실행, 거래소 UI 와 대조.

## 5. 일상 점검

| 주기 | 항목 | 명령 |
|---|---|---|
| 매일 00:10 UTC 이후 | 결정 로그·주문·헤지율 오차 | `status` |
| 매일 | 내부/거래소 일치 | `reconcile` |
| 매주 | 펀딩 실측 vs 계산 비율(≈1) | DB `ledger_entries`, income |
| 매주 | 계약 사양 변경·수수료율(`commissionRate`) | 시작 보고서 |
| 매월 | 백테스트 재실행(신규 데이터) | `backtest` |

## 6. 수동 개입

```bash
python -m btc_inventory_quant safe-hold --config configs/live.yaml                      # 즉시 SAFE_HOLD (미체결 취소, 포지션 유지)
python -m btc_inventory_quant safe-hold --config configs/live.yaml --release --ack I_ACCEPT_COIN_M_TRADING_RISK
```
SAFE_HOLD 원인은 `status` 의 `safe_hold_reason` 과 `events` 테이블에서 확인한다. 해제 전 반드시 `reconcile` 이 OK 여야 한다.

## 7. Docker

```bash
docker compose up -d shadow          # shadow 모드 상시 실행
docker compose run --rm backtest     # 백테스트
```

## 8. 로그·비밀

* 로그는 JSON(`BIQ_LOG_JSON=true`), API 키·서명·listenKey 는 자동 마스킹.
* `.env`, `configs/live.yaml`, `state/`, `*.sqlite3` 는 git 에 커밋되지 않는다.
