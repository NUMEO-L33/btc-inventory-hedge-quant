from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from btc_inventory_quant.data.downloader import (
    USDT_M_START_MS,
    BinanceCoinMPublicDownloader,
    BinanceUsdtMPublicDownloader,
    verify_manifest,
)
from btc_inventory_quant.data.funding_data import (
    FundingDataError,
    load_funding_events,
    normalise_funding_time,
)
from btc_inventory_quant.data.market_data import MarketDataError, build_daily_dataset
from btc_inventory_quant.data.repositories import load_contract_spec

DAY = 86_400_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % DAY)  # aligned to 00:00 UTC


def _kline(open_ms: int, o: float, h: float, low: float, c: float) -> list:  # type: ignore[type-arg]
    return [
        open_ms,
        f"{o:.1f}",
        f"{h:.1f}",
        f"{low:.1f}",
        f"{c:.1f}",
        "1000",
        open_ms + DAY - 1,
        "10.5",
        100,
        "500",
        "5.2",
        "0",
    ]


def _write_pages(directory: Path, rows: list, per_page: int = 3) -> None:  # type: ignore[type-arg]
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(rows), per_page):
        (directory / f"page_{i // per_page + 1:03d}.json").write_text(
            json.dumps(rows[i : i + per_page])
        )


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    rows = [_kline(T0 + i * DAY, 100 + i, 105 + i, 95 + i, 101 + i) for i in range(7)]
    _write_pages(tmp_path / "klines_1d", rows)
    _write_pages(
        tmp_path / "mark_price_klines_1d",
        [_kline(T0 + i * DAY, 100.2 + i, 105 + i, 95 + i, 101.1 + i) for i in range(7)],
    )
    _write_pages(
        tmp_path / "index_price_klines_1d",
        [_kline(T0 + i * DAY, 100.1 + i, 105 + i, 95 + i, 101.05 + i) for i in range(7)],
    )
    funding = []
    for i in range(7):
        for h in (0, 8, 16):
            funding.append(
                {
                    "symbol": "BTCUSD_PERP",
                    "fundingTime": T0 + i * DAY + h * 3_600_000 + (7 if h == 0 else 0),
                    "fundingRate": "0.00010000",
                    "markPrice": "" if i < 3 else "101.0",
                }
            )
    _write_pages(tmp_path / "funding_rate", funding, per_page=10)
    return tmp_path


def test_incomplete_last_candle_is_dropped(raw_dir: Path) -> None:
    as_of = datetime.fromtimestamp((T0 + 6 * DAY + 3_600_000) / 1000, tz=UTC)  # inside day 7
    ds = build_daily_dataset(raw_dir, as_of=as_of)
    assert len(ds) == 6
    assert ds.quality["klines_1d"].dropped_incomplete == 1
    later = datetime.fromtimestamp((T0 + 7 * DAY + 1000) / 1000, tz=UTC)
    assert len(build_daily_dataset(raw_dir, as_of=later)) == 7


def test_gap_and_ohlc_validation(tmp_path: Path) -> None:
    rows = [
        _kline(T0, 100, 105, 95, 101),
        _kline(T0 + 2 * DAY, 100, 105, 95, 101),
        _kline(T0 + 3 * DAY, 100, 90, 95, 101),
    ]
    _write_pages(tmp_path / "klines_1d", rows)
    _write_pages(tmp_path / "mark_price_klines_1d", rows)
    ds = build_daily_dataset(
        tmp_path, as_of=datetime.fromtimestamp((T0 + 10 * DAY) / 1000, tz=UTC), require_index=False
    )
    q = ds.quality["klines_1d"]
    assert len(q.gaps) == 1
    assert q.ohlc_violations == 1
    with pytest.raises(MarketDataError):
        build_daily_dataset(tmp_path, as_of=datetime.now(tz=UTC), require_index=True)


def test_exact_decimal_prices_available(raw_dir: Path) -> None:
    ds = build_daily_dataset(raw_dir, as_of=datetime.fromtimestamp((T0 + 10 * DAY) / 1000, tz=UTC))
    ts = ds.frame.index[0]
    assert ds.dec(ts, "open") == Decimal("100.0")
    assert ds.dec(ts, "mark_close") == Decimal("101.1")


def test_funding_loading_and_normalisation(raw_dir: Path) -> None:
    events, report = load_funding_events(raw_dir / "funding_rate")
    assert report.rows == 21
    assert report.irregular_intervals == ()
    assert events[0].funding_time.minute == 0 and events[0].funding_time.microsecond == 0
    assert events[0].raw_funding_time_ms % 3_600_000 == 7
    assert report.rows_with_mark_price == 12
    assert events[0].mark_price is None
    assert events[-1].mark_price == Decimal("101.0")
    with pytest.raises(FundingDataError):
        normalise_funding_time(T0 + 30 * 60 * 1000)  # 30 minutes off the hour


