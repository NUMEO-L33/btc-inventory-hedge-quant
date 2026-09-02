"""Paper exchange: an in-memory simulated COIN-M account that implements :class:`ExchangeAdapter`.

Used by **shadow mode** (real public market data, simulated fills — no real orders can ever be
sent) and by the resilience tests (scripted timeouts, rejections, partial fills, lost responses).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from btc_inventory_quant.domain.inverse_contract import ContractSpec, OrderSide, funding_payment_btc
from btc_inventory_quant.domain.position import InversePosition, PositionFlipError
from btc_inventory_quant.exchange.base import (
    ExchangeAPIError,
    ExchangeError,
    ExchangeTimeout,
    MarketDataProvider,
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
    TimeInForce,
)


@dataclass(slots=True)
class PaperOrder:
    request: OrderRequest
    order_id: str
    status: OrderStatus
    executed_qty: Decimal = Decimal("0")
    avg_price: Decimal | None = None
    created: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    updated: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def to_model(self) -> ExchangeOrder:
        r = self.request
        return ExchangeOrder(
            symbol=r.symbol,
            order_id=self.order_id,
            client_order_id=r.client_order_id,
            side=r.side,
            order_type=r.order_type.value,
            status=self.status,
            orig_qty=r.quantity,
            executed_qty=self.executed_qty,
            avg_price=self.avg_price,
            reduce_only=r.reduce_only,
            position_side="BOTH",
            price=r.price,
            time_in_force=r.time_in_force.value if r.time_in_force else None,
            update_time=self.updated,
            cum_base=None,
        )


class StaticMarketData:
    """MarketDataProvider backed by fixed values (tests)."""

    def __init__(
        self,
        spec: ContractSpec,
        mark_price: Decimal,
        klines: list[list[Any]] | None = None,
        funding: list[dict[str, Any]] | None = None,
    ) -> None:
        self.spec = spec
        self.mark_price = mark_price
        self._klines = klines or []
        self._funding = funding or []
        self.now: Callable[[], datetime] = lambda: datetime.now(tz=UTC)

    def ping(self) -> bool:
        return True

    def server_time_ms(self) -> int:
        return int(self.now().timestamp() * 1000)

    def exchange_info(self) -> dict[str, Any]:
        return {"serverTime": self.server_time_ms(), "symbols": [{"symbol": self.spec.symbol}]}

    def contract_spec(self, symbol: str) -> ContractSpec:
        return self.spec

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        return self._klines[-limit:]

    def mark_price_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        return self._klines[-limit:]

    def index_price_klines(
        self,
        pair: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        return self._klines[-limit:]

    def funding_rate_history(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        return self._funding[-limit:]

    def premium_index(self, symbol: str) -> MarkPriceInfo:
        return MarkPriceInfo(
            symbol=symbol,
            mark_price=self.mark_price,
            index_price=self.mark_price,
            last_funding_rate=Decimal("0.0001"),
            next_funding_time=None,
            time=self.now(),
        )

    def book_ticker(self, symbol: str) -> BookTicker:
        tick = self.spec.price_tick
        return BookTicker(
            symbol=symbol,
            bid_price=self.mark_price - tick,
            bid_qty=Decimal(1000),
            ask_price=self.mark_price + tick,
            ask_qty=Decimal(1000),
            time=self.now(),
        )

    def ticker_price(self, symbol: str) -> TickerPrice:
        return TickerPrice(symbol=symbol, price=self.mark_price, time=self.now())


class PaperExchange:
    """Simulated account on top of any :class:`MarketDataProvider`."""

    name = "paper"

    def __init__(
        self,
        market: MarketDataProvider,
        spec: ContractSpec,
        initial_btc: Decimal,
        symbol: str = "BTCUSD_PERP",
        taker_fee: Decimal = Decimal("0.0005"),
        maker_fee: Decimal = Decimal("0.0002"),
        slippage_bps: Decimal = Decimal("5"),
        environment: str = "paper",
    ) -> None:
        self.market = market
        self.spec = spec
        self.symbol = symbol
        self.environment = environment
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.slippage_bps = slippage_bps
        self.wallet_balance = initial_btc
        self.position = InversePosition(spec=spec, allow_long=False)
        self.orders: dict[str, PaperOrder] = {}
        self.trades: list[ExchangeTrade] = []
        self.income_records: list[IncomeRecord] = []
        self._ids = itertools.count(1)
        self.position_mode_value = PositionMode.ONE_WAY
        self.leverage = 1
        self.margin_type = "cross"
        # fault injection (tests)
        self.fail_next_place: list[Exception] = []
        self.lose_response_next_place: int = 0  # accept+fill the order but raise ExchangeTimeout
        self.partial_fill_fraction: Decimal | None = (
            None  # fill only this fraction of MARKET orders
        )
        self.reject_market_orders: bool = False
        self.rest_limit_orders: bool = True  # LIMIT GTX orders rest until fill_resting_orders()
        self.query_failures: int = 0  # raise ExchangeTimeout on the next N query_order calls
        self.listen_key = "paperlistenkey"
        self._settled_funding_times: set[int] = set()

    # ------------------------------------------------------------ market data
    @property
    def supports_trading(self) -> bool:
        return True

    def ping(self) -> bool:
        return self.market.ping()

    def server_time_ms(self) -> int:
        return self.market.server_time_ms()

    def exchange_info(self) -> dict[str, Any]:
        return self.market.exchange_info()

    def contract_spec(self, symbol: str) -> ContractSpec:
        return self.spec

    def klines(self, *a: Any, **k: Any) -> list[list[Any]]:
        return self.market.klines(*a, **k)

    def mark_price_klines(self, *a: Any, **k: Any) -> list[list[Any]]:
        return self.market.mark_price_klines(*a, **k)

    def index_price_klines(self, *a: Any, **k: Any) -> list[list[Any]]:
        return self.market.index_price_klines(*a, **k)

    def funding_rate_history(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return self.market.funding_rate_history(*a, **k)

    def premium_index(self, symbol: str) -> MarkPriceInfo:
        return self.market.premium_index(symbol)

    def book_ticker(self, symbol: str) -> BookTicker:
        return self.market.book_ticker(symbol)

    def ticker_price(self, symbol: str) -> TickerPrice:
        return self.market.ticker_price(symbol)

    def mark_price(self) -> Decimal:
        return self.market.premium_index(self.symbol).mark_price

    # --------------------------------------------------------------- account
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.server_time_ms() / 1000, tz=UTC)

    def _position_model(self, mark: Decimal | None = None) -> ExchangePosition:
        mark = mark or self.mark_price()
        return ExchangePosition(
            symbol=self.symbol,
            contracts=self.position.contracts,
            entry_price=self.position.avg_entry_price,
            mark_price=mark,
            unrealized_pnl=self.position.unrealized_pnl_btc(mark),
            liquidation_price=None,
            leverage=self.leverage,
            margin_type=self.margin_type,
            position_side="BOTH",
            update_time=self._now(),
        )

    def account(self) -> AccountState:
        mark = self.mark_price()
        upnl = self.position.unrealized_pnl_btc(mark)
        return AccountState(
            asset="BTC",
            wallet_balance=self.wallet_balance,
            cross_wallet_balance=self.wallet_balance,
            unrealized_pnl=upnl,
            margin_balance=self.wallet_balance + upnl,
            available_balance=self.wallet_balance
            + upnl
            - self.position.notional_usd() / mark * Decimal("0.01"),
            maint_margin=self.position.notional_usd() / mark * Decimal("0.004"),
            positions=(self._position_model(mark),),
            can_trade=True,
            update_time=self._now(),
        )

    def positions(self, pair: str | None = None) -> list[ExchangePosition]:
        return [self._position_model()]

    def position_mode(self) -> PositionMode:
        return self.position_mode_value

    def commission_rates(self, symbol: str) -> CommissionRates:
        return CommissionRates(symbol=symbol, maker=self.maker_fee, taker=self.taker_fee)

    def leverage_brackets(self, symbol: str) -> list[LeverageBracket]:
        return [LeverageBracket(1, 125, Decimal(50), Decimal(0), Decimal("0.004"), Decimal(0))]

    def income(
        self,
        symbol: str | None = None,
        income_type: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[IncomeRecord]:
        out = [
            r for r in self.income_records if (income_type is None or r.income_type == income_type)
        ]
        if start_ms is not None:
            out = [
                r for r in out if r.time is not None and int(r.time.timestamp() * 1000) >= start_ms
            ]
        return out[-limit:]

    def user_trades(
        self,
        symbol: str,
        order_id: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[ExchangeTrade]:
        out = [t for t in self.trades if order_id is None or t.order_id == order_id]
        return out[-limit:]

    # ---------------------------------------------------------------- orders
    def _fill_price(self, side: OrderSide, reference: Decimal) -> Decimal:
        s = self.slippage_bps / Decimal(10000)
        raw = reference * (1 + s) if side is OrderSide.BUY else reference * (1 - s)
        return self.spec.round_price(
            raw, rounding=ROUND_CEILING if side is OrderSide.BUY else ROUND_FLOOR
        )

    def _execute(self, order: PaperOrder, qty: Decimal, price: Decimal, maker: bool) -> None:
        req = order.request
        fee_rate = self.maker_fee if maker else self.taker_fee
        try:
            res = self.position.apply_fill(req.side, qty, price, fee_rate)
        except PositionFlipError as exc:
            order.status = OrderStatus.REJECTED
            raise ExchangeAPIError(-2022, "ReduceOnly Order is rejected.") from exc
        self.wallet_balance += res.realized_pnl_btc - res.fee_btc
        prev_qty = order.executed_qty
        order.executed_qty += qty
        # inverse-consistent average price of the fills (harmonic)
        if order.avg_price is None:
            order.avg_price = price
        else:
            order.avg_price = order.executed_qty / (prev_qty / order.avg_price + qty / price)
        order.status = (
            OrderStatus.FILLED
            if order.executed_qty >= req.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        order.updated = self._now()
        trade = ExchangeTrade(
            symbol=req.symbol,
            trade_id=str(next(self._ids)),
            order_id=order.order_id,
            side=req.side,
            price=price,
            qty=qty,
            base_qty_btc=qty * self.spec.contract_value_usd / price,
            realized_pnl=res.realized_pnl_btc,
            commission=res.fee_btc,
            commission_asset="BTC",
            time=self._now(),
            maker=maker,
        )
        self.trades.append(trade)
        if res.realized_pnl_btc != 0:
            self.income_records.append(
                IncomeRecord(
                    "REALIZED_PNL",
                    res.realized_pnl_btc,
                    "BTC",
                    trade.time,
                    str(next(self._ids)),
                    req.symbol,
                    "",
                    trade.trade_id,
                )
            )
        self.income_records.append(
            IncomeRecord(
                "COMMISSION",
                -res.fee_btc,
                "BTC",
                trade.time,
                str(next(self._ids)),
                req.symbol,
                "",
                trade.trade_id,
            )
        )

    def place_order(self, request: OrderRequest) -> ExchangeOrder:
        if self.fail_next_place:
            raise self.fail_next_place.pop(0)
        if request.symbol != self.symbol:
            raise ExchangeAPIError(-1121, "Invalid symbol.")
        if (
            request.client_order_id in self.orders
            and self.orders[request.client_order_id].status.is_open
        ):
            raise ExchangeAPIError(-4015, "Client order id is not valid.")
        if request.side is OrderSide.BUY and not request.reduce_only:
            raise ExchangeAPIError(
                -2022, "paper exchange only accepts reduce-only BUY orders (project rule)"
            )
        if request.side is OrderSide.BUY and request.quantity > self.position.short_contracts:
            raise ExchangeAPIError(-2022, "ReduceOnly Order is rejected.")
        if request.order_type is OrderType.MARKET and self.reject_market_orders:
            raise ExchangeAPIError(
                -4113, "The counterparty's best price does not meet the PERCENT_PRICE filter limit."
            )
        self.spec.validate_qty(request.quantity, market=request.order_type is OrderType.MARKET)
        order = PaperOrder(request=request, order_id=str(next(self._ids)), status=OrderStatus.NEW)
        self.orders[request.client_order_id] = order
        mark = self.mark_price()
        if request.order_type is OrderType.MARKET:
            qty = request.quantity
            if self.partial_fill_fraction is not None:
                qty = max(
                    self.spec.min_qty,
                    self.spec.round_qty_down(
                        (request.quantity * self.partial_fill_fraction).quantize(
                            Decimal(1), rounding=ROUND_FLOOR
                        )
                    ),
                )
            self._execute(order, qty, self._fill_price(request.side, mark), maker=False)
            if order.executed_qty < request.quantity:
                order.status = OrderStatus.EXPIRED  # market remainder cancelled (expiry reason 9)
        elif request.time_in_force is TimeInForce.GTX:
            assert request.price is not None
            crosses = (request.side is OrderSide.BUY and request.price >= mark) or (
                request.side is OrderSide.SELL and request.price <= mark
            )
            if crosses:
                order.status = OrderStatus.EXPIRED  # post-only would take → expired (reason 11)
            elif not self.rest_limit_orders:
                self._execute(order, request.quantity, request.price, maker=True)
        else:
            self._execute(order, request.quantity, request.price or mark, maker=True)
        if self.lose_response_next_place > 0:
            self.lose_response_next_place -= 1
            raise ExchangeTimeout("simulated lost response after the exchange accepted the order")
        return order.to_model()

    def fill_resting_orders(self, price: Decimal | None = None) -> int:
        """Fill every resting LIMIT order at its limit price (tests / maker-first simulation)."""
        n = 0
        for o in self.orders.values():
            if o.status.is_open and o.request.order_type is OrderType.LIMIT:
                self._execute(
                    o,
                    o.request.quantity - o.executed_qty,
                    price or o.request.price or self.mark_price(),
                    maker=True,
                )
                n += 1
        return n

    def query_order(
        self, symbol: str, client_order_id: str | None = None, order_id: str | None = None
    ) -> ExchangeOrder:
        if self.query_failures > 0:
            self.query_failures -= 1
            raise ExchangeTimeout("simulated query timeout")
        for o in self.orders.values():
            if (client_order_id and o.request.client_order_id == client_order_id) or (
                order_id and o.order_id == order_id
            ):
                return o.to_model()
        raise ExchangeAPIError(-2013, "Order does not exist.")

    def cancel_order(
        self, symbol: str, client_order_id: str | None = None, order_id: str | None = None
    ) -> ExchangeOrder:
        for o in self.orders.values():
            if (client_order_id and o.request.client_order_id == client_order_id) or (
                order_id and o.order_id == order_id
            ):
                if not o.status.is_open:
                    raise ExchangeAPIError(-2011, "CANCEL_REJECTED")
                o.status = OrderStatus.CANCELED
                o.updated = self._now()
                return o.to_model()
        raise ExchangeAPIError(-2011, "CANCEL_REJECTED")

    def cancel_all_open_orders(self, symbol: str) -> None:
        for o in self.orders.values():
            if o.status.is_open:
                o.status = OrderStatus.CANCELED

    def open_orders(self, symbol: str) -> list[ExchangeOrder]:
        return [o.to_model() for o in self.orders.values() if o.status.is_open]

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage = leverage

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self.margin_type = margin_type.lower().replace("crossed", "cross")

    def start_user_stream(self) -> str:
        return self.listen_key

    def keepalive_user_stream(self) -> None:
        return None

    def close_user_stream(self) -> None:
        return None

    # ------------------------------------------------------------ simulation
    def apply_funding(self, rate: Decimal, mark_price: Decimal | None = None) -> Decimal:
        mark = mark_price or self.mark_price()
        pay = funding_payment_btc(self.position.contracts, self.spec.contract_value_usd, mark, rate)
        if pay != 0:
            self.wallet_balance += pay
            self.income_records.append(
                IncomeRecord(
                    "FUNDING_FEE", pay, "BTC", self._now(), str(next(self._ids)), self.symbol, ""
                )
            )
        return pay

    def settle_funding(self, since_ms: int | None = None) -> list[dict[str, Any]]:
        """Shadow mode: apply the REAL funding history of the market provider to the paper account.

        Each funding record (``fundingTime``, ``fundingRate``, optional ``markPrice``) is settled
        exactly once (dedupe by fundingTime) on the position held at the time of the call — the
        trader calls this at the start of every daily cycle, i.e. before that day's trade, which
        matches the backtest timeline (00:00 event before the trade, 08:00/16:00 after it).
        """
        rows = self.market.funding_rate_history(self.symbol, start_ms=since_ms)
        applied: list[dict[str, Any]] = []
        for r in rows:
            t = int(r["fundingTime"])
            if t in self._settled_funding_times:
                continue
            if since_ms is not None and t < since_ms:
                continue
            rate = Decimal(str(r["fundingRate"]))
            mp = r.get("markPrice")
            mark = Decimal(str(mp)) if mp not in (None, "", "0") else None
            pay = self.apply_funding(rate, mark)
            self._settled_funding_times.add(t)
            applied.append({"fundingTime": t, "rate": str(rate), "amount_btc": str(pay)})
        return applied

    def external_transfer(self, amount_btc: Decimal) -> None:
        """Simulate an unexpected deposit (+) / withdrawal (−)."""
        self.wallet_balance += amount_btc
        self.income_records.append(
            IncomeRecord(
                "TRANSFER",
                amount_btc,
                "BTC",
                self._now(),
                str(next(self._ids)),
                "",
                "DEPOSIT" if amount_btc > 0 else "WITHDRAW",
            )
        )

    def inject_unknown_order(
        self, side: OrderSide = OrderSide.SELL, qty: Decimal = Decimal(1)
    ) -> ExchangeOrder:
        """An order nobody in this system placed (e.g. manual trading on the account)."""
        req = OrderRequest(
            symbol=self.symbol,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=qty,
            client_order_id=f"manual{next(self._ids)}",
            price=self.mark_price() * 2,
            time_in_force=TimeInForce.GTC,
        )
        order = PaperOrder(request=req, order_id=str(next(self._ids)), status=OrderStatus.NEW)
        self.orders[req.client_order_id] = order
        return order.to_model()

    def force_position(self, contracts_signed: Decimal, entry_price: Decimal) -> None:
        """Set the exchange-side position directly (simulates manual trades / restart recovery)."""
        self.position = InversePosition(spec=self.spec, allow_long=False)
        if contracts_signed < 0:
            self.position.apply_fill(OrderSide.SELL, -contracts_signed, entry_price, Decimal("0"))
        elif contracts_signed > 0:
            raise ExchangeError("paper exchange never holds longs")


class ReplayMarketData:
    """MarketDataProvider that replays a :class:`DailyDataset` day by day (offline shadow / tests).

    ``cursor`` is the index of the *execution day*: klines returned are the completed candles up to
    ``cursor − 1`` plus the (in-progress) candle ``cursor``; the mark price is the day's mark open —
    exactly what the live loop sees a few minutes after the daily close.
    """

    def __init__(self, dataset: Any, spec: ContractSpec, funding: Any | None = None) -> None:
        from btc_inventory_quant.data.market_data import DailyDataset

        assert isinstance(dataset, DailyDataset)
        self.dataset = dataset
        self.spec = spec
        self.funding = funding
        self.cursor = 0
        self._rows = self._to_rows(dataset.frame)

    @staticmethod
    def _to_rows(frame: Any) -> list[list[Any]]:
        rows = []
        for ts, r in frame.iterrows():
            open_ms = int(ts.timestamp() * 1000)
            rows.append(
                [
                    open_ms,
                    r["open_dec"],
                    f"{r['high']:.1f}",
                    f"{r['low']:.1f}",
                    r["close_dec"],
                    f"{r['volume']:.0f}",
                    open_ms + 86_400_000 - 1,
                    "0",
                    0,
                    "0",
                    "0",
                    "0",
                ]
            )
        return rows

    @property
    def now(self) -> datetime:
        ts: datetime = self.dataset.frame.index[self.cursor].to_pydatetime()
        return ts.replace(minute=5)

    def advance(self) -> None:
        self.cursor += 1

    def ping(self) -> bool:
        return True

    def server_time_ms(self) -> int:
        return int(self.now.timestamp() * 1000)

    def sync_time(self) -> int:
        return 0

    def exchange_info(self) -> dict[str, Any]:
        return {"serverTime": self.server_time_ms(), "symbols": []}

    def contract_spec(self, symbol: str) -> ContractSpec:
        return self.spec

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        rows = self._rows[: self.cursor + 1]
        return rows[-limit:]

    def mark_price_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        return self.klines(symbol, interval, start_ms, end_ms, limit)

    def index_price_klines(
        self,
        pair: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        return self.klines(pair, interval, start_ms, end_ms, limit)

    def funding_rate_history(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Funding events already settled at the replay clock (≤ 00:00 of the cursor day)."""
        if self.funding is None:
            return []
        now_ms = int(self.now.timestamp() * 1000)
        rows = []
        for ev in self.funding.events:
            t = int(ev.funding_time.timestamp() * 1000)
            if t > now_ms or (start_ms is not None and t < start_ms):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "fundingTime": t,
                    "fundingRate": str(ev.rate),
                    "markPrice": "" if ev.mark_price is None else str(ev.mark_price),
                }
            )
        return rows[-limit:]

    def ticker_price(self, symbol: str) -> TickerPrice:
        ts = self.dataset.frame.index[self.cursor]
        return TickerPrice(symbol=symbol, price=self.dataset.dec(ts, "open"), time=self.now)

    def premium_index(self, symbol: str) -> MarkPriceInfo:
        ts = self.dataset.frame.index[self.cursor]
        mark = self.dataset.dec(ts, "mark_open")
        return MarkPriceInfo(
            symbol=symbol,
            mark_price=mark,
            index_price=mark,
            last_funding_rate=Decimal("0.0001"),
            next_funding_time=None,
            time=self.now,
        )

    def book_ticker(self, symbol: str) -> BookTicker:
        mark = self.premium_index(symbol).mark_price
        tick = self.spec.price_tick
        return BookTicker(
            symbol=symbol,
            bid_price=mark - tick,
            bid_qty=Decimal(1000),
            ask_price=mark + tick,
            ask_qty=Decimal(1000),
            time=self.now,
        )
