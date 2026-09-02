"""File-based repositories: contract spec snapshot, processed datasets, manifests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from btc_inventory_quant.config.schema import DataConfig
from btc_inventory_quant.data.funding_data import (
    FundingCalendar,
    FundingQualityReport,
    load_funding_events,
)
from btc_inventory_quant.data.market_data import DailyDataset, MarketDataError, build_daily_dataset
from btc_inventory_quant.data.settlement_marks import load_settlement_marks_if_present
from btc_inventory_quant.domain.inverse_contract import ContractSpec


def load_contract_spec(path: Path, symbol: str) -> ContractSpec:
    """Read the contract specification from an ``exchangeInfo`` snapshot file (never hard-coded)."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if symbols is None and isinstance(payload, dict) and "symbol" in payload:
        symbols = [payload["symbol"]] if isinstance(payload["symbol"], dict) else [payload]
    if not symbols:
        raise MarketDataError(f"{path}: not an exchangeInfo payload")
    for s in symbols:
        if s.get("symbol") == symbol:
            fetched = payload.get("serverTime") if isinstance(payload, dict) else None
            src = f"{path} (serverTime={fetched})"
            return ContractSpec.from_exchange_info_symbol(s, source=src)
    raise MarketDataError(f"symbol {symbol} not found in {path}")


class MarketDataRepository:
    def __init__(self, cfg: DataConfig, as_of: datetime | None = None) -> None:
        self.cfg = cfg
        self.as_of = as_of or datetime.now(tz=UTC)

    def contract_spec(self) -> ContractSpec:
        return load_contract_spec(self.cfg.exchange_info_path, self.cfg.symbol)

    def daily(self) -> DailyDataset:
        return build_daily_dataset(
            self.cfg.raw_dir,
            symbol=self.cfg.symbol,
            as_of=self.as_of,
            max_mark_index_divergence=float(self.cfg.max_mark_index_divergence),
            require_index=self.cfg.require_index_validation,
        )

    def funding(self) -> tuple[FundingCalendar, FundingQualityReport]:
        events, report = load_funding_events(self.cfg.raw_dir / "funding_rate")
        return FundingCalendar(events), report

    def settlement_marks(self) -> dict[datetime, Decimal]:
        """Exact mark at each settlement instant; ``{}`` when the 8h dataset was not downloaded."""
        return load_settlement_marks_if_present(self.cfg.raw_dir)

    def raw_manifest(self) -> dict[str, Any] | None:
        p = self.cfg.raw_dir / "manifest.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def export_processed(self, dataset: DailyDataset, out_dir: Path | None = None) -> Path:
        out = out_dir or self.cfg.processed_dir
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{self.cfg.symbol}_1d.csv"
        cols = [c for c in dataset.frame.columns if not c.endswith("_dec")]
        dataset.frame[cols].to_csv(path, index_label="open_time")
        return path


def write_quality_report(path: Path, dataset: DailyDataset, funding: FundingQualityReport) -> None:
    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "as_of": dataset.as_of.isoformat(),
        "symbol": dataset.symbol,
        "rows": len(dataset),
        "start": dataset.start.isoformat(),
        "end": dataset.end.isoformat(),
        "market": {k: v.to_dict() for k, v in dataset.quality.items()},
        "funding": funding.to_dict(),
        "notes": dataset.frame.attrs.get("notes", []),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def frame_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    out = df.reset_index()
    return out.to_dict(orient="records")  # type: ignore[return-value]
