from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from btc_inventory_quant.config.schema import AppConfig, RiskLimits
from btc_inventory_quant.domain.inverse_contract import ContractSpec

FIXTURES = Path(__file__).parent / "fixtures"
RAW_SNAPSHOT = Path(__file__).parents[1] / "data" / "raw"


def raw_snapshot_present(raw: Path = RAW_SNAPSHOT) -> bool:
    """True when the real-data snapshot is here with its pages, not just its manifest.

    The public copy of this repository keeps ``manifest.json`` (what was downloaded, with
    SHA-256 per page) but not the Binance market data itself; tests that need the data skip.
    """
    manifest = raw / "manifest.json"
    if not manifest.exists():
        return False
    datasets = json.loads(manifest.read_text(encoding="utf-8")).get("datasets", {})
    return bool(datasets) and all(any((raw / name).glob("page_*.json")) for name in datasets)


@pytest.fixture(scope="session")
def exchange_info_symbol() -> dict:  # type: ignore[type-arg]
    payload = json.loads((FIXTURES / "exchange_info_btcusd_perp.json").read_text())
    return payload["symbol"]  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def spec(exchange_info_symbol: dict) -> ContractSpec:  # type: ignore[type-arg]
    return ContractSpec.from_exchange_info_symbol(exchange_info_symbol, source="fixture")


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits()


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig()


@pytest.fixture
def t0() -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC)


D = Decimal
