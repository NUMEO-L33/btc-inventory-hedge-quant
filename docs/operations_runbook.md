# 운영 Runbook

## 0. 사전 준비

```bash
bash scripts/setup_macos.sh                                   # macOS: Python 3.12 확인 → venv → 설치 → 데이터 검증 → 테스트
source .venv/bin/activate
# 수동(모든 OS): python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
cp .env.example .env                                          # 키 입력, 절대 커밋 금지 (setup 스크립트가 자동 생성)
python -m btc_inventory_quant data verify                     # 원본 데이터 체크섬
python -m pytest -q                                           # 119 테스트
```
macOS 상시 실행: `scripts/launchd/com.numeo.btc-inventory-quant.shadow.plist` (파일 상단 설치 절차) → `launchctl load` 후 `tail -f state/shadow.log`.

## 1. 백테스트

```bash
python -m btc_inventory_quant backtest --config configs/backtest.yaml
python -m btc_inventory_quant research --config configs/backtest.yaml walk-forward
python -m btc_inventory_quant research --config configs/backtest.yaml sensitivity
```
결과: `reports/<run_id>/backtest_report.md`, `summary.json`, CSV, `charts/`.

데이터 갱신(Binance 접속 가능한 머신에서):
```bash
python -m btc_inventory_quant data --raw-dir data/raw download          # 전체 재수집 + manifest(sha256)
python -m btc_inventory_quant data --raw-dir data/raw download --include-8h-mark   # 펀딩 시각 마크가격 정밀화(선택, docs/backtest_methodology.md §6-2)
```

## 2. Shadow (실데이터 + 가상계좌, 실주문 불가)

```bash
python -m btc_inventory_quant shadow --config configs/shadow.yaml            # 매일 00:05 UTC 판단, 상시 실행
python -m btc_inventory_quant shadow --config configs/shadow.yaml --once     # 1회 사이클
python -m btc_inventory_quant status --config configs/shadow.yaml
```
상시 실행(macOS, 권장) — 로그인 시 자동 시작하고 죽으면 재시작한다:
```bash
bash scripts/install_shadow_agent.sh              # 설치 + 시작 (설정 사전 점검 포함)
tail -f state/shadow.log                          # 로그
bash scripts/install_shadow_agent.sh --uninstall  # 중지
```
Shadow 는 **API 키가 필요 없다** — 실시간 공개 시세를 읽고 계좌는 시뮬레이션이므로 실주문이 구조적으로 불가능하다.

알림을 기계 밖으로 내보낸다(권장). `.env` 에:
```
BIQ_TELEGRAM_BOT_TOKEN=...      # @BotFather → /newbot
BIQ_TELEGRAM_CHAT_ID=...        # 봇에게 아무 메시지나 보낸 뒤 getUpdates 의 chat.id
```
`configs/shadow.yaml` 은 이미 `alert_channels: [log, telegram]` 이다. 자격증명이 없으면 경고만
남기고 로그 채널로 계속 돈다 — 설치 스크립트가 사전 점검에서 이를 알려준다.

**주간 점검 항목** (`status --config configs/shadow.yaml`):

| 필드 | 정상 | 이상하면 |
|---|---|---|
| `system_state` | `NORMAL` | `SAFE_HOLD` 면 `safe_hold_reason` 확인 |
| `latest_decision.ts` | 최근 24시간 이내 | 오래됐으면 데몬이 멈춘 것 — `state/shadow.log` |
| `latest_reconciliation.ok` | `true` | 내부 원장과 페이퍼 계좌 불일치 |
| `unresolved_orders` | `[]` | 미확인 주문 잔존 |
| `websocket_connected` | `true` | **이 값만으로는 판단하지 말 것** — 죽은 소켓도 `true` 를 보고한다([인시던트](incident_2026-09-05_stream_silence.md)). 로그의 `forcing stream reconnect` 빈도를 함께 본다 |
| `recent_events` | ERROR 없음 | 재연결·시세 지연 반복 여부 |

