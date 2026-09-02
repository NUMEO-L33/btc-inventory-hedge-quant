"""Binance COIN-M WebSocket streams (user data stream + mark-price stream).

Verified against the official documentation (docs/exchange_api_verification.md §1, §6, §7):

* User data stream: ``wss://dstream.binance.com/ws/<listenKey>`` (demo: ``wss://demo-dstream.binance.com``);
  listenKey valid 60 min, extended with ``PUT /dapi/v1/listenKey`` (we do it every 30 min); a single
  connection is valid 24 h (we reconnect proactively before that); ``listenKeyExpired`` → new key.
* Market stream: ``/ws/<symbol lowercase>@markPrice`` → ``markPriceUpdate`` (``p`` mark, ``i`` index,
  ``r`` funding rate, ``T`` next funding time).
* Server pings every 3 minutes; the ``websockets`` library answers pings automatically.

The streams run in a background thread with their own asyncio loop and expose thread-safe
snapshots (:meth:`status`, :meth:`latest_order_event`, :meth:`last_position`). They are an
*additional* source of truth: the REST query remains authoritative for order resolution, the
stream is consulted to (a) confirm an order exists when REST says -2013, (b) detect position
divergence between REST and WS, (c) detect prolonged silence (SAFE_HOLD trigger).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from btc_inventory_quant.exchange.models import OrderStatus, dec, dec0, ms_to_dt
from btc_inventory_quant.monitoring.logging import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- events
@dataclass(frozen=True, slots=True)
class OrderEvent:
    symbol: str
    client_order_id: str
    order_id: str
    side: str
    status: OrderStatus
    execution_type: str
    orig_qty: Decimal
    cum_filled_qty: Decimal
    last_filled_qty: Decimal
    last_filled_price: Decimal | None
    avg_price: Decimal | None
    commission: Decimal
    commission_asset: str
    realized_pnl: Decimal
    reduce_only: bool
    trade_id: str
    event_time: datetime | None
    transaction_time: datetime | None


@dataclass(frozen=True, slots=True)
class PositionEvent:
    symbol: str
    position_amt: Decimal
    entry_price: Decimal | None
    unrealized_pnl: Decimal
    margin_type: str
    position_side: str


@dataclass(frozen=True, slots=True)
class AccountEvent:
    reason: str
    wallet_balance: Decimal | None
    cross_wallet_balance: Decimal | None
    balance_change: Decimal | None
    positions: tuple[PositionEvent, ...]
    event_time: datetime | None


@dataclass(frozen=True, slots=True)
class MarkPriceEvent:
    symbol: str
    mark_price: Decimal
    index_price: Decimal | None
    funding_rate: Decimal | None
    next_funding_time: datetime | None
    event_time: datetime | None


def parse_order_trade_update(payload: dict[str, Any]) -> OrderEvent:
    """§6.2 ORDER_TRADE_UPDATE → :class:`OrderEvent` (field letters per the official payload)."""
    o = payload["o"]
    status_raw = str(o.get("X", "UNKNOWN"))
    try:
        status = OrderStatus(status_raw)
    except ValueError:
        status = OrderStatus.UNKNOWN
    avg = dec(o.get("ap"))
    if avg is not None and avg == 0:
        avg = None
    last_price = dec(o.get("L"))
    if last_price is not None and last_price == 0:
        last_price = None
    return OrderEvent(
        symbol=str(o["s"]),
        client_order_id=str(o.get("c", "")),
        order_id=str(o.get("i", "")),
        side=str(o.get("S", "")),
        status=status,
        execution_type=str(o.get("x", "")),
        orig_qty=dec0(o.get("q")),
        cum_filled_qty=dec0(o.get("z")),
        last_filled_qty=dec0(o.get("l")),
        last_filled_price=last_price,
        avg_price=avg,
        commission=dec0(o.get("n")),
        commission_asset=str(o.get("N") or "BTC"),
        realized_pnl=dec0(o.get("rp")),
        reduce_only=bool(o.get("R", False)),
        trade_id=str(o.get("t", "")),
        event_time=ms_to_dt(payload.get("E")),
        transaction_time=ms_to_dt(payload.get("T")),
    )


def parse_account_update(payload: dict[str, Any]) -> AccountEvent:
    """§6.3 ACCOUNT_UPDATE → :class:`AccountEvent`."""
    a = payload["a"]
    wb = cw = bc = None
    for b in a.get("B", []):
        if b.get("a") == "BTC":
            wb, cw, bc = dec(b.get("wb")), dec(b.get("cw")), dec(b.get("bc"))
    positions = tuple(
        PositionEvent(
            symbol=str(p["s"]),
            position_amt=dec0(p.get("pa")),
            entry_price=dec(p.get("ep")),
            unrealized_pnl=dec0(p.get("up")),
            margin_type=str(p.get("mt", "")),
            position_side=str(p.get("ps", "BOTH")),
        )
        for p in a.get("P", [])
    )
    return AccountEvent(
        reason=str(a.get("m", "")),
        wallet_balance=wb,
        cross_wallet_balance=cw,
        balance_change=bc,
        positions=positions,
        event_time=ms_to_dt(payload.get("E")),
    )


def parse_mark_price_update(payload: dict[str, Any]) -> MarkPriceEvent:
    """§7 markPriceUpdate → :class:`MarkPriceEvent`."""
    mark = dec(payload.get("p"))
    if mark is None:
        raise ValueError("markPriceUpdate without mark price")
    return MarkPriceEvent(
        symbol=str(payload["s"]),
        mark_price=mark,
        index_price=dec(payload.get("i")),
        funding_rate=dec(payload.get("r")),
        next_funding_time=ms_to_dt(payload.get("T")) if payload.get("T") else None,
        event_time=ms_to_dt(payload.get("E")),
    )


# --------------------------------------------------------------------------- status
@dataclass(slots=True)
class StreamStatus:
    name: str
    connected: bool
    last_message_at: datetime | None
    reconnects: int
    messages: int
    last_error: str = ""

    def silence_seconds(self, now: datetime) -> float | None:
        if self.last_message_at is None:
            return None
        return (now - self.last_message_at).total_seconds()


class ListenKeyProvider(Protocol):
    ws_url: str

    def start_user_stream(self) -> str: ...
    def keepalive_user_stream(self) -> None: ...


class _BaseStream:
    """Background-thread WebSocket consumer with reconnect/backoff."""

    def __init__(
        self,
        name: str,
        *,
        backoff_max: float = 30.0,
        recv_timeout: float = 5.0,
        max_connection_age: float = 23 * 3600,
    ) -> None:
        self.name = name
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connected = False
        self._last_message_at: datetime | None = None
        self._reconnects = 0
        self._messages = 0
        self._last_error = ""
        self._backoff_max = backoff_max
        self._recv_timeout = recv_timeout
        self._max_connection_age = max_connection_age

    # --- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name=f"ws-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def status(self) -> StreamStatus:
        with self._lock:
            return StreamStatus(
                self.name,
                self._connected,
                self._last_message_at,
                self._reconnects,
                self._messages,
                self._last_error,
            )

    # --- to override ------------------------------------------------------
    def _url(self) -> str:
        raise NotImplementedError

    def _handle(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    async def _on_connected(self) -> None:
        return None

    async def _periodic(self) -> None:
        return None

    # --- loop -------------------------------------------------------------
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - last resort
            with self._lock:
                self._last_error = repr(exc)
                self._connected = False

    async def _run(self) -> None:
        import websockets

        delay = 0.5
        while not self._stop.is_set():
            try:
                url = self._url()
            except Exception as exc:  # listenKey fetch failed etc.
                self._note_error(exc)
                await asyncio.sleep(delay)
                delay = min(self._backoff_max, delay * 2)
                continue
            connected_at = time.monotonic()
            try:
                async with websockets.connect(url, ping_interval=None, max_queue=1024) as ws:
                    with self._lock:
                        self._connected = True
                        self._last_error = ""
                    await self._on_connected()
                    delay = 0.5
                    while not self._stop.is_set():
                        if time.monotonic() - connected_at > self._max_connection_age:
                            log.info(
                                "proactive reconnect before the 24h limit",
                                fields={"stream": self.name},
                            )
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self._recv_timeout)
                        except TimeoutError:
                            await self._periodic()
                            continue
                        self._dispatch(raw)
                        await self._periodic()
            except Exception as exc:
                self._note_error(exc)
            with self._lock:
                self._connected = False
                self._reconnects += 1
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(self._backoff_max, delay * 2)

    def _note_error(self, exc: BaseException) -> None:
        with self._lock:
            self._last_error = f"{exc.__class__.__name__}: {exc}"
        log.warning("stream error", fields={"stream": self.name, "error": self._last_error})

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        if isinstance(payload, dict) and "data" in payload and "stream" in payload:
            payload = payload["data"]  # combined stream envelope
        if not isinstance(payload, dict):
            return
        with self._lock:
            self._messages += 1
            self._last_message_at = datetime.now(tz=UTC)
        try:
            self._handle(payload)
        except Exception as exc:  # never let a bad message kill the stream
            self._note_error(exc)


class UserDataStream(_BaseStream):
    """ORDER_TRADE_UPDATE / ACCOUNT_UPDATE / listenKeyExpired consumer."""

    def __init__(
        self,
        provider: ListenKeyProvider,
        on_event: Callable[[str, Any], None] | None = None,
        *,
        keepalive_seconds: float = 1800.0,
        url_override: str | None = None,
        **kw: Any,
    ) -> None:
        super().__init__("user_data", **kw)
        self.provider = provider
        self.on_event = on_event
        self.keepalive_seconds = keepalive_seconds
        self._url_override = url_override
        self._listen_key: str | None = None
        self._last_keepalive = 0.0
        self._order_events: dict[str, OrderEvent] = {}
        self._positions: dict[str, PositionEvent] = {}
        self._wallet_balance: Decimal | None = None
        self._expired = False

    def _url(self) -> str:
        if self._listen_key is None or self._expired:
            self._listen_key = self.provider.start_user_stream()
            self._expired = False
            self._last_keepalive = time.monotonic()
        base = self._url_override or self.provider.ws_url
        return f"{base}/ws/{self._listen_key}"

    async def _periodic(self) -> None:
        if time.monotonic() - self._last_keepalive >= self.keepalive_seconds:
            try:
                await asyncio.to_thread(self.provider.keepalive_user_stream)
                self._last_keepalive = time.monotonic()
            except Exception as exc:
                self._note_error(exc)
        if self._expired:
            raise ConnectionResetError("listenKey expired; reconnecting with a new key")

    def _handle(self, payload: dict[str, Any]) -> None:
        kind = payload.get("e")
        if kind == "ORDER_TRADE_UPDATE":
            ev = parse_order_trade_update(payload)
            with self._lock:
                self._order_events[ev.client_order_id] = ev
            if self.on_event:
                self.on_event(kind, ev)
        elif kind == "ACCOUNT_UPDATE":
            acc = parse_account_update(payload)
            with self._lock:
                for p in acc.positions:
                    self._positions[p.symbol] = p
                if acc.wallet_balance is not None:
                    self._wallet_balance = acc.wallet_balance
            if self.on_event:
                self.on_event(kind, acc)
        elif kind == "listenKeyExpired":
            log.warning("listenKey expired; a new key will be requested")
            self._expired = True
            if self.on_event:
                self.on_event(kind, payload)
        elif self.on_event and kind:
            self.on_event(str(kind), payload)

    # --- thread-safe accessors --------------------------------------------
    def latest_order_event(self, client_order_id: str) -> OrderEvent | None:
        with self._lock:
            return self._order_events.get(client_order_id)

    def last_position(self, symbol: str) -> PositionEvent | None:
        with self._lock:
            return self._positions.get(symbol)

    def last_wallet_balance(self) -> Decimal | None:
        with self._lock:
            return self._wallet_balance


class MarkPriceStream(_BaseStream):
    def __init__(
        self,
        ws_url: str,
        symbol: str,
        on_update: Callable[[MarkPriceEvent], None] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__("mark_price", **kw)
        self.ws_url = ws_url
        self.symbol = symbol
        self.on_update = on_update
        self._last: MarkPriceEvent | None = None

    def _url(self) -> str:
        return f"{self.ws_url}/ws/{self.symbol.lower()}@markPrice"

    def _handle(self, payload: dict[str, Any]) -> None:
        if payload.get("e") != "markPriceUpdate":
            return
        ev = parse_mark_price_update(payload)
        with self._lock:
            self._last = ev
        if self.on_update:
            self.on_update(ev)

    def last(self) -> MarkPriceEvent | None:
        with self._lock:
            return self._last


@dataclass(slots=True)
class StreamBundle:
    """What the trader needs from the streams (None = streams disabled)."""

    user: UserDataStream | None = None
    mark: MarkPriceStream | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def enabled(self) -> bool:
        return self.user is not None or self.mark is not None

    def start(self) -> None:
        for s in (self.user, self.mark):
            if s is not None:
                s.start()

    def stop(self) -> None:
        for s in (self.user, self.mark):
            if s is not None:
                with contextlib.suppress(Exception):
                    s.stop()

    def statuses(self) -> list[StreamStatus]:
        return [s.status() for s in (self.user, self.mark) if s is not None]

    def connected(self) -> bool:
        st = self.statuses()
        return bool(st) and all(s.connected for s in st)

    def max_silence_seconds(self, now: datetime) -> float | None:
        vals = []
        for s in self.statuses():
            sil = s.silence_seconds(now)
            vals.append((now - self.started_at).total_seconds() if sil is None else sil)
        return max(vals) if vals else None
