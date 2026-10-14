"""Binance COIN-M Futures (dapi) REST adapter.

Every endpoint, parameter and response field used here was checked against the official
documentation on 2026-09-02 — see ``docs/exchange_api_verification.md`` (section numbers cited
inline). Items marked PARTIAL/UNVERIFIED in that document are guarded at runtime (e.g. the demo
host is verified by ``ping`` before use; ``avgPrice`` is never taken from the order-placement
response because the 2026-06 migration removed it).

Design rules
* Public endpoints work without credentials (shadow mode, data download).
* SIGNED endpoints: HMAC-SHA256 over the query string (``totalParams``), ``X-MBX-APIKEY`` header,
  ``timestamp`` adjusted by the measured server-time offset, ``recvWindow`` ≤ 5000 by default.
* GET requests are retried with exponential backoff on connectivity errors / 5xx / 429.
* POST/DELETE (orders) are NEVER retried blindly: a timeout raises :class:`ExchangeTimeout` and
  the order manager resolves the outcome by querying ``origClientOrderId``.
* Secrets never reach logs (masking in :mod:`monitoring.logging`).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from btc_inventory_quant.domain.inverse_contract import ContractSpec, OrderSide
from btc_inventory_quant.exchange.base import (
    ExchangeAPIError,
    ExchangeError,
    ExchangeTimeout,
    ExchangeUnavailable,
)
from btc_inventory_quant.exchange.models import (
    AccountState,
    BookTicker,
    CommissionRates,
    ExchangeOrder,
    ExchangePosition,
    ExchangeTrade,
    IncomeRecord,
    LeverageBracket,
    MarkPriceInfo,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionMode,
    TickerPrice,
    dec,
    dec0,
    ms_to_dt,
)
from btc_inventory_quant.monitoring.logging import get_logger

log = get_logger(__name__)

# §1 Base URLs (VERIFIED in docs; demo reachability must be confirmed with ping at runtime)
BASE_URLS = {
    "production": "https://dapi.binance.com",
    "demo": "https://demo-dapi.binance.com",
}
WS_URLS = {
    "production": "wss://dstream.binance.com",
    "demo": "wss://demo-dstream.binance.com",
}
RETRYABLE_HTTP = {500, 502, 503, 504}
RATE_LIMIT_HTTP = {429, 418}


def hmac_sha256_signature(secret: str, total_params: str) -> str:
    """§2: keyed HMAC-SHA256, secret as key, totalParams as value, hex digest."""
    return hmac.new(
        secret.encode("utf-8"), total_params.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class BinanceCoinMAdapter:
    name = "binance_coinm"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        environment: str = "demo",
        recv_window_ms: int = 5000,
        timeout_seconds: float = 10.0,
        max_retries: int = 5,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 30.0,
        client: httpx.Client | None = None,
        base_url: str | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        if environment not in BASE_URLS:
            raise ValueError(f"unknown environment {environment!r}")
        self.environment = environment
        self.base_url = base_url or BASE_URLS[environment]
        self.ws_url = WS_URLS[environment]
        self._api_key = api_key
        self._api_secret = api_secret
        self.recv_window_ms = recv_window_ms
        self.max_retries = max_retries
        self.backoff_base = backoff_base_seconds
        self.backoff_max = backoff_max_seconds
        self._sleep = sleep
        self._client = client or httpx.Client(
            timeout=timeout_seconds, headers={"User-Agent": "btc-inventory-quant/0.1"}
        )
        self.time_offset_ms = 0
        self.time_sync_rtt_ms: int | None = None  # round trip of the sample behind time_offset_ms
        self.last_used_weight: int | None = None
        self.last_order_count: int | None = None
        self.error_count = 0

    # ------------------------------------------------------------------ core
    @property
    def supports_trading(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _headers(self, signed: bool) -> dict[str, str]:
        h: dict[str, str] = {}
        if signed or self._api_key:
            if not self._api_key:
                raise ExchangeError("API key required for this endpoint")
            h["X-MBX-APIKEY"] = self._api_key
        return h

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    def _sign(self, params: Mapping[str, Any]) -> str:
        if not self._api_secret:
            raise ExchangeError("API secret required for SIGNED endpoint")
        query = urlencode(list(params.items()), doseq=False)
        return f"{query}&signature={hmac_sha256_signature(self._api_secret, query)}"

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        security: str = "NONE",
        retry: bool | None = None,
    ) -> Any:
        """security: NONE | USER_STREAM (key only) | SIGNED (key + signature)."""
        params = dict(params or {})
        retry = (method == "GET") if retry is None else retry
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            headers = self._headers(signed=security != "NONE")
            if security == "SIGNED":
                params = {k: v for k, v in params.items() if v is not None}
                params["recvWindow"] = self.recv_window_ms
                params["timestamp"] = self._timestamp_ms()
                query = self._sign(params)
                full_url = f"{url}?{query}"
                req_params = None
            else:
                full_url = url
                req_params = {k: v for k, v in params.items() if v is not None} or None
            try:
                resp = self._client.request(method, full_url, params=req_params, headers=headers)
            except httpx.TimeoutException as exc:
                self.error_count += 1
                if retry and attempt <= self.max_retries:
                    self._backoff(attempt)
                    continue
                raise ExchangeTimeout(f"{method} {path} timed out") from exc
            except httpx.HTTPError as exc:
                self.error_count += 1
                if retry and attempt <= self.max_retries:
                    self._backoff(attempt)
                    continue
                raise ExchangeUnavailable(f"{method} {path}: {exc.__class__.__name__}") from exc
            self._record_headers(resp)
            if resp.status_code in RATE_LIMIT_HTTP:
                self.error_count += 1
                retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                if resp.status_code == 418 or attempt > self.max_retries:
                    raise ExchangeUnavailable(
                        f"rate limited/banned (http {resp.status_code}, retry-after {retry_after}s)"
                    )
                self._sleep(max(retry_after, self._backoff_seconds(attempt)))
                continue  # the request was rejected, safe to resend (also for orders)
            if resp.status_code in RETRYABLE_HTTP:
                self.error_count += 1
                if retry and attempt <= self.max_retries:
                    self._backoff(attempt)
                    continue
                raise ExchangeUnavailable(
                    f"{method} {path}: http {resp.status_code} {resp.text[:200]}"
                )
            try:
                payload = resp.json() if resp.content else {}
            except ValueError as exc:
                raise ExchangeError(
                    f"{method} {path}: non-JSON response {resp.text[:200]}"
                ) from exc
            if resp.status_code >= 400 or (
                isinstance(payload, dict)
                and isinstance(payload.get("code"), int)
                and payload.get("code", 0) < 0
            ):
                code = int(payload.get("code", -1)) if isinstance(payload, dict) else -1
                msg = (
                    str(payload.get("msg", resp.text[:200]))
                    if isinstance(payload, dict)
                    else resp.text[:200]
                )
                self.error_count += 1
                if code == -1021 and attempt <= 1 and security == "SIGNED":
                    # §2: clock skew → resync once and retry (the order was rejected, not placed)
                    self.sync_time()
                    continue
                raise ExchangeAPIError(code, msg, resp.status_code)
            return payload

    def _request_list(
        self, method: str, path: str, params: Mapping[str, Any] | None = None
    ) -> list[Any]:
        payload = self._request(method, path, params)
        if not isinstance(payload, list):
            raise ExchangeError(
                f"{method} {path}: expected a JSON array, got {type(payload).__name__}"
            )
        return payload

    def _record_headers(self, resp: httpx.Response) -> None:
        for k, v in resp.headers.items():
            lk = k.lower()
            if lk.startswith("x-mbx-used-weight"):
                self.last_used_weight = int(v)
            elif lk.startswith("x-mbx-order-count"):
                self.last_order_count = int(v)

    def _backoff_seconds(self, attempt: int) -> float:
        return float(min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1))))

    def _backoff(self, attempt: int) -> None:
        self._sleep(self._backoff_seconds(attempt))

    # ------------------------------------------------------------- public API
    def ping(self) -> bool:
        self._request("GET", "/dapi/v1/ping")
        return True

    def server_time_ms(self) -> int:
        return int(self._request("GET", "/dapi/v1/time")["serverTime"])

    def sync_time(self, samples: int = 3) -> int:
        """Measure local-vs-server offset (ms); returns the offset and remembers it for signing.

        Midpoint of the round trip, taken from the sample with the shortest round trip: the
        midpoint assumes symmetric latency, and one slow leg on a just-woken Wi-Fi can add
        hundreds of milliseconds of apparent skew (weight 1 per call; three is cheap).
        """
        best_rtt: int | None = None
        best_offset = 0
        for _ in range(max(1, samples)):
            t0 = int(time.time() * 1000)
            server = self.server_time_ms()
            t1 = int(time.time() * 1000)
            rtt = t1 - t0
            if best_rtt is None or rtt < best_rtt:
                best_rtt, best_offset = rtt, server - (t0 + t1) // 2
        self.time_offset_ms = best_offset
        self.time_sync_rtt_ms = best_rtt
        return self.time_offset_ms

    def exchange_info(self) -> dict[str, Any]:
        payload = self._request("GET", "/dapi/v1/exchangeInfo")
        if not isinstance(payload, dict):
            raise ExchangeError("exchangeInfo: expected a JSON object")
        return payload

    def contract_spec(self, symbol: str) -> ContractSpec:
        info = self.exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                return ContractSpec.from_exchange_info_symbol(
                    s,
                    source=f"{self.base_url}/dapi/v1/exchangeInfo serverTime={info.get('serverTime')}",
                )
        raise ExchangeError(f"symbol {symbol} not in exchangeInfo")

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        return self._request_list(
            "GET",
            "/dapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            },
        )

    def mark_price_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        return self._request_list(
            "GET",
            "/dapi/v1/markPriceKlines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            },
        )

    def index_price_klines(
        self,
        pair: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        return self._request_list(
            "GET",
            "/dapi/v1/indexPriceKlines",
            {
                "pair": pair,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            },
        )

    def funding_rate_history(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        return self._request_list(
            "GET",
            "/dapi/v1/fundingRate",
            {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": limit},
        )

    def premium_index(self, symbol: str) -> MarkPriceInfo:
        rows = self._request("GET", "/dapi/v1/premiumIndex", {"symbol": symbol})
        row = rows[0] if isinstance(rows, list) else rows
        mark = dec(row.get("markPrice"))
        if mark is None:
            raise ExchangeError("premiumIndex without markPrice")
        t = ms_to_dt(row.get("time"))
        if t is None:
            raise ExchangeError("premiumIndex without time")
        return MarkPriceInfo(
            symbol=str(row["symbol"]),
            mark_price=mark,
            index_price=dec(row.get("indexPrice")),
            last_funding_rate=dec(row.get("lastFundingRate")),
            next_funding_time=ms_to_dt(row.get("nextFundingTime"))
            if row.get("nextFundingTime")
            else None,
            time=t,
        )

    def book_ticker(self, symbol: str) -> BookTicker:
        rows = self._request("GET", "/dapi/v1/ticker/bookTicker", {"symbol": symbol})
        row = rows[0] if isinstance(rows, list) else rows
        return BookTicker(
            symbol=str(row["symbol"]),
            bid_price=dec0(row["bidPrice"]),
            bid_qty=dec0(row["bidQty"]),
            ask_price=dec0(row["askPrice"]),
            ask_qty=dec0(row["askQty"]),
            time=ms_to_dt(row.get("time")),
        )

    def ticker_price(self, symbol: str) -> TickerPrice:
        """§4.7 GET /dapi/v1/ticker/price (weight 1 for a single symbol)."""
        rows = self._request("GET", "/dapi/v1/ticker/price", {"symbol": symbol})
        row = rows[0] if isinstance(rows, list) else rows
        return TickerPrice(
            symbol=str(row["symbol"]), price=dec0(row["price"]), time=ms_to_dt(row.get("time"))
        )

    # ------------------------------------------------------------ account API
    def account(self) -> AccountState:
        raw = self._request("GET", "/dapi/v1/account", security="SIGNED")
        assets = {a["asset"]: a for a in raw.get("assets", [])}
        btc: dict[str, Any] = assets.get("BTC") or next(iter(assets.values()), {})
        positions = tuple(
            self._parse_account_position(p)
            for p in raw.get("positions", [])
            if dec0(p.get("positionAmt")) != 0 or p.get("symbol", "").startswith("BTCUSD")
        )
        return AccountState(
            asset=str(btc.get("asset", "BTC")),
            wallet_balance=dec0(btc.get("walletBalance")),
            cross_wallet_balance=dec0(btc.get("crossWalletBalance")),
            unrealized_pnl=dec0(btc.get("unrealizedProfit")),
            margin_balance=dec0(btc.get("marginBalance")),
            available_balance=dec0(btc.get("availableBalance")),
            maint_margin=dec0(btc.get("maintMargin")),
            positions=positions,
            can_trade=bool(raw.get("canTrade", False)),
            update_time=ms_to_dt(raw.get("updateTime")),
            raw=raw,
        )

    @staticmethod
    def _parse_account_position(p: Mapping[str, Any]) -> ExchangePosition:
        return ExchangePosition(
            symbol=str(p["symbol"]),
            contracts=dec0(p.get("positionAmt")),
            entry_price=dec(p.get("entryPrice")),
            mark_price=None,
            unrealized_pnl=dec0(p.get("unrealizedProfit")),
            liquidation_price=None,
            leverage=int(p["leverage"]) if p.get("leverage") else None,
            margin_type="isolated" if p.get("isolated") else "cross",
            position_side=str(p.get("positionSide", "BOTH")),
            update_time=ms_to_dt(p.get("updateTime")),
            raw=dict(p),
        )

    def positions(self, pair: str | None = None) -> list[ExchangePosition]:
        """§5.10 GET /dapi/v1/positionRisk (v1 is the only COIN-M version)."""
        rows = self._request("GET", "/dapi/v1/positionRisk", {"pair": pair}, security="SIGNED")
        out = []
        for p in rows:
            out.append(
                ExchangePosition(
                    symbol=str(p["symbol"]),
                    contracts=dec0(p.get("positionAmt")),
                    entry_price=dec(p.get("entryPrice")),
                    mark_price=dec(p.get("markPrice")),
                    unrealized_pnl=dec0(p.get("unRealizedProfit")),
                    liquidation_price=(
                        dec(p.get("liquidationPrice"))
                        if dec0(p.get("liquidationPrice")) != 0
                        else None
                    ),
                    leverage=int(p["leverage"]) if p.get("leverage") else None,
                    margin_type=str(p.get("marginType")) if p.get("marginType") else None,
                    position_side=str(p.get("positionSide", "BOTH")),
                    update_time=ms_to_dt(p.get("updateTime")),
                    raw=dict(p),
                )
            )
        return out

    def position_mode(self) -> PositionMode:
        raw = self._request("GET", "/dapi/v1/positionSide/dual", security="SIGNED")
        dual = raw.get("dualSidePosition")
        if isinstance(dual, str):
            dual = dual.lower() == "true"
        return PositionMode.HEDGE if dual else PositionMode.ONE_WAY

    def commission_rates(self, symbol: str) -> CommissionRates:
        raw = self._request("GET", "/dapi/v1/commissionRate", {"symbol": symbol}, security="SIGNED")
        return CommissionRates(
            symbol=str(raw["symbol"]),
            maker=dec0(raw["makerCommissionRate"]),
            taker=dec0(raw["takerCommissionRate"]),
        )

    def leverage_brackets(self, symbol: str) -> list[LeverageBracket]:
        rows = self._request(
            "GET", "/dapi/v2/leverageBracket", {"symbol": symbol}, security="SIGNED"
        )
        out: list[LeverageBracket] = []
        for entry in rows if isinstance(rows, list) else [rows]:
            if entry.get("symbol") not in (None, symbol):
                continue
            for b in entry.get("brackets", []):
                out.append(
                    LeverageBracket(
                        bracket=int(b["bracket"]),
                        initial_leverage=int(b["initialLeverage"]),
                        qty_cap=dec0(b.get("qtyCap")),
                        qty_floor=dec0(
                            b.get("qtylFloor", b.get("qtyFloor"))
                        ),  # both spellings (UNVERIFIED which is live)
                        maint_margin_ratio=dec0(b.get("maintMarginRatio")),
                        cum=dec0(b.get("cum")),
                    )
                )
        return out

    def income(
        self,
        symbol: str | None = None,
        income_type: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[IncomeRecord]:
        rows = self._request(
            "GET",
            "/dapi/v1/income",
            {
                "symbol": symbol,
                "incomeType": income_type,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            },
            security="SIGNED",
        )
        return [
            IncomeRecord(
                income_type=str(r.get("incomeType")),
                amount=dec0(r.get("income")),
                asset=str(r.get("asset", "BTC")),
                time=ms_to_dt(r.get("time")),
                tran_id=str(r.get("tranId", "")),
                symbol=str(r.get("symbol", "")),
                info=str(r.get("info", "")),
                trade_id=str(r.get("tradeId", "")),
            )
            for r in rows
        ]

    def user_trades(
        self,
        symbol: str,
        order_id: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[ExchangeTrade]:
        rows = self._request(
            "GET",
            "/dapi/v1/userTrades",
            {
                "symbol": symbol,
                "orderId": order_id,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            },
            security="SIGNED",
        )
        return [self._parse_trade(r) for r in rows]

    @staticmethod
    def _parse_trade(r: Mapping[str, Any]) -> ExchangeTrade:
        return ExchangeTrade(
            symbol=str(r["symbol"]),
            trade_id=str(r["id"]),
            order_id=str(r["orderId"]),
            side=OrderSide(str(r["side"])),
            price=dec0(r["price"]),
            qty=dec0(r["qty"]),
            base_qty_btc=dec(r.get("baseQty")),
            realized_pnl=dec0(r.get("realizedPnl")),
            commission=dec0(r.get("commission")),
            commission_asset=str(r.get("commissionAsset", "BTC")),
            time=ms_to_dt(r.get("time")),
            maker=bool(r.get("maker", False)),
            raw=dict(r),
        )

    # --------------------------------------------------------------- orders
    def place_order(self, request: OrderRequest) -> ExchangeOrder:
        """§5.1 POST /dapi/v1/order. One-way mode: positionSide omitted (=BOTH), reduceOnly allowed."""
        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": request.order_type.value,
            "quantity": str(request.quantity.quantize(Decimal(1))),
            "newClientOrderId": request.client_order_id,
            "newOrderRespType": request.response_type,
        }
        if request.reduce_only:
            params["reduceOnly"] = "true"
        if request.order_type is OrderType.LIMIT:
            assert request.price is not None and request.time_in_force is not None
            params["price"] = str(request.price)
            params["timeInForce"] = request.time_in_force.value
        raw = self._request("POST", "/dapi/v1/order", params, security="SIGNED", retry=False)
        return self._parse_order(raw)

    def query_order(
        self, symbol: str, client_order_id: str | None = None, order_id: str | None = None
    ) -> ExchangeOrder:
        if not client_order_id and not order_id:
            raise ValueError("client_order_id or order_id required")
        raw = self._request(
            "GET",
            "/dapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_order_id, "orderId": order_id},
            security="SIGNED",
        )
        return self._parse_order(raw)

    def cancel_order(
        self, symbol: str, client_order_id: str | None = None, order_id: str | None = None
    ) -> ExchangeOrder:
        raw = self._request(
            "DELETE",
            "/dapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_order_id, "orderId": order_id},
            security="SIGNED",
            retry=False,
        )
        return self._parse_order(raw)

    def cancel_all_open_orders(self, symbol: str) -> None:
        self._request(
            "DELETE", "/dapi/v1/allOpenOrders", {"symbol": symbol}, security="SIGNED", retry=False
        )

    def open_orders(self, symbol: str) -> list[ExchangeOrder]:
        rows = self._request("GET", "/dapi/v1/openOrders", {"symbol": symbol}, security="SIGNED")
        return [self._parse_order(r) for r in rows]

    @staticmethod
    def _parse_order(raw: Mapping[str, Any]) -> ExchangeOrder:
        status_raw = str(raw.get("status", "UNKNOWN"))
        try:
            status = OrderStatus(status_raw)
        except ValueError:
            status = OrderStatus.UNKNOWN
        avg = dec(raw.get("avgPrice"))
        if avg is not None and avg == 0:
            avg = None  # "0.0" means no fill yet (never trust 0 as a price)
        return ExchangeOrder(
            symbol=str(raw["symbol"]),
            order_id=str(raw["orderId"]),
            client_order_id=str(raw.get("clientOrderId", "")),
            side=OrderSide(str(raw["side"])),
            order_type=str(raw.get("type", "")),
            status=status,
            orig_qty=dec0(raw.get("origQty")),
            executed_qty=dec0(raw.get("executedQty")),
            avg_price=avg,
            reduce_only=bool(raw.get("reduceOnly", False)),
            position_side=str(raw.get("positionSide", "BOTH")),
            price=dec(raw.get("price")),
            time_in_force=str(raw.get("timeInForce")) if raw.get("timeInForce") else None,
            update_time=ms_to_dt(raw.get("updateTime")),
            cum_base=dec(raw.get("cumBase")),
            raw=dict(raw),
        )

    # ---------------------------------------------------- account configuration
    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._request(
            "POST",
            "/dapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage},
            security="SIGNED",
            retry=False,
        )

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            self._request(
                "POST",
                "/dapi/v1/marginType",
                {"symbol": symbol, "marginType": margin_type},
                security="SIGNED",
                retry=False,
            )
        except ExchangeAPIError as exc:
            if exc.code != -4046:  # "No need to change margin type."
                raise

    # ------------------------------------------------------------ user stream
    def start_user_stream(self) -> str:
        raw = self._request("POST", "/dapi/v1/listenKey", security="USER_STREAM", retry=True)
        return str(raw["listenKey"])

    def keepalive_user_stream(self) -> None:
        self._request("PUT", "/dapi/v1/listenKey", security="USER_STREAM", retry=True)

    def close_user_stream(self) -> None:
        self._request("DELETE", "/dapi/v1/listenKey", security="USER_STREAM", retry=False)
