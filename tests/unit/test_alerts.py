"""Alerts from several agents can share one chat and still say who is talking."""

from __future__ import annotations

from typing import Any

import httpx

from btc_inventory_quant.monitoring.alerts import Alert, Alerter, LogChannel, TelegramChannel


class _Capture:
    def __init__(self) -> None:
        self.sent: list[Alert] = []

    name = "capture"

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)


def test_alert_text_names_the_source_agent_when_one_is_set() -> None:
    tagged = Alerter([LogChannel()], async_send=False, source="testnet")
    plain = Alerter([LogChannel()], async_send=False)

    tagged.notify("stale_data_blocked", "trading blocked", level="WARNING")
    plain.notify("stale_data_blocked", "trading blocked", level="WARNING")

    assert tagged.sent[0].text().startswith("[testnet] [WARNING] stale_data_blocked:")
    assert plain.sent[0].text().startswith("[WARNING] stale_data_blocked:"), (
        "no source, no prefix: single-agent output stays exactly as before"
    )


def test_telegram_message_carries_the_source_prefix() -> None:
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    channel = TelegramChannel("token", "chat", client=client)
    alerter = Alerter([channel], async_send=False, source="shadow")

    alerter.notify("safe_hold_engaged", "SAFE_HOLD: reconciliation mismatch", level="CRITICAL")

    assert posted and posted[0]["text"].startswith("[shadow] [CRITICAL] safe_hold_engaged:")
    assert "token" not in posted[0]["text"]
