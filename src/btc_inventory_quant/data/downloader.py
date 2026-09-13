"""Public-data downloader for Binance COIN-M (no API key required).

Fetches ``/dapi/v1/klines``, ``/dapi/v1/markPriceKlines``, ``/dapi/v1/indexPriceKlines``,
``/dapi/v1/fundingRate``, ``/dapi/v1/exchangeInfo`` and ``/dapi/v1/premiumIndex`` with pagination and
stores VERBATIM JSON pages (``page_NNN.json``) plus a manifest with SHA-256 checksums of the canonical
JSON, matching the layout consumed by :mod:`btc_inventory_quant.data.market_data`.

Run it from a machine that can reach ``https://dapi.binance.com`` (the development sandbox used to
build this project could not — the initial snapshot was collected through a browser session and
verified with the same checksum scheme).

Usage::

    python -m btc_inventory_quant data --raw-dir data/raw download --symbol BTCUSD_PERP
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://dapi.binance.com"
DEFAULT_START_MS = 1_596_240_000_000  # 2020-08-01T00:00:00Z (before BTCUSD_PERP onboarding)
DAY_MS = 86_400_000


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(slots=True)
class PageRecord:
    file: str
    url: str
    rows: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "url": self.url,
            "rows": self.rows,
            "sha256": self.sha256,
            "verified": True,
        }


@dataclass(slots=True)
class DatasetRecord:
    name: str
    pages: list[PageRecord] = field(default_factory=list)
    total_rows: int = 0
    first_time_ms: int | None = None
    last_time_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages": [p.to_dict() for p in self.pages],
            "total_rows": self.total_rows,
            "first_time_ms": self.first_time_ms,
            "last_time_ms": self.last_time_ms,
            "first_date": _iso(self.first_time_ms),
            "last_date": _iso(self.last_time_ms),
        }


def _iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


class BinanceCoinMPublicDownloader:
    def __init__(
        self,
        raw_dir: Path,
        symbol: str = "BTCUSD_PERP",
        pair: str = "BTCUSD",
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        sleep_seconds: float = 0.25,
        max_retries: int = 5,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.symbol = symbol
        self.pair = pair
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(
            timeout=30.0, headers={"User-Agent": "btc-inventory-quant/0.1"}
        )
        self.sleep_seconds = sleep_seconds
        self.max_retries = max_retries
        self.manifest: dict[str, Any] = {"datasets": {}, "snapshots": {}}

    # --- HTTP -----------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, str]:
        url = f"{self.base_url}{path}"
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.get(url, params=params)
                if resp.status_code in (418, 429):
                    retry_after = float(resp.headers.get("Retry-After", delay * 10))
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp.json(), str(resp.request.url)
            except (httpx.HTTPError, ValueError):
                if attempt >= self.max_retries:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise RuntimeError("unreachable")  # pragma: no cover

    # --- paginated datasets ---------------------------------------------
    def _paginate(
        self,
        name: str,
        path: str,
        params: dict[str, Any],
        time_key: int | str,
        limit: int,
        start_ms: int,
        end_ms: int | None,
    ) -> DatasetRecord:
        out_dir = self.raw_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("page_*.json"):
            old.unlink()
        record = DatasetRecord(name=name)
        cursor = start_ms
        page_no = 0
        while True:
            q = dict(params)
            q["startTime"] = cursor
            q["limit"] = limit
            if end_ms is not None:
                q["endTime"] = end_ms
            payload, url = self._get(path, q)
            if not isinstance(payload, list):
                raise RuntimeError(f"{name}: unexpected payload {str(payload)[:200]}")
            if not payload:
                break
            page_no += 1
            file = out_dir / f"page_{page_no:03d}.json"
            file.write_text(
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
            )
            record.pages.append(
                PageRecord(
                    file=str(file.relative_to(self.raw_dir)),
                    url=url,
                    rows=len(payload),
                    sha256=canonical_sha256(payload),
                )
            )
            record.total_rows += len(payload)
            first_t = int(payload[0][time_key])
            last_t = int(payload[-1][time_key])
            record.first_time_ms = first_t if record.first_time_ms is None else record.first_time_ms
            record.last_time_ms = last_t
            if len(payload) < limit:
                break
            cursor = last_t + 1
            time.sleep(self.sleep_seconds)
        self.manifest["datasets"][name] = record.to_dict()
        return record

    def download_klines(
        self, start_ms: int = DEFAULT_START_MS, end_ms: int | None = None
    ) -> DatasetRecord:
        return self._paginate(
            "klines_1d",
            "/dapi/v1/klines",
            {"symbol": self.symbol, "interval": "1d"},
            0,
            1500,
            start_ms,
            end_ms,
        )

    def download_mark_klines(
        self, start_ms: int = DEFAULT_START_MS, end_ms: int | None = None
    ) -> DatasetRecord:
        return self._paginate(
            "mark_price_klines_1d",
            "/dapi/v1/markPriceKlines",
            {"symbol": self.symbol, "interval": "1d"},
            0,
            1500,
            start_ms,
            end_ms,
        )

    def download_index_klines(
        self, start_ms: int = DEFAULT_START_MS, end_ms: int | None = None
    ) -> DatasetRecord:
        return self._paginate(
            "index_price_klines_1d",
            "/dapi/v1/indexPriceKlines",
            {"pair": self.pair, "interval": "1d"},
            0,
            1500,
            start_ms,
            end_ms,
        )

    def download_funding(
        self, start_ms: int = DEFAULT_START_MS, end_ms: int | None = None
    ) -> DatasetRecord:
        return self._paginate(
            "funding_rate",
            "/dapi/v1/fundingRate",
            {"symbol": self.symbol},
            "fundingTime",
            1000,
            start_ms,
            end_ms,
        )

    def download_mark_klines_8h(
        self, start_ms: int = DEFAULT_START_MS, end_ms: int | None = None
    ) -> DatasetRecord:
        """Optional: exact mark prices at funding times (00/08/16 UTC bar opens)."""
        return self._paginate(
            "mark_price_klines_8h",
            "/dapi/v1/markPriceKlines",
            {"symbol": self.symbol, "interval": "8h"},
            0,
            1500,
            start_ms,
            end_ms,
        )

    # --- snapshots -------------------------------------------------------
    def _snapshot(self, name: str, path: str, params: dict[str, Any] | None = None) -> None:
        payload, url = self._get(path, params)
        file = self.raw_dir / f"{name}.json"
        file.write_text(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
        )
        self.manifest["snapshots"][name] = {
            "file": file.name,
            "url": url,
            "sha256": canonical_sha256(payload),
        }

    def download_snapshots(self) -> None:
        self._snapshot("exchange_info", "/dapi/v1/exchangeInfo")
        self._snapshot("premium_index", "/dapi/v1/premiumIndex", {"symbol": self.symbol})
        self._snapshot("server_time", "/dapi/v1/time")
        try:
            self._snapshot("funding_info", "/dapi/v1/fundingInfo")
        except httpx.HTTPError as exc:  # optional endpoint
            self.manifest["snapshots"]["funding_info"] = {"error": str(exc)}

    def download_all(
        self, start_ms: int = DEFAULT_START_MS, include_8h_mark: bool = False
    ) -> dict[str, Any]:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.download_snapshots()
        self.download_klines(start_ms)
        self.download_mark_klines(start_ms)
        self.download_index_klines(start_ms)
        self.download_funding(start_ms)
        if include_8h_mark:
            self.download_mark_klines_8h(start_ms)
        self.manifest["fetched_at_utc"] = datetime.now(tz=UTC).isoformat()
        self.manifest["source"] = {
            "exchange": "Binance COIN-M public REST",
            "base_url": self.base_url,
            "symbol": self.symbol,
            "pair": self.pair,
        }
        self.manifest["collection_method"] = (
            "btc_inventory_quant.data.downloader (httpx, verbatim JSON, canonical sha256)"
        )
        (self.raw_dir / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return self.manifest


USDT_M_BASE_URL = "https://fapi.binance.com"
USDT_M_START_MS = 1_567_900_800_000  # 2019-09-08T00:00:00Z (BTCUSDT perpetual onboarding)


class BinanceUsdtMPublicDownloader(BinanceCoinMPublicDownloader):
    """USDT-margined ``BTCUSDT`` perpetual — price and funding history back to 2019-09.

    This exists for ONE purpose: the V5 sample-extension study
    (:doc:`../../docs/strategy_v5_sample_extension`). COIN-M ``BTCUSD_PERP`` only starts at
    2020-08-11, so the dataset contains one major drawdown episode; BTCUSDT reaches back far
    enough to add the March 2020 crash **with real Binance funding rates** rather than assumed
    ones.

    It is NOT a drop-in substitute for the COIN-M data. BTCUSDT is a *linear* contract: its PnL
    maths differ and its funding rate is its own series, not ours. Only the price and funding-rate
    series are usable, and only after the overlap-period proxy check in the V5 study §3 passes.
    Nothing in the live/backtest path reads this directory.
    """

    def __init__(self, raw_dir: Path, symbol: str = "BTCUSDT", **kw: Any) -> None:
        kw.setdefault("base_url", USDT_M_BASE_URL)
        super().__init__(raw_dir, symbol=symbol, pair=symbol, **kw)

    def download_klines(
        self, start_ms: int = USDT_M_START_MS, end_ms: int | None = None
    ) -> DatasetRecord:
        return self._paginate(
            "klines_1d", "/fapi/v1/klines", {"symbol": self.symbol, "interval": "1d"},
            0, 1500, start_ms, end_ms,
        )  # fmt: skip

    def download_mark_klines(
        self, start_ms: int = USDT_M_START_MS, end_ms: int | None = None
    ) -> DatasetRecord:
        return self._paginate(
            "mark_price_klines_1d", "/fapi/v1/markPriceKlines",
            {"symbol": self.symbol, "interval": "1d"}, 0, 1500, start_ms, end_ms,
        )  # fmt: skip

    def download_index_klines(
        self, start_ms: int = USDT_M_START_MS, end_ms: int | None = None
    ) -> DatasetRecord:
        """Not fetched: a linear contract has no pair-level index series we use.

        Recorded in the manifest as an empty dataset rather than omitted, so that a reader can
        tell "deliberately not collected" from "collection failed".
        """
        record = DatasetRecord(name="index_price_klines_1d")
        self.manifest["datasets"][record.name] = {
            **record.to_dict(),
            "skipped": "not applicable to a linear contract",
        }
        return record

    def download_funding(
        self, start_ms: int = USDT_M_START_MS, end_ms: int | None = None
    ) -> DatasetRecord:
        return self._paginate(
            "funding_rate", "/fapi/v1/fundingRate", {"symbol": self.symbol},
            "fundingTime", 1000, start_ms, end_ms,
        )  # fmt: skip

    def download_mark_klines_8h(
        self, start_ms: int = USDT_M_START_MS, end_ms: int | None = None
    ) -> DatasetRecord:
        return self._paginate(
            "mark_price_klines_8h", "/fapi/v1/markPriceKlines",
            {"symbol": self.symbol, "interval": "8h"}, 0, 1500, start_ms, end_ms,
        )  # fmt: skip

    def download_snapshots(self) -> None:
        self._snapshot("exchange_info", "/fapi/v1/exchangeInfo")
        self._snapshot("premium_index", "/fapi/v1/premiumIndex", {"symbol": self.symbol})
        self._snapshot("server_time", "/fapi/v1/time")

    def download_all(
        self, start_ms: int = USDT_M_START_MS, include_8h_mark: bool = False
    ) -> dict[str, Any]:
        manifest = super().download_all(start_ms=start_ms, include_8h_mark=include_8h_mark)
        manifest["source"]["exchange"] = "Binance USDT-M public REST"
        manifest["source"]["caveat"] = (
            "LINEAR contract. Price and funding-rate series only; inverse PnL maths do not carry "
            "over. Usable for the V5 study only if docs/strategy_v5_sample_extension.md "
            "section 3 proxy gate passes."
        )
        (self.raw_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return manifest


def _resolve_page_path(raw_dir: Path, file: str) -> Path | None:
    """Manifests may store paths relative to raw_dir or to the repository root."""
    candidates = [raw_dir / file, Path(file)]
    rel = Path(file)
    if raw_dir.name in rel.parts:
        idx = rel.parts.index(raw_dir.name)
        candidates.append(raw_dir.joinpath(*rel.parts[idx + 1 :]))
    for c in candidates:
        if c.exists():
            return c
    return None


def verify_manifest(raw_dir: Path) -> list[str]:
    """Re-hash every page listed in the manifest; return a list of mismatches (empty = OK)."""
    manifest = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    for name, ds in manifest.get("datasets", {}).items():
        for page in ds.get("pages", []):
            file = _resolve_page_path(raw_dir, page["file"])
            if file is None:
                problems.append(f"{name}: missing {page['file']}")
                continue
            payload = json.loads(file.read_text(encoding="utf-8"))
            if len(payload) != page["rows"]:
                problems.append(f"{name}: {file.name} rows {len(payload)} != {page['rows']}")
            if canonical_sha256(payload) != page["sha256"]:
                problems.append(f"{name}: {file.name} sha256 mismatch")
    return problems
