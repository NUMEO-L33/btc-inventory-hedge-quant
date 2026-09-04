# Inverse(COIN-M) 계약 수학

구현: `src/btc_inventory_quant/domain/inverse_contract.py` (모두 `Decimal`).
근거: `docs/exchange_api_verification.md` §8~§11 (공식 문서 인용).

## 1. 계약 정의

* BTCUSD_PERP 1계약 = **100 USD** (`exchangeInfo.contractSize`, 공식 FAQ "Each BTC futures contract represents 100 USD"). 코드에 고정하지 않고 exchangeInfo에서 읽는다.
* 수량 단위: 정수 계약(`quantityPrecision=0`, `LOT_SIZE.stepSize=1`, `MARKET_LOT_SIZE.maxQty=60000`).
* 가격 단위: `tickSize=0.1`. 마진·정산 자산: BTC.

## 2. 손익

숏 `c` 계약, 진입가 `Pe`, 청산가 `Px`, 계약가치 `CV`:

```
PnL_BTC = c × CV × (1/Px − 1/Pe)
```

미실현 손익은 **Mark Price** 로 계산한다: `UPnL = c × CV × (1/Mark − 1/Pe)`.

검증 사례 (단위 테스트 `tests/unit/test_inverse_math.py`):

| 사례 | 계산 | 결과 |
|---|---|---|
| 1 BTC, 진입 100,000, 명목 100,000 USD(1,000계약), 가격 50,000 | 1000×100×(1/50000−1/100000) | +1 BTC → 총 2 BTC = 100,000 USD |
| 같은 조건, 가격 200,000 | 1000×100×(1/200000−1/100000) | −0.5 BTC → 총 0.5 BTC = 100,000 USD |

즉 100% 헤지는 USD 가치를 고정하는 **합성 달러**이며, 수익은 헤지율을 낮췄다 높이는 과정에서만 발생한다.

## 3. 명목가치·목표 계약수·유효 헤지율

```
short_notional_usd      = |c| × CV
notional_btc            = |c| × CV / price
target_short_notional   = equity_btc × mark × target_ratio
target_contracts        = floor(target_short_notional / CV)   (이후 stepSize 로 내림)
effective_hedge_ratio   = |c| × CV / (equity_btc × mark)
equity_btc              = wallet_balance_btc + UPnL(mark)
equity_usd              = equity_btc × mark
```

`floor` 로 인해 항상 목표 이하로 반올림된다(보수적). 1계약(100 USD) 미만은 거래하지 않는다.

## 4. 평균 진입가 (부분 진입)

Inverse 계약의 BTC 노출은 `q × CV / P` 이므로 평균가는 **조화평균**이다.

```
avg_entry = Σ q_i / Σ (q_i / P_i)
```

예: 100계약@100,000 + 100계약@50,000 → 200 / (0.001 + 0.002) = **66,666.67** (산술평균 75,000 은 오답).
부분 청산은 평균가를 바꾸지 않으며, 실현손익 = `closed × CV × (1/Px − 1/avg)`.
FIFO lot 방식과 평균가 방식은 전체 청산 시 합계가 동일함을 테스트로 확인한다.

## 5. 수수료 (공식 FAQ 검증)

```
fee_btc = |c| × CV / trade_price × fee_rate
```
공식 예시: 1계약@8,800 → 0.01136364 BTC × 0.04% = 0.00000454 BTC (테스트 포함).
기본 요율(백테스트): Taker 0.05%, Maker 0.02% — 보수적 값. 실제 요율은 `GET /dapi/v1/commissionRate` 로 조회(문서에서 현재 VIP0 미확인).

## 6. 펀딩비 (**VERIFIED — 실측 2026-09-04, ±8.1% 정밀도**)

```
funding_btc = −sign(c) × |c| × CV / mark_price × funding_rate
```
* 펀딩비율 > 0: 롱이 숏에게 지급 → 숏은 **수취**(+). < 0 이면 숏이 지급.
* 정산 시각: 00:00 / 08:00 / 16:00 UTC (심볼별 주기는 `fundingInfo`, `premiumIndex.nextFundingTime` 로 확인).
 * "÷ Mark Price" 형태의 COIN-M 전용 원문은 여전히 공식 문서에서 찾지 못했다(수수료 FAQ 의 "(계약수×계약크기)/가격" 명목가치 정의와 펀딩 FAQ 의 "명목가치×비율"을 결합한 것). **그러나 실제 정산과의 대조로 확인됐다.**

### 실측 검증 (2026-09-04T00:00:00Z, 데모 환경)

