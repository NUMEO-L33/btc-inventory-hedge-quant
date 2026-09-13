"""WebSocket stream tests against a local server sending the OFFICIAL payload examples."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from decimal import Decimal
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import serve

from btc_inventory_quant.exchange.binance_ws import (
    MarkPriceStream,
    StreamBundle,
    UserDataStream,
    parse_account_update,
    parse_mark_price_update,
    parse_order_trade_update,
)
from btc_inventory_quant.exchange.models import OrderStatus

ORDER_TRADE_UPDATE = {
    "e": "ORDER_TRADE_UPDATE",
    "E": 1591274595442,
    "T": 1591274595442,
    "i": "SfsR",
    "o": {
        "s": "BTCUSD_PERP",
        "c": "biq-s-20260901T000500-abcd1234",
        "S": "SELL",
        "o": "MARKET",
        "f": "GTC",
        "q": "20",
        "p": "0",
        "ap": "77500.5",
        "sp": "0",
        "x": "TRADE",
        "X": "FILLED",
        "i": 8888888,
        "l": "20",
        "z": "20",
        "L": "77500.5",
        "ma": "BTC",
        "N": "BTC",
        "n": "0.00001290",
        "T": 1591274595442,
        "t": 12345,
        "rp": "0",
        "b": "0",
        "a": "0",
        "m": False,
        "R": False,
        "wt": "CONTRACT_PRICE",
        "ot": "MARKET",
        "ps": "BOTH",
        "cp": False,
        "pP": False,
        "V": "EXPIRE_MAKER",
        "pm": "NONE",
        "er": "0",
    },
}
ACCOUNT_UPDATE = {
    "e": "ACCOUNT_UPDATE",
    "E": 1564745798939,
    "T": 1564745798938,
    "i": "SfsR",
    "a": {
        "m": "ORDER",
        "B": [{"a": "BTC", "wb": "1.00012345", "cw": "1.00012345", "bc": "0"}],
        "P": [
            {
                "s": "BTCUSD_PERP",
                "pa": "-20",
                "ep": "77500.5",
                "cr": "0",
                "up": "-0.00000100",
                "mt": "cross",
                "iw": "0.00000000",
                "ps": "BOTH",
            }
        ],
    },
}
MARK_PRICE_UPDATE = {
    "e": "markPriceUpdate",
    "E": 1596095725000,
    "s": "BTCUSD_PERP",
    "p": "10934.62615417",
    "P": "10962.17178236",
    "i": "10933.62615417",
    "r": "0.00010000",
    "T": 1596096000000,
    "st": 2,
}


def test_parse_official_payloads() -> None:
    ev = parse_order_trade_update(ORDER_TRADE_UPDATE)
    assert ev.client_order_id.startswith("biq-") and ev.status is OrderStatus.FILLED
    assert ev.cum_filled_qty == Decimal("20") and ev.avg_price == Decimal("77500.5")
    assert ev.commission == Decimal("0.00001290") and ev.order_id == "8888888"
    acc = parse_account_update(ACCOUNT_UPDATE)
    assert acc.wallet_balance == Decimal("1.00012345")
    assert (
        acc.positions[0].position_amt == Decimal("-20") and acc.positions[0].symbol == "BTCUSD_PERP"
    )
    mp = parse_mark_price_update(MARK_PRICE_UPDATE)
    assert mp.mark_price == Decimal("10934.62615417") and mp.funding_rate == Decimal("0.00010000")
    assert mp.next_funding_time is not None


class _Server:
    """Local WebSocket server that replays scripted messages, optionally dropping the connection."""

    def __init__(self, script: list[Any], close_after: int | None = None) -> None:
        self.script = script
        self.close_after = close_after
        self.connections = 0
        self.paths: list[str] = []
        self.port = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None

    async def _handler(self, ws: Any) -> None:
        self.connections += 1
        self.paths.append(ws.request.path)
        for sent, msg in enumerate(self.script, start=1):
            await ws.send(json.dumps(msg))
            if self.close_after is not None and sent >= self.close_after:
                await ws.close()
                return
        # keep the connection open until the client goes away
        with contextlib.suppress(Exception):
            await ws.wait_closed()

    async def _main(self) -> None:
        self._stop = asyncio.Event()
        async with serve(self._handler, "127.0.0.1", 0) as server:
            self.port = next(iter(server.sockets)).getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    def start(self) -> None:
        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._main())

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5)

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(5)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


class _FakeProvider:
    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self.keys: list[str] = []
        self.keepalives = 0

    def start_user_stream(self) -> str:
        key = f"listenkey{len(self.keys) + 1}"
        self.keys.append(key)
        return key

    def keepalive_user_stream(self) -> None:
        self.keepalives += 1


def _wait(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_user_data_stream_receives_events_and_uses_listen_key() -> None:
    server = _Server([ORDER_TRADE_UPDATE, ACCOUNT_UPDATE])
    server.start()
    provider = _FakeProvider(server.url)
    events: list[tuple[str, Any]] = []
    stream = UserDataStream(provider, on_event=lambda k, e: events.append((k, e)), recv_timeout=0.2)
    stream.start()
    try:
        assert _wait(lambda: len(events) >= 2)
        assert server.paths[0] == "/ws/listenkey1"
        ev = stream.latest_order_event("biq-s-20260901T000500-abcd1234")
        assert ev is not None and ev.status is OrderStatus.FILLED
        pos = stream.last_position("BTCUSD_PERP")
        assert pos is not None and pos.position_amt == Decimal("-20")
        assert stream.last_wallet_balance() == Decimal("1.00012345")
        st = stream.status()
        assert st.connected and st.messages == 2 and st.last_message_at is not None
    finally:
        stream.stop()
        server.stop()


def test_user_data_stream_reconnects_after_disconnect_and_renews_expired_key() -> None:
    server = _Server([{"e": "listenKeyExpired", "E": 1234567890}], close_after=1)
    server.start()
    provider = _FakeProvider(server.url)
    stream = UserDataStream(provider, recv_timeout=0.2, backoff_max=0.2)
    stream.start()
    try:
        assert _wait(lambda: server.connections >= 3, timeout=10)
        # each reconnect after listenKeyExpired must request a NEW listenKey
        assert len(provider.keys) >= 3
        assert server.paths[1] == "/ws/listenkey2"
        assert stream.status().reconnects >= 2
    finally:
        stream.stop()
        server.stop()


def test_mark_price_stream_and_bundle_status() -> None:
    server = _Server([MARK_PRICE_UPDATE])
    server.start()
    stream = MarkPriceStream(server.url, "BTCUSD_PERP", recv_timeout=0.2)
    bundle = StreamBundle(user=None, mark=stream)
    bundle.start()
    try:
        assert _wait(lambda: stream.last() is not None)
        assert server.paths[0] == "/ws/btcusd_perp@markPrice"
        last = stream.last()
        assert last is not None and last.mark_price == Decimal("10934.62615417")
        assert bundle.connected()
        from datetime import UTC, datetime

        sil = bundle.max_silence_seconds(datetime.now(tz=UTC))
        assert sil is not None and sil < 5
    finally:
        bundle.stop()
        server.stop()


def test_stream_survives_malformed_messages() -> None:
    server = _Server(["not json", {"e": "unknownEvent"}, MARK_PRICE_UPDATE])
    server.start()
    stream = MarkPriceStream(server.url, "BTCUSD_PERP", recv_timeout=0.2)
    stream.start()
    try:
        assert _wait(lambda: stream.last() is not None)
        assert stream.status().connected
    finally:
        stream.stop()
        server.stop()


# --------------------------------------------------------------------------- dead-socket recovery
# A dropped connection does not have to raise. When the host suspends, the socket is gone but
# recv() only keeps timing out, so `connected` stays True and the stream is silently dead. That
# is what the _Server below reproduces for free: it sends its script and then holds the socket
# open saying nothing — indistinguishable, from the client's side, from a laptop that slept.


def test_a_silent_but_connected_stream_forces_a_reconnect() -> None:
    server = _Server([MARK_PRICE_UPDATE])
    server.start()
    stream = MarkPriceStream(
        server.url,
        "BTCUSD_PERP",
        recv_timeout=0.2,
        backoff_max=0.2,
        stale_reconnect_seconds=1.0,
    )
    stream.start()
    try:
        assert _wait(lambda: server.connections >= 2, timeout=10), (
            "the client sat on a dead socket instead of reconnecting"
        )
        st = stream.status()
        assert st.forced_reconnects >= 1
        assert "no message" in st.last_reconnect_reason
    finally:
        stream.stop()
        server.stop()


def test_an_idle_user_stream_is_not_judged_by_silence() -> None:
    """An event-driven stream is quiet by design; counting that as silence fires the guard
    for the one reason it is not meant to catch — a live account that simply had no fills."""
    from datetime import UTC, datetime, timedelta

    server = _Server([])  # connects, then never says anything
    server.start()
    provider = _FakeProvider(server.url)
    user = UserDataStream(provider, recv_timeout=0.2)
    bundle = StreamBundle(user=user, mark=None)
    bundle.started_at = datetime.now(tz=UTC) - timedelta(hours=6)
    bundle.start()
    try:
        assert _wait(lambda: user.status().connected)
        assert bundle.max_silence_seconds(datetime.now(tz=UTC)) is None
    finally:
        bundle.stop()
        server.stop()


def test_a_suspend_gap_forces_a_reconnect_even_while_the_socket_says_connected() -> None:
    stream = MarkPriceStream("ws://unused", "BTCUSD_PERP", suspend_gap_seconds=60.0)
    now = time.time()
    # a loop pass that cannot take longer than recv_timeout took 40 minutes: the host slept
    assert "suspended" in stream._reconnect_reason(now - 3000.0, now - 2400.0)
    # a normal pass is left alone
    assert stream._reconnect_reason(now - 1.0, now - 0.2) == ""


def test_a_reconnected_user_stream_forgets_its_position_snapshot() -> None:
    """A gap may have swallowed an ACCOUNT_UPDATE, so the snapshot is no longer evidence.
    Keeping it would let the WS-vs-REST guard latch SAFE_HOLD over "we were not listening"."""
    server = _Server([ACCOUNT_UPDATE], close_after=1)
    server.start()
    provider = _FakeProvider(server.url)
    stream = UserDataStream(provider, recv_timeout=0.2, backoff_max=0.2)
    stream.start()
    try:
        assert _wait(lambda: stream.status().reconnects >= 1, timeout=10)
        assert _wait(lambda: stream.last_position("BTCUSD_PERP") is None, timeout=5)
    finally:
        stream.stop()
        server.stop()


@pytest.mark.parametrize("payload", [ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, MARK_PRICE_UPDATE])
def test_payloads_round_trip_json(payload: dict[str, Any]) -> None:
    assert json.loads(json.dumps(payload)) == payload
    assert websockets.__version__