**스테일 데이터는 더 이상 SAFE_HOLD 를 즉시 걸지 않는다.** 스트림이 조용하면 그 사이클의 주문만
막고(`action: blocked`), 스트림은 스스로 재연결한다. `risk.stale_data_cycles_to_safe_hold`(기본 3)
연속 사이클 동안 회복하지 못하면 그때 래치한다. 배경은 [인시던트
2026-09-05](incident_2026-09-05_stream_silence.md).

SAFE_HOLD 가 걸렸다면 원인을 먼저 확인하고, 해소된 뒤에 해제한다:
```bash
python -m btc_inventory_quant status --config configs/shadow.yaml    # safe_hold_reason
python -m btc_inventory_quant safe-hold --config configs/shadow.yaml \
    --release --ack I_ACCEPT_COIN_M_TRADING_RISK
```

에이전트는 `caffeinate -s` 로 파이썬을 감싸 실행한다. 절전되면 그날의 판단이 덮개를 여는 시각에
실행되어 백테스트 가정(00:05 UTC 체결)이 깨지기 때문이다. 확인:

```bash
ps aux | grep "[c]affeinate"                        # caffeinate -s ... shadow 가 보여야 한다
pmset -g assertions | grep -i -A1 PreventSystemSleep
```

`-s` 는 `PreventSystemSleep` 어서션을 만든다 — `PreventUserIdleSystemSleep` 이 아니다. 후자로
grep 하면 macOS 자체의 "화면 켜져 있을 때" 어서션(powerd)만 보이고 우리 것은 안 보인다.

**이것이 막아주지 못하는 것**: `-s` 는 **전원 연결 중에만** 유효하고(배터리에서는 macOS 가 무시),
**덮개를 닫으면 그래도 잠든다**. 즉 절전 자체를 없앨 수는 없다. 덮개를 닫는 경우는 스트림의 정지
감지가 처리한다 — 깨어난 뒤 강제 재연결하고, 스테일 판정은 래치하지 않는다.

**Shadow 가 알려주는 것과 아닌 것**: 20% 정적 헤지의 *성과*는 백테스트로 이미 안다. 여기서 새로 얻는 정보는 **운영 계층**이다 — 데몬이 몇 주간 살아남는가, 재연결이 되는가, 원장이 어긋나지 않는가. 최소 2~4주 관찰 후 다음 단계.

1주차(2026-09-04 ~ 09-11)는 **운영 버그 3건을 잡았고 전략 관찰은 0일**로 끝났다. 기록은
[docs/incident_2026-09-05_stream_silence.md](incident_2026-09-05_stream_silence.md).

## 3. Testnet(데모)

1. Binance Demo Trading 에서 API 키 발급(공식 FAQ 절차; COIN-M 데모 적용 여부는 PARTIAL → 첫 실행 보고서로 확인).
2. `.env` 에 `BIQ_BINANCE_DEMO_API_KEY/SECRET` 입력.
3. 계정 설정: One-way 모드, BTCUSD_PERP Cross, 레버리지 1. **API 키는 출금 권한 없음·IP 화이트리스트를 거래소 UI에서 직접 확인**(COIN-M API 로는 출금 권한을 조회할 수 없음).
4. `python -m btc_inventory_quant testnet --config configs/testnet.yaml --once` → 시작 보고서에서 `contract_spec`, `order_recovery`, `portfolio` 항목 확인.
5. 상시 실행 후 `reconcile` 로 상태 일치 확인.
6. **펀딩 공식 검증(최우선)**: 정산 시각(00/08/16 UTC)을 넘긴 뒤 `python -m btc_inventory_quant verify-funding --config configs/testnet.yaml`.
   `verdict: VERIFIED` 면 `docs/inverse_math.md` §6 PARTIAL 해소. 상세 절차는 **[docs/demo_verification_runbook.md](demo_verification_runbook.md)**.

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
