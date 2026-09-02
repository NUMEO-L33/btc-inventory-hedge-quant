"""Alert channels (log / Telegram / email). A failing alert NEVER stops the trading loop."""

from __future__ import annotations

import smtplib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from btc_inventory_quant.config.settings import Settings
from btc_inventory_quant.monitoring.logging import get_logger, mask_mapping

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Alert:
    ts: datetime
    event_type: str
    level: str
    message: str
    payload: dict[str, Any] | None = None

    def text(self) -> str:
        head = f"[{self.level}] {self.event_type}: {self.message}"
        if self.payload:
            return (
                head
                + "\n"
                + "\n".join(f"  {k}: {v}" for k, v in mask_mapping(self.payload).items())
            )
        return head


class AlertChannel(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...


class LogChannel:
    name = "log"

    def send(self, alert: Alert) -> None:
        fn = {"CRITICAL": log.critical, "ERROR": log.error, "WARNING": log.warning}.get(
            alert.level, log.info
        )
        fn(f"ALERT {alert.event_type}: {alert.message}", fields=alert.payload or {})


class TelegramChannel:
    name = "telegram"

    def __init__(
        self, bot_token: str, chat_id: str, timeout: float = 5.0, client: httpx.Client | None = None
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._client = client or httpx.Client(timeout=timeout)

    def send(self, alert: Alert) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        self._client.post(
            url, json={"chat_id": self._chat_id, "text": alert.text()[:4000]}
        ).raise_for_status()


class EmailChannel:
    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        user: str | None,
        password: str | None,
        to_addr: str,
        timeout: float = 10.0,
    ) -> None:
        self.host, self.port, self.user, self.password, self.to_addr, self.timeout = (
            host,
            port,
            user,
            password,
            to_addr,
            timeout,
        )

    def send(self, alert: Alert) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[BIQ {alert.level}] {alert.event_type}"
        msg["From"] = self.user or "btc-inventory-quant@localhost"
        msg["To"] = self.to_addr
        msg.set_content(alert.text())
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
            s.starttls()
            if self.user and self.password:
                s.login(self.user, self.password)
            s.send_message(msg)


class Alerter:
    """Fan-out with isolation: each channel runs in its own thread with try/except."""

    def __init__(
        self,
        channels: list[AlertChannel],
        recorder: Callable[[Alert], None] | None = None,
        async_send: bool = True,
    ) -> None:
        self.channels = channels
        self.recorder = recorder
        self.async_send = async_send
        self.failures: dict[str, int] = {}
        self.sent: list[Alert] = []

    def notify(
        self,
        event_type: str,
        message: str,
        level: str = "INFO",
        payload: dict[str, Any] | None = None,
    ) -> None:
        alert = Alert(datetime.now(tz=UTC), event_type, level, message, payload)
        self.sent.append(alert)
        if len(self.sent) > 1000:
            del self.sent[:-500]
        if self.recorder is not None:
            try:
                self.recorder(alert)
            except Exception as exc:  # alert persistence must never raise
                log.warning("alert recorder failed", fields={"error": str(exc)})
        for ch in self.channels:
            if self.async_send and ch.name != "log":
                threading.Thread(target=self._send_one, args=(ch, alert), daemon=True).start()
            else:
                self._send_one(ch, alert)

    def _send_one(self, channel: AlertChannel, alert: Alert) -> None:
        try:
            channel.send(alert)
        except Exception as exc:  # never propagate alert failures
            self.failures[channel.name] = self.failures.get(channel.name, 0) + 1
            log.warning("alert channel failed", fields={"channel": channel.name, "error": str(exc)})


def build_alerter(
    settings: Settings,
    channel_names: tuple[str, ...],
    recorder: Callable[[Alert], None] | None = None,
) -> Alerter:
    channels: list[AlertChannel] = []
    for name in channel_names:
        if name == "log":
            channels.append(LogChannel())
        elif name == "telegram":
            if settings.telegram_bot_token and settings.telegram_chat_id:
                channels.append(
                    TelegramChannel(
                        settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
                    )
                )
            else:
                log.warning("telegram alert channel configured but credentials missing; skipping")
        elif name == "email":
            if settings.smtp_host and settings.alert_email_to:
                channels.append(
                    EmailChannel(
                        settings.smtp_host,
                        settings.smtp_port,
                        settings.smtp_user,
                        settings.smtp_password.get_secret_value()
                        if settings.smtp_password
                        else None,
                        settings.alert_email_to,
                    )
                )
            else:
                log.warning("email alert channel configured but SMTP settings missing; skipping")
    if not channels:
        channels.append(LogChannel())
    return Alerter(channels, recorder=recorder)
