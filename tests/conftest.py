from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from btc_inventory_quant.config.schema import AppConfig, RiskLimits
from btc_inventory_quant.domain.inverse_contract import ContractSpec

FIXTURES = Path(__file__).parent / "fixtures"


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