| 항목 | 값 |
|---|---|
| 요율 (정산 828초 전 `premiumIndex` 로 캡처) | 0.00010000 |
| 마크가격 | 81,206.99154563 |
| 계약수 | −1 (숏) |
| **모델값** | **0.0000001231421** |
| **거래소 실제 정산액** | **0.00000012** |
| 차이 | −3.14e-9 |
| 거래소 보고 양자화 단위 | 1e-8 |
| 판정 | 차이 < 단위 → **VERIFIED** |

재현: `python -m btc_inventory_quant verify-funding --config configs/testnet.yaml --snapshot-rate` (정산 직전) → 정산 후 `verify-funding --window-hours 12`.

**이 검증의 정밀도는 ±8.12% 다.** 거래소가 `FUNDING_FEE` 를 소수점 8자리에서 절삭 보고하는데, 1계약 포지션의 펀딩액이 1.23e-7 BTC 라 절삭 오차가 전체의 8%를 차지하기 때문이다. 따라서 확인된 것은:

* ✅ **공식의 구조** — `÷ mark_price` 가 맞다(없으면 77,000배, 곱하면 6e9배 어긋난다)
* ✅ **부호 규약** — 요율 > 0 일 때 숏이 수취
* ✅ **계약가치 100 USD 적용**
* ⚠️ **8% 이내의 미세 오차는 배제하지 못한다** (예: 마크 대신 인덱스 사용, 정산 시점 마크의 미세한 차이)

더 조이려면 포지션을 키우면 된다 — 펀딩액은 계약수에 비례해 커지는데 보고 단위는 1e-8 로 고정이므로, 8계약이면 ±1.0%, 100계약이면 ±0.08% 다.

**주의**: 데모 환경은 펀딩을 정산하면서도 `fundingRate` 이력을 공개하지 않는다(`docs/exchange_api_verification.md` §16). 프로덕션 요율을 대입하면 데모 자체 요율(0.01%)과 달라(당시 0.00317%) 2.9배 MISMATCH 가 나온다 — 반드시 `--snapshot-rate` 로 같은 환경의 요율을 미리 잡아야 한다.
* 백테스트 근사: 2023-11-01 이후는 펀딩 레코드의 `markPrice` 를 사용, 그 이전은 해당 일봉 마크가격(00:00 은 시가, 08:00/16:00 은 시가·종가 선형보간)을 사용한다. 오차는 펀딩 금액의 수 % 이내(펀딩 자체가 명목가치의 ~0.01%).

## 7. 유지증거금·청산 (PARTIAL)

```
maint_margin_btc = |c| × CV / price × MMR − cum
```
공식 FAQ 예시: 100계약@25,000, MMR 0.4% → 0.0016 BTC (테스트 포함).

단일 숏, Cross 마진, 다른 포지션 없음일 때 청산가 추정:

```
wallet + c·CV·(1/P − 1/Pe) = c·CV/P·MMR − cum
→ P_liq = c·CV·(1 − MMR) / (c·CV/Pe − wallet − cum)
```
분모 ≤ 0 (즉 `wallet ≥ c·CV/Pe`, 유효 레버리지 ≤ 1) 이면 **청산가가 존재하지 않는다**.
본 전략은 헤지율 ≤ 85%, 유효 레버리지 ≤ 1 이므로 가격이 아무리 올라도 이론상 청산되지 않는다:

```
equity(P) − MM(P) = E0(1−h) + (h·E0·P0/P)(1 − MMR) > 0   (h < 1)
```
단, 음(−)의 펀딩 누적과 수수료가 wallet 을 줄이므로 실제로는 거래소의 `positionRisk.liquidationPrice` 를 1차 소스로 감시하고 이 추정치는 보조로 쓴다.

## 8. 85% 헤지에서 가격 상승 시

850계약(85,000 USD)@100,000, 가격 → ∞: 손실 상한 = 0.85 BTC, 자산 → 0.15 BTC. 포지션 부호는 가격 변동으로 바뀌지 않으며(계약수 불변), 유효 헤지율은 1 미만으로 감소한다. BUY 주문은 항상 `qty ≤ 현재 숏` 이어야 하며 초과 시 `PositionFlipError` / `RiskLimitViolation(BUY_EXCEEDS_SHORT)`.

## 9. 반올림 규칙

* 계약수: 항상 내림(`ROUND_DOWN`).
* BTC 금액 표시/원장: 8자리(`ROUND_HALF_EVEN`), 내부 계산은 전체 정밀도 유지.
* 거래소 실측 값(체결 수수료·펀딩)이 내부 계산과 1 satoshi 이상 차이나면 reconciliation 경고.
