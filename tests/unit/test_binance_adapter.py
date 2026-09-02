"""Binance COIN-M adapter tests against OFFICIAL documentation examples (no network)."""

from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from btc_inventory_quant.domain.inverse_contract import OrderSide
from btc_inventory_quant.exchange.base import ExchangeAPIError, ExchangeTimeout, ExchangeUnavailable
from btc_inventory_quant.exchange.binance_coinm import (
    BASE_URLS,
    BinanceCoinMAdapter,
    hmac_sha256_signature,
)
from btc_inventory_quant.exchange.models import OrderRequest, OrderStatus, OrderType, PositionMode

# Official example keys/params from the COIN-M General Info page (docs/exchange_api_verification.md §2)
DOC_API_KEY = "dbefbc809e3e83c283a984c3a1459732ea7db1360ca80c5c2c8867408d28cc83"
DOC_SECRET = "2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9"
DOC_PARAMS = "symbol=BTCUSD_200925&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943"
DOC_SIGNATURE = "21fd819734bf0e5c68740eed892909414d693635c5f7fffab1313925ae13556a"
DOC_PARAMS_3 = "symbol=BTCUSD_200925&side=BUY&type=LIMIT&timeInForce=GTCquantity=1&price=9000&recvWindow=5000&timestamp=1591702613943"
DOC_SIGNATURE_3 = "f3129e7c72c7727037891ad8a86b76a7dc514ba125a536775c8ba403b2d1b222"

ORDER_QUERY_EXAMPLE = {
    "avgPrice": "0.0",
    "clientOrderId": "abc",
    "cumBase": "0",
    "executedQty": "0",
    "orderId": 1917641,
    "origQty": "0.40",
    "origType": "TRAILING_STOP_MARKET",
    "price": "0",
    "reduceOnly": False,
    "side": "BUY",
    "positionSide": "SHORT",
    "status": "NEW",
    "stopPrice": "9300",
    "closePosition": False,
    "symbol": "BTCUSD_200925",
    "pair": "BTCUSD",
    "time": 1579276756075,
    "timeInForce": "GTC",
    "type": "TRAILING_STOP_MARKET",
    "activatePrice": "9020",
    "priceRate": "0.3",
    "updateTime": 1579276756075,
    "workingType": "CONTRACT_PRICE",
    "priceProtect": False,
    "priceMatch": "NONE",
    "selfTradePreventionMode": "NONE",
}
POSITION_RISK_EXAMPLE = [
    {
        "symbol": "BTCUSD_PERP",
        "positionAmt": "-850",
        "entryPrice": "100000.0",
        "breakEvenPrice": "0.0",
        "markPrice": "101000.00000000",
        "unRealizedProfit": "-0.00841584",
        "liquidationPrice": "0",
        "leverage": "1",
        "maxQty": "50",
        "marginType": "cross",
        "isolatedMargin": "0.00000000",
        "isAutoAddMargin": "false",
        "positionSide": "BOTH",
        "updateTime": 0,
    }
]
ACCOUNT_EXAMPLE = {
    "assets": [
        {
            "asset": "BTC",
            "walletBalance": "0.00241969",
            "unrealizedProfit": "0.00000000",
            "marginBalance": "0.00241969",
            "maintMargin": "0.00000000",
            "initialMargin": "0.00000000",
            "positionInitialMargin": "0.00000000",
            "openOrderInitialMargin": "0.00000000",
            "maxWithdrawAmount": "0.00241969",
            "crossWalletBalance": "0.00241969",
            "crossUnPnl": "0.00000000",
            "availableBalance": "0.00241969",
            "updateTime": 1625474304765,
        }
    ],
    "positions": [
        {
            "symbol": "BTCUSD_PERP",
            "positionAmt": "0",
            "initialMargin": "0",
            "maintMargin": "0",
            "unrealizedProfit": "0.00000000",
            "positionInitialMargin": "0",
            "openOrderInitialMargin": "0",
            "leverage": "125",
            "isolated": False,
            "positionSide": "BOTH",
            "entryPrice": "0.0",
            "breakEvenPrice": "0.0",
            "maxQty": "50",
            "updateTime": 0,
            "notionalValue": "0",
        }
    ],
    "canDeposit": True,
    "canTrade": True,
    "canWithdraw": True,
    "feeTier": 2,
    "updateTime": 0,
}
TRADE_EXAMPLE = [
    {
        "symbol": "BTCUSD_200626",
        "id": 6,
        "orderId": 28,
        "pair": "BTCUSD",
        "side": "SELL",
        "price": "8800",
        "qty": "1",
        "realizedPnl": "0",
        "marginAsset": "BTC",
        "baseQty": "0.01136364",
        "quoteQty": "0",
        "commission": "0.00000454",
        "commissionAsset": "BTC",
        "time": 1590743483586,
        "positionSide": "BOTH",
        "buyer": False,
        "maker": False,
    }
]
NEW_ORDER_RESPONSE_POST_MIGRATION = {
    "clientOrderId": "testOrder",
    "cumQty": "0",
    "executedQty": "10",
    "orderId": 22542179,
    "origQty": "10",
    "price": "0",
    "reduceOnly": False,
    "closePosition": False,
    "side": "SELL",
    "positionSide": "BOTH",
    "status": "FILLED",
    "stopPrice": "0",
    "symbol": "BTCUSD_PERP",
    "pair": "BTCUSD",
    "timeInForce": "GTC",
    "type": "MARKET",
    "origType": "MARKET",
    "updateTime": 1566818724722,
    "workingType": "CONTRACT_PRICE",
    "priceProtect": False,
    "priceMatch": "NONE",
    "selfTradePreventionMode": "NONE",
}


