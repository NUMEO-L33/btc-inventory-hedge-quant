"""YAML config loading with provenance (hash, git commit, timestamp) for reproducibility."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from btc_inventory_quant import __version__
from btc_inventory_quant.config.schema import AppConfig


@dataclass(frozen=True, slots=True)
class RunMetadata:
    run_id: str
    started_at_utc: str
    code_version: str
    git_commit: str
    config_path: str
    config_sha256: str
    python_version: str
    platform: str
    seed: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _git_commit(repo_root: Path | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            cwd=repo_root,
            timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_sha256(config: AppConfig) -> str:
    """Hash of the *effective* config (defaults included) so identical runs hash identically."""
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> AppConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping: {p}")
    if overrides:
        raw = _deep_merge(raw, overrides)
    return AppConfig.model_validate(raw)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def build_run_metadata(
    config: AppConfig, config_path: str | Path, run_name: str | None = None
) -> RunMetadata:
    now = datetime.now(tz=UTC)
    cfg_hash = config_sha256(config)
    run_id = (
        f"{run_name or config.backtest.run_name}-{now.strftime('%Y%m%dT%H%M%SZ')}-{cfg_hash[:8]}"
    )
    return RunMetadata(
        run_id=run_id,
        started_at_utc=now.isoformat(),
        code_version=__version__,
        git_commit=_git_commit(),
        config_path=str(config_path),
        config_sha256=cfg_hash,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        seed=config.backtest.seed,
    )
