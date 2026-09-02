"""Live-trading gate (prompt §9). EVERY condition must hold or real orders are refused."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from btc_inventory_quant.config.schema import AppConfig, RunMode
from btc_inventory_quant.config.settings import Settings


@dataclass(slots=True)
class GateReport:
    passed: bool
    failures: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "failures": self.failures, "checks": self.checks}


@dataclass(frozen=True, slots=True)
class RuntimeEvidence:
    """Facts gathered by the trader before asking the gate (all default to failing)."""

    api_key_present: bool = False
    api_permissions_ok: bool = False
    time_synced: bool = False
    clock_offset_ms: int | None = None
    position_consistent: bool = False
    no_unconfirmed_orders: bool = False
    hedge_ratio_within_limits: bool = False
    market_data_fresh: bool = False
    contract_spec_verified: bool = False
    position_mode_one_way: bool = False
    equity_btc: Decimal | None = None


def evaluate_live_gate(
    config: AppConfig, settings: Settings, confirm_live: bool, evidence: RuntimeEvidence
) -> GateReport:
    checks: dict[str, bool] = {}
    checks["mode_is_live"] = config.mode is RunMode.LIVE
    checks["env_ENABLE_LIVE_TRADING"] = settings.enable_live_trading is True
    checks["config_live_trading_enabled"] = config.live.live_trading_enabled is True
    checks["cli_confirm_live"] = confirm_live is True
    checks["acknowledgement_matches"] = (
        bool(config.live.expected_acknowledgement)
        and settings.live_acknowledgement == config.live.expected_acknowledgement
        and config.live.acknowledgement == config.live.expected_acknowledgement
    )
    checks["exchange_is_production"] = config.exchange.environment == "production"
    checks["api_key_present"] = evidence.api_key_present
    checks["api_permissions_ok"] = evidence.api_permissions_ok
    checks["time_synced"] = (
        evidence.time_synced
        and evidence.clock_offset_ms is not None
        and abs(evidence.clock_offset_ms) <= config.risk.max_clock_skew_ms
    )
    checks["position_consistent"] = evidence.position_consistent
    checks["no_unconfirmed_orders"] = evidence.no_unconfirmed_orders
    checks["hedge_ratio_within_limits"] = evidence.hedge_ratio_within_limits
    checks["market_data_fresh"] = evidence.market_data_fresh
    checks["contract_spec_verified"] = evidence.contract_spec_verified
    checks["position_mode_one_way"] = evidence.position_mode_one_way
    checks["equity_within_small_size_limit"] = (
        evidence.equity_btc is not None
        and evidence.equity_btc <= config.live.max_initial_equity_btc
    )
    failures = [name for name, ok in checks.items() if not ok]
    return GateReport(passed=not failures, failures=failures, checks=checks)
