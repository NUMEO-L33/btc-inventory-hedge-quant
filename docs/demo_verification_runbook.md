# Demo 접속 · 펀딩 공식 검증 절차

목적: **미검증 4건을 실접속 한 번으로 해소한다.** 특히 ①이 핵심이다.

| # | 항목 | 현재 상태 | 이 절차로 |
|---|---|---|---|
| ① | **펀딩 금액 공식** (`÷ Mark Price`) | **PARTIAL** — COIN-M 원문에 분모 명시 없음 | 실제 정산 1건과 대조 |
| ② | 거래소 실접속 | **0회** | `--once` 스모크 |
| ③ | 실제 수수료율(VIP 등급) | 문서값 사용 | `commissionRate` 조회 |
| ④ | WebSocket 실서버 | 로컬 목만 | 상시 실행 중 확인 |

> **①이 왜 중요한가**: 네 라운드의 연구를 견디고 살아남은 유일한 효과가 **펀딩 캐리**다(`docs/strategy_v4_control_arms.md`). 나머지는 전부 무작위와 구분되지 않았다. 그런데 그 캐리를 계산하는 공식이 미검증이다. **틀렸다면 V1~V4 전체를 다시 계산해야 한다.** 그래서 이건 요식 절차가 아니라 토대 점검이다.

---

## 0. 보안 — 먼저 읽을 것

* **API Key·Secret 은 `.env` 에만 둔다.** 채팅·스크린샷·커밋 어디에도 넣지 않는다. `.env` 는 `.gitignore` 에 있다.
* **데모 키와 실계좌 키를 절대 섞지 않는다.** 변수명이 다르다(`BIQ_BINANCE_DEMO_*` vs `BIQ_BINANCE_*`).
* 실계좌 키를 만들 단계가 아니다. **이 절차는 데모 키만 필요하다.**
* 나중에 실거래를 논의하게 되면 그때: 전용 서브계정 · **출금 권한 비활성화** · IP 화이트리스트. COIN-M API 로는 출금 권한 조회가 불가능하므로 거래소 UI 에서 직접 확인해야 한다.

## 1. 데모 키 발급

1. Binance 로그인 → **Binance Demo Trading** 접속
2. API Key Management 에서 `[Create API]`
3. 권한: **Futures 활성화**. 출금은 애초에 데모에 없다.
4. Key·Secret 을 복사 (Secret 은 이때 한 번만 보인다)

> **UNVERIFIED 경고**: 같은 데모 키가 COIN-M(`demo-dapi.binance.com`)에도 적용되는지를 **명시한 공식 문장을 찾지 못했다**(`docs/exchange_api_verification.md` §13). 2단계에서 실패하면 이 가정이 틀린 것이므로, 무리해서 우회하지 말고 결과를 알려달라.

## 2. 설정과 스모크 테스트

```
cd ~/Downloads/btc-inventory-hedge-quant
source .venv/bin/activate
```

`.env` 편집 (없으면 `cp .env.example .env`):

```
BIQ_BINANCE_DEMO_API_KEY=발급받은키
BIQ_BINANCE_DEMO_API_SECRET=발급받은시크릿
```

접속 확인:

```
python -m btc_inventory_quant testnet --config configs/testnet.yaml --once
```

시작 보고서에서 볼 것:

| 항목 | 기대 | 의미 |
|---|---|---|
| `contract_spec` | `contractSize: 100` | ③ 계약사양 실서버 확인 |
| `commission_rates` | maker/taker 실제 값 | ③ 해소 — 백테스트의 0.05%/0.02% 와 비교 |
| `position_mode` | `ONE_WAY` | 아니면 거래소 UI 에서 변경 |
| `account.canTrade` | `true` | 키 권한 정상 |

**실패하면 여기서 멈추고 오류 메시지를 알려달라.** 특히 `-2015`(권한/IP), 연결 거부(호스트 문제), `-1022`(서명)는 원인이 각각 다르다.

## 3. 포지션을 열고 정산을 넘긴다