# Official USDⓈ-M General Info example (same HMAC-SHA256 scheme, reproduces exactly)
USDM_PARAMS = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943"
USDM_SIGNATURE = "3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9"


def test_hmac_signature_matches_official_example() -> None:
    assert hmac_sha256_signature(DOC_SECRET, USDM_PARAMS) == USDM_SIGNATURE


def test_coinm_doc_example_signature_is_documented_as_non_reproducible() -> None:
    """The COIN-M page prints example signatures that do NOT reproduce from its printed inputs
    (checked 2026-09-02; see docs/exchange_api_verification.md §2). The scheme itself is verified by
    the USDⓈ-M vector above, so this test only pins the observation to detect a docs fix."""
    assert hmac_sha256_signature(DOC_SECRET, DOC_PARAMS) != DOC_SIGNATURE
    assert hmac_sha256_signature(DOC_SECRET, DOC_PARAMS_3) != DOC_SIGNATURE_3


def _adapter(handler, **kw):  # type: ignore[no-untyped-def]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return BinanceCoinMAdapter(
        api_key=DOC_API_KEY,
        api_secret=DOC_SECRET,
        environment="demo",
        client=client,
        sleep=lambda s: None,
        **kw,
    )


def test_base_urls_are_the_verified_ones() -> None:
    assert BASE_URLS["production"] == "https://dapi.binance.com"
    assert BASE_URLS["demo"] == "https://demo-dapi.binance.com"
    a = BinanceCoinMAdapter(
        environment="production",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    assert a.ws_url == "wss://dstream.binance.com"
    assert (
        BinanceCoinMAdapter(
            environment="demo",
            client=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
            ),
        ).ws_url
        == "wss://demo-dstream.binance.com"
    )


def test_signed_request_shape_and_header() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=ORDER_QUERY_EXAMPLE)

    a = _adapter(handler)
    order = a.query_order("BTCUSD_200925", client_order_id="abc")
    url = urlparse(str(seen["url"]))
    qs = parse_qs(url.query)
    assert url.path == "/dapi/v1/order"
    assert seen["headers"]["x-mbx-apikey"] == DOC_API_KEY  # type: ignore[index]
    assert qs["origClientOrderId"] == ["abc"]
    assert qs["recvWindow"] == ["5000"]
    assert "timestamp" in qs
    # signature is the LAST parameter and equals HMAC of everything before it
    query = url.query
    body, sig = query.rsplit("&signature=", 1)
    assert hmac_sha256_signature(DOC_SECRET, body) == sig
    assert order.status is OrderStatus.NEW
    assert order.avg_price is None  # "0.0" must not be mistaken for a price
    assert order.orig_qty == Decimal("0.40")


def test_parse_position_account_and_trades() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/dapi/v1/positionRisk":
            return httpx.Response(200, json=POSITION_RISK_EXAMPLE)
        if request.url.path == "/dapi/v1/account":
            return httpx.Response(200, json=ACCOUNT_EXAMPLE)
        if request.url.path == "/dapi/v1/userTrades":
            return httpx.Response(200, json=TRADE_EXAMPLE)
        if request.url.path == "/dapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": False})
        return httpx.Response(404, json={"code": -1, "msg": "?"})

    a = _adapter(handler)
    pos = a.positions("BTCUSD")[0]
    assert pos.contracts == Decimal("-850")
    assert pos.short_contracts == Decimal("850")
    assert pos.entry_price == Decimal("100000.0")
    assert pos.liquidation_price is None  # "0" → no liquidation price
    acct = a.account()
    assert acct.wallet_balance == Decimal("0.00241969")
    assert acct.can_trade is True
    trades = a.user_trades("BTCUSD_200626", order_id="28")
    t = trades[0]
    assert (
        t.side is OrderSide.SELL and t.qty == Decimal("1") and t.commission == Decimal("0.00000454")
    )
    assert t.base_qty_btc == Decimal("0.01136364")
    assert a.position_mode() is PositionMode.ONE_WAY


