"""SAFE_HOLD kill switch: stop new orders, cancel open orders, KEEP the position, alert.

The state is persisted in the database so that a restart cannot silently resume trading.
Release requires an explicit operator action (CLI ``safe-hold --release``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from btc_inventory_quant.monitoring.alerts import Alerter
from btc_inventory_quant.monitoring.logging import get_logger
from btc_inventory_quant.storage.database import Database

log = get_logger(__name__)
STATE_KEY = "kill_switch"


class SystemState(StrEnum):
    NORMAL = "NORMAL"
    SAFE_HOLD = "SAFE_HOLD"


@dataclass(frozen=True, slots=True)
class KillSwitchStatus:
    state: SystemState
    reason: str
    since: datetime | None
    details: dict[str, Any]


class KillSwitch:
    def __init__(self, db: Database, alerter: Alerter) -> None:
        self.db = db
        self.alerter = alerter

    def status(self) -> KillSwitchStatus:
        raw = self.db.get_state(STATE_KEY) or {}
        since = raw.get("since")
        return KillSwitchStatus(
            state=SystemState(raw.get("state", SystemState.NORMAL.value)),
            reason=str(raw.get("reason", "")),
            since=datetime.fromisoformat(since) if since else None,
            details=dict(raw.get("details") or {}),
        )

    @property
    def engaged(self) -> bool:
        return self.status().state is SystemState.SAFE_HOLD

    def engage(self, reason: str, details: dict[str, Any] | None = None) -> None:
        if self.engaged:
            log.warning("SAFE_HOLD already engaged", fields={"reason": reason})
            return
        now = datetime.now(tz=UTC)
        self.db.set_state(
            STATE_KEY,
            {
                "state": SystemState.SAFE_HOLD.value,
                "reason": reason,
                "since": now.isoformat(),
                "details": details or {},
            },
        )
        self.db.add_event("CRITICAL", "safe_hold_engaged", reason, details)
        log.error("SAFE_HOLD engaged", fields={"reason": reason, **(details or {})})
        self.alerter.notify(
            "safe_hold_engaged", f"SAFE_HOLD: {reason}", level="CRITICAL", payload=details
        )

    def release(self, acknowledgement: str, operator: str = "cli") -> None:
        st = self.status()
        if st.state is SystemState.NORMAL:
            return
        self.db.set_state(
            STATE_KEY,
            {
                "state": SystemState.NORMAL.value,
                "reason": "",
                "since": None,
                "details": {
                    "released_by": operator,
                    "ack": acknowledgement,
                    "previous_reason": st.reason,
                },
            },
        )
        self.db.add_event(
            "WARNING",
            "safe_hold_released",
            f"released by {operator}",
            {"previous_reason": st.reason},
        )
        self.alerter.notify(
            "safe_hold_released",
            f"SAFE_HOLD released by {operator} (previous reason: {st.reason})",
            level="WARNING",
        )