def test_contract_spec_from_snapshot(tmp_path: Path) -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "exchange_info_btcusd_perp.json").read_text()
    )
    snapshot = {"serverTime": 1, "symbols": [fixture["symbol"]]}
    p = tmp_path / "exchange_info.json"
    p.write_text(json.dumps(snapshot))
    spec = load_contract_spec(p, "BTCUSD_PERP")
    assert spec.contract_value_usd == Decimal("100")
    with pytest.raises(MarketDataError):
        load_contract_spec(p, "ETHUSD_PERP")


def test_downloader_paginates_and_writes_verifiable_manifest(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        path = request.url.path
        params = dict(request.url.params)
        if path == "/dapi/v1/exchangeInfo":
            return httpx.Response(200, json={"serverTime": 1, "symbols": []})
        if path == "/dapi/v1/premiumIndex":
            return httpx.Response(200, json=[{"symbol": "BTCUSD_PERP", "markPrice": "1"}])
        if path == "/dapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1})
        if path == "/dapi/v1/fundingInfo":
            return httpx.Response(200, json=[])
        start = int(params["startTime"])
        limit = int(params["limit"])

        def aligned(step: int) -> int:  # first grid point >= start (like the real API)
            return T0 + max(0, -(-(start - T0) // step)) * step

        if path == "/dapi/v1/fundingRate":
            step = 8 * 3_600_000
            end = T0 + 5 * DAY
            rows = [
                {"symbol": "BTCUSD_PERP", "fundingTime": t, "fundingRate": "0.0001"}
                for t in range(aligned(step), end, step)
            ][:limit]
            return httpx.Response(200, json=rows)
        end = T0 + 4 * DAY
        rows = [_kline(t, 1, 2, 0.5, 1.5) for t in range(aligned(DAY), end, DAY)][:limit]
        return httpx.Response(200, json=rows)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dl = BinanceCoinMPublicDownloader(tmp_path, client=client, sleep_seconds=0)
    # force small pages to exercise pagination
    dl._paginate(
        "klines_1d",
        "/dapi/v1/klines",
        {"symbol": "BTCUSD_PERP", "interval": "1d"},
        0,
        3,
        T0 - DAY,
        None,
    )
    dl._paginate(
        "funding_rate",
        "/dapi/v1/fundingRate",
        {"symbol": "BTCUSD_PERP"},
        "fundingTime",
        4,
        T0 - DAY,
        None,
    )
    dl.download_snapshots()
    dl.manifest["fetched_at_utc"] = "test"
    (tmp_path / "manifest.json").write_text(json.dumps(dl.manifest))
    assert dl.manifest["datasets"]["klines_1d"]["total_rows"] == 4
    assert len(dl.manifest["datasets"]["klines_1d"]["pages"]) == 2
    assert dl.manifest["datasets"]["funding_rate"]["total_rows"] == 15
    assert verify_manifest(tmp_path) == []
    # tamper with a page → verification fails
    page = tmp_path / "klines_1d" / "page_001.json"
    payload = json.loads(page.read_text())
    payload[0][4] = "9.9"
    page.write_text(json.dumps(payload))
    assert any("sha256" in p for p in verify_manifest(tmp_path))


def test_usdt_m_downloader_hits_fapi_and_labels_the_linear_caveat(tmp_path: Path) -> None:
    """The V5 extension pulls BTCUSDT. It must not silently look like COIN-M data."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        assert request.url.host == "fapi.binance.com", "USDT-M must not hit the COIN-M host"
        if path.endswith("exchangeInfo"):
            return httpx.Response(200, json={"serverTime": 1, "symbols": []})
        if path.endswith("premiumIndex"):
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "markPrice": "1"}])
        if path.endswith("/time"):
            return httpx.Response(200, json={"serverTime": 1})
        start = int(dict(request.url.params)["startTime"])
        if path.endswith("fundingRate"):
            if start > T0:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "fundingTime": T0, "fundingRate": "0.0001"}],
            )
        if start > T0:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[_kline(T0, 1, 2, 0.5, 1.5)])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dl = BinanceUsdtMPublicDownloader(tmp_path, client=client, sleep_seconds=0)
    manifest = dl.download_all(start_ms=T0 - DAY)

    assert all(p.startswith("/fapi/") for p in paths), paths
    assert manifest["source"]["exchange"] == "Binance USDT-M public REST"
    assert "LINEAR" in manifest["source"]["caveat"]
    assert manifest["datasets"]["funding_rate"]["total_rows"] == 1
    # a linear contract has no pair-level index series to fetch
    assert manifest["datasets"]["index_price_klines_1d"]["total_rows"] == 0
    assert verify_manifest(tmp_path) == []


def test_usdt_m_start_precedes_coin_m_listing() -> None:
    """The whole point of the extension: reach back past the COIN-M listing date."""
    assert USDT_M_START_MS < 1_597_104_000_000  # 2020-08-11, BTCUSD_PERP onboarding


def test_real_snapshot_manifest_verifies() -> None:
    raw = Path(__file__).parents[2] / "data" / "raw"
    if not (raw / "manifest.json").exists():
        pytest.skip("raw snapshot not present")
    manifest = json.loads((raw / "manifest.json").read_text())
    assert manifest.get("pages_failed_verification") in (None, [], 0)