def test_place_market_order_params_and_post_migration_response() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["qs"] = parse_qs(urlparse(str(request.url)).query)
        return httpx.Response(200, json=NEW_ORDER_RESPONSE_POST_MIGRATION)

    a = _adapter(handler)
    req = OrderRequest(
        symbol="BTCUSD_PERP",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
        client_order_id="biq-test-1",
        reduce_only=True,
    )
    order = a.place_order(req)
    qs = seen["qs"]
    assert seen["method"] == "POST"
    assert qs["type"] == ["MARKET"] and qs["quantity"] == ["10"] and qs["reduceOnly"] == ["true"]  # type: ignore[index]
    assert qs["newClientOrderId"] == ["biq-test-1"] and qs["newOrderRespType"] == ["RESULT"]  # type: ignore[index]
    assert "positionSide" not in qs  # one-way mode: omitted (= BOTH)
    assert order.status is OrderStatus.FILLED
    assert order.avg_price is None  # removed from placement responses after the 2026-06 migration


def test_timestamp_error_resyncs_and_retries_once() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(
                400,
                json={
                    "code": -1021,
                    "msg": "Timestamp for this request is outside of the recvWindow.",
                },
            )
        return httpx.Response(200, json=ORDER_QUERY_EXAMPLE)

    a = _adapter(handler)
    a.query_order("BTCUSD_200925", order_id="1917641")
    assert len(attempts) == 2


def test_rate_limit_backoff_and_ban() -> None:
    n = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        if n["calls"] == 1:
            return httpx.Response(
                429, headers={"Retry-After": "1"}, json={"code": -1003, "msg": "Too many requests"}
            )
        return httpx.Response(200, json={"serverTime": 1})

    a = _adapter(handler)
    assert a.server_time_ms() == 1
    assert n["calls"] == 2

    def banned(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, json={"code": -1003, "msg": "banned"})

    with pytest.raises(ExchangeUnavailable):
        _adapter(banned).server_time_ms()


def test_post_timeout_is_not_retried_and_raises_exchange_timeout() -> None:
    n = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        raise httpx.ReadTimeout("timeout")

    a = _adapter(handler)
    req = OrderRequest(
        symbol="BTCUSD_PERP",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        client_order_id="biq-t",
    )
    with pytest.raises(ExchangeTimeout):
        a.place_order(req)
    assert n["calls"] == 1  # never resent blindly


def test_get_retries_on_5xx_then_succeeds() -> None:
    n = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        if n["calls"] < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"serverTime": 5})

    a = _adapter(handler)
    assert a.server_time_ms() == 5
    assert n["calls"] == 3


def test_api_error_surfaces_code_and_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": -2022, "msg": "ReduceOnly Order is rejected."})

    a = _adapter(handler)
    with pytest.raises(ExchangeAPIError) as exc:
        a.place_order(
            OrderRequest(
                symbol="BTCUSD_PERP",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
                client_order_id="x",
                reduce_only=True,
            )
        )
    assert exc.value.code == -2022


def test_contract_spec_from_live_exchange_info() -> None:
    fixture = json.loads(
        (
            __import__("pathlib").Path(__file__).parents[1]
            / "fixtures"
            / "exchange_info_btcusd_perp.json"
        ).read_text()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"serverTime": 1, "symbols": [fixture["symbol"]]})

    spec = _adapter(handler).contract_spec("BTCUSD_PERP")
    assert spec.contract_value_usd == Decimal("100")
    assert "exchangeInfo" in spec.source


def test_public_endpoints_work_without_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "x-mbx-apikey" not in request.headers
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSD_PERP",
                    "pair": "BTCUSD",
                    "markPrice": "77582.4",
                    "indexPrice": "77613.05",
                    "estimatedSettlePrice": "77509.8",
                    "lastFundingRate": "0.00009882",
                    "interestRate": "0.00010000",
                    "nextFundingTime": 1788336000000,
                    "time": 1788326362000,
                }
            ],
        )

    a = BinanceCoinMAdapter(
        environment="production", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert not a.supports_trading
    info = a.premium_index("BTCUSD_PERP")
    assert info.mark_price == Decimal("77582.4")
    assert info.last_funding_rate == Decimal("0.00009882")
