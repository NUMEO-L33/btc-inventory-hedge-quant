"""Structured JSON logging with secret masking.

Masked: API keys, secrets, signatures, auth headers, listen keys and full account identifiers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEYS = re.compile(
    r"(api[_-]?key|secret|signature|authorization|x-mbx-apikey|listenkey|password|token|account[_-]?id)",
    re.IGNORECASE,
)
_SIGNATURE_PARAM = re.compile(r"(signature=)([0-9a-fA-F]{8,})")
_LISTEN_KEY = re.compile(r"([A-Za-z0-9]{40,})")
_API_KEY_HEADER = re.compile(
    r"(X-MBX-APIKEY[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9]{8,})", re.IGNORECASE
)


def mask_value(value: Any) -> str:
    s = str(value)
    if len(s) <= 6:
        return "***"
    return f"{s[:3]}…{s[-2:]}"


def mask_mapping(data: Any) -> Any:
    if isinstance(data, dict):
        return {
            k: (mask_value(v) if _SENSITIVE_KEYS.search(str(k)) else mask_mapping(v))
            for k, v in data.items()
        }
    if isinstance(data, list | tuple):
        return [mask_mapping(v) for v in data]
    return data


def mask_text(text: str) -> str:
    text = _SIGNATURE_PARAM.sub(lambda m: m.group(1) + "***", text)
    text = _API_KEY_HEADER.sub(lambda m: m.group(1) + "***", text)
    return _LISTEN_KEY.sub(lambda m: mask_value(m.group(1)), text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": mask_text(record.getMessage()),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(mask_mapping(extra))
        if record.exc_info:
            payload["exc"] = mask_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{datetime.fromtimestamp(record.created, tz=UTC).strftime('%H:%M:%S')} {record.levelname:5s} {record.name}: {mask_text(record.getMessage())}"
        extra = getattr(record, "extra_fields", None)
        if extra:
            base += " " + json.dumps(mask_mapping(extra), ensure_ascii=False, default=str)
        if record.exc_info:
            base += "\n" + mask_text(self.formatException(record.exc_info))
        return base


class _ExtraAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        fields = kwargs.pop("fields", None)
        if fields:
            kwargs["extra"] = {"extra_fields": fields}
        return msg, kwargs


def configure_logging(level: str | None = None, json_output: bool | None = None) -> None:
    lvl = (level or os.environ.get("BIQ_LOG_LEVEL") or "INFO").upper()
    use_json = (
        json_output
        if json_output is not None
        else os.environ.get("BIQ_LOG_JSON", "false").lower() == "true"
    )
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if use_json else TextFormatter())
    root.addHandler(handler)
    root.setLevel(lvl)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)


def get_logger(name: str) -> _ExtraAdapter:
    return _ExtraAdapter(logging.getLogger(name), {})
