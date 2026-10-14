"""Which clock each mode's guard is judged against (runtime/app.py::build_clock_reference)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx

from btc_inventory_quant.config.schema import AppConfig
from btc_inventory_quant.domain.inverse_contract import ContractSpec
from btc_inventory_quant.exchange.binance_coinm import BinanceCoinMAdapter
from btc_inventory_quant.exchange.paper import PaperExchange
from btc_inventory_quant.runtime.app import build_clock_reference
from tests.conftest import FIXTURES
from tests.simulation.test_trader_simulation import Harness

DOC_KEY = "doc-example-key"  # never a real credential
DOC_SECRET = "doc-example-secret"


def _offline_adapter(environment: str, **kw: str) -> BinanceCoinMAdapter:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"building the wiring must not touch the network: {request.url}")

    return BinanceCoinMAdapter(
        environment=environment, client=httpx.Client(transport=httpx.MockTransport(refuse)), **kw
    )


def _spec() -> ContractSpec:
    sym = json.loads((FIXTURES / "exchange_info_btcusd_perp.json").read_text())["symbol"]
    return ContractSpec.from_exchange_info_symbol(sym, "fixture")


def test_live_is_its_own_reference() -> None:
    live = _offline_adapter("production", api_key=DOC_KEY, api_secret=DOC_SECRET)
    assert build_clock_reference(AppConfig(), live) is None


def test_testnet_is_judged_against_production_without_its_credentials() -> None:
    demo = _offline_adapter("demo", api_key=DOC_KEY, api_secret=DOC_SECRET)

    ref = build_clock_reference(AppConfig(), demo)

    assert isinstance(ref, BinanceCoinMAdapter)
    assert ref.environment == "production" and ref.base_url == "https://dapi.binance.com"
    assert not ref.supports_trading, "the reference reads public /time only: no keys, ever"
    assert ref.max_retries <= 1, "a second opinion fails fast; the guard falls back"


def test_shadow_reuses_the_production_adapter_it_already_reads() -> None:
    market = _offline_adapter("production")
    shadow = PaperExchange(market, _spec(), initial_btc=Decimal("1"))

    assert build_clock_reference(AppConfig(), shadow) is market


def test_replay_and_backtests_have_no_wall_clock_to_judge(tmp_path: Path) -> None:
    h = Harness(tmp_path)  # PaperExchange over ReplayMarketData
    assert build_clock_reference(h.config, h.exchange) is None