펀딩 정산은 **00:00 / 08:00 / 16:00 UTC** 에 일어난다. 그 시각에 포지션이 있어야 기록이 남는다.

가장 단순한 방법은 데모 UI 에서 **BTCUSD_PERP 숏을 소량**(예: 10계약 = 1,000 USD 명목) 열고 다음 정산 시각을 넘기는 것이다. 크기는 무관하다 — **비율만 맞으면 되므로 작을수록 좋다.**

정산 시각까지 포지션 크기를 **바꾸지 않는 것**이 중요하다. 바뀌면 계산에 쓸 포지션이 애매해진다(바꿔야 했다면 4단계에서 `--contracts` 로 명시).

한국 시간 기준 정산 시각: **09:00 / 17:00 / 01:00 (KST)**.

## 4. 검증 실행

정산 시각이 지난 뒤:

```
python -m btc_inventory_quant verify-funding --config configs/testnet.yaml --window-hours 24
```

포지션을 중간에 바꿨다면 정산 시점의 계약수를 직접 지정한다(숏은 음수):

```
python -m btc_inventory_quant verify-funding --config configs/testnet.yaml --contracts -10
```

### 결과 읽는 법

```json
{
  "verdict": "VERIFIED",
  "samples": 1,
  "mean_ratio": "1.0000...",
  "resolves_partial": true,
  "settlements": [{ "actual_btc": "...", "computed_btc": "...", "ratio": "1.000..." }]
}
```

| verdict | 의미 | 다음 |
|---|---|---|
| `VERIFIED` | 모델값이 거래소 실제값과 0.5% 이내로 일치 | **① 해소.** 문서 PARTIAL → VERIFIED 로 갱신 |
| `MISMATCH` | 불일치 | **공식 또는 마크가격 소스가 틀렸다.** `ratio` 가 단서다(예: 2.0 이면 분모 절반, 1.0 근처가 아니면 분모 자체가 다름). **V1~V4 재계산 필요** |
| `MIXED` | 일부만 일치 | 정산별 포지션 크기가 달랐을 가능성. `--contracts` 로 재실행 |
| `NO_DATA` | 정산 기록 없음 | 정산 시각을 안 넘겼거나 창이 짧다. `--window-hours 72` 로 재시도 |

`ratio` 는 **실제 ÷ 모델**이다. 1.0 이면 우리 공식이 맞다.

## 5. 결과를 알려줄 때

JSON 전체를 그대로 붙여주면 된다. **키·시크릿은 출력에 포함되지 않는다**(로그 마스킹 적용). 계좌 잔고도 포함되지 않는다 — 정산 금액·요율·마크가격·계약수만 나온다.

`VERIFIED` 면 내가 문서 3곳(`inverse_math.md` §6, `exchange_api_verification.md`, `requirements_audit.md`)을 갱신하고 미검증 목록에서 뺀다.
`MISMATCH` 면 `ratio` 로 실제 분모를 역산해서 공식을 고치고, 백테스트를 전부 다시 돌린다.

## 6. 정리

검증이 끝나면 데모 포지션은 닫아도 된다. 계속 두면 ④(WebSocket 실서버)도 관찰할 수 있으니, 며칠 `testnet` 을 상시 실행해두면 덤으로 해소된다.

```
python -m btc_inventory_quant testnet --config configs/testnet.yaml     # 상시 실행
python -m btc_inventory_quant status --config configs/testnet.yaml      # 상태 확인
```

## 7. 이 절차가 하지 않는 것

* **실거래를 하지 않는다.** 데모 전용이며, live 게이트는 별개로 전부 꺼져 있다.
* **폭락 표본 1개 문제를 해결하지 않는다.** 그건 2020년 이전 데이터가 필요하고 이 절차와 무관하다.
* **전략이 유효하다는 증거가 되지 않는다.** V4 결론(무작위와 구분 불가)은 그대로다. 이건 **비용 모델이 맞는지**만 확인한다.
