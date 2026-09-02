"""Runtime metrics snapshot (prompt §16), persisted to the DB and a JSON file for ``status``."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from btc_inventory_quant.storage.database import Database

METRICS_KEY = "metrics"


@dataclass(slots=True)
class MetricsSnapshot:
    ts: str = ""
    mode: str = ""
    system_state: str = "NORMAL"
    equity_btc: str | None = None
    equity_usd: str | None = None
    wallet_balance_btc: str | None = None
    last_price: str | None = None
    mark_price: str | None = None
    short_contracts: str | None = None
    short_notional_usd: str | None = None
    avg_entry_price: str | None = None
    target_hedge_ratio: str | None = None
    effective_hedge_ratio: str | None = None
    hedge_ratio_error: str | None = None
    realized_pnl_btc: str | None = None
    unrealized_pnl_btc: str | None = None
    cumulative_funding_btc: str | None = None
    cumulative_fees_btc: str | None = None
    daily_pnl_btc: str | None = None
    daily_pnl_usd: str | None = None
    max_drawdown_usd: str | None = None
    peak_equity_usd: str | None = None
    last_data_time: str | None = None
    last_order_time: str | None = None
    websocket_connected: bool | None = None
    api_error_count: int = 0
    order_reject_count: int = 0
    reconciliation_ok: bool | None = None
    last_reconciliation_time: str | None = None
    liquidation_price_estimate: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MetricsRegistry:
    def __init__(self, db: Database, file_path: Path | None = None) -> None:
        self.db = db
        self.file_path = file_path
        self.current = MetricsSnapshot()
        self._peak_usd: Decimal | None = None

    def update(self, **fields: Any) -> MetricsSnapshot:
        for k, v in fields.items():
            if k == "extra":
                self.current.extra.update(v)
            elif hasattr(self.current, k):
                setattr(self.current, k, str(v) if isinstance(v, Decimal) else v)
            else:
                self.current.extra[k] = str(v) if isinstance(v, Decimal) else v
        if "equity_usd" in fields and fields["equity_usd"] is not None:
            eq = Decimal(str(fields["equity_usd"]))
            self._peak_usd = eq if self._peak_usd is None else max(self._peak_usd, eq)
            self.current.peak_equity_usd = str(self._peak_usd)
            self.current.max_drawdown_usd = str(
                (eq / self._peak_usd - 1) if self._peak_usd > 0 else Decimal(0)
            )
        self.current.ts = datetime.now(tz=UTC).isoformat()
        return self.current

    def persist(self) -> None:
        payload = self.current.to_dict()
        self.db.set_state(METRICS_KEY, payload)
        if self.file_path is not None:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
            )

    @staticmethod
    def load(db: Database) -> dict[str, Any] | None:
        raw = db.get_state(METRICS_KEY)
        return dict(raw) if raw else None
