from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from btc_inventory_quant.config.loader import config_sha256, load_config
from btc_inventory_quant.config.schema import AppConfig, RiskLimits, RunMode, StrategyName
from btc_inventory_quant.domain.inverse_contract import OrderSide
from btc_inventory_quant.risk.limits import (
    RiskLimitViolation,
    apply_daily_change_cap,
    clamp_hedge_ratio,
    rebalance_needed,
    resolve_hedge_target,
    validate_order_against_position,
)

D = Decimal


def test_hedge_ratio_clamped_to_85_percent(limits: RiskLimits) -> None:
    assert clamp_hedge_ratio(D("0.95"), limits) == D("0.85")
    assert clamp_hedge_ratio(D("-0.1"), limits) == D("0")
    assert clamp_hedge_ratio(D("0.5"), limits) == D("0.5")
    assert clamp_hedge_ratio(D("1.5"), limits, bypass_cap=True) == D("1")


def test_daily_change_capped_at_20_points(limits: RiskLimits) -> None:
    assert apply_daily_change_cap(D("0.10"), D("0.80"), limits) == D("0.30")
    assert apply_daily_change_cap(D("0.80"), D("0.10"), limits) == D("0.60")
    assert apply_daily_change_cap(D("0.40"), D("0.55"), limits) == D("0.55")


def test_no_trade_below_10_point_difference(limits: RiskLimits) -> None:
    assert not rebalance_needed(D("0.45"), D("0.54"), limits)
    assert rebalance_needed(D("0.45"), D("0.55"), limits)
    assert rebalance_needed(D("0.45"), D("0.35"), limits)


def test_resolve_hedge_target_pipeline(limits: RiskLimits) -> None:
    d = resolve_hedge_target(D("0.10"), D("0.95"), limits)
    assert d.clamped_target_ratio == D("0.85")
    assert d.capped_target_ratio == D("0.30")
    assert d.rebalance
    assert d.executable_target == D("0.30")
    d2 = resolve_hedge_target(D("0.45"), D("0.50"), limits)
    assert not d2.rebalance
    assert d2.executable_target == D("0.45")


def test_sell_rejected_when_notional_exceeds_equity(spec, limits: RiskLimits) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RiskLimitViolation) as exc:
        validate_order_against_position(
            OrderSide.SELL, D("1001"), D("0"), D("1"), D("100000"), spec, limits
        )
    assert exc.value.code == "NOTIONAL_EXCEEDS_EQUITY"
    with pytest.raises(RiskLimitViolation) as exc2:
        validate_order_against_position(
            OrderSide.SELL, D("900"), D("0"), D("1"), D("100000"), spec, limits
        )
    assert exc2.value.code == "HEDGE_CAP"
    validate_order_against_position(
        OrderSide.SELL, D("850"), D("0"), D("1"), D("100000"), spec, limits
    )
    validate_order_against_position(
        OrderSide.SELL, D("1000"), D("0"), D("1"), D("100000"), spec, limits, bypass_cap=True
    )
    validate_order_against_position(
        OrderSide.BUY, D("100"), D("100"), D("1"), D("100000"), spec, limits
    )


def test_risk_limits_single_source_of_truth() -> None:
    cfg = AppConfig()
    assert cfg.risk.max_hedge_ratio == D("0.85")
    assert cfg.risk.max_daily_hedge_change == D("0.20")
    assert cfg.risk.min_rebalance_diff == D("0.10")
    assert cfg.risk.allow_long is False
    with pytest.raises(ValidationError):
        RiskLimits(allow_long=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RiskLimits(max_hedge_ratio=D("1.2"))
    with pytest.raises(ValidationError):
        RiskLimits(min_rebalance_diff=D("0.3"), max_daily_hedge_change=D("0.2"))


def test_config_load_and_hash(tmp_path: Path) -> None:
    payload = {
        "mode": "backtest",
        "strategy": {"name": "regime_hedge_v1", "regime_hedge": {"hedge_bear": "0.75"}},
        "risk": {"max_hedge_ratio": "0.80"},
    }
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(payload))
    cfg = load_config(p)
    assert cfg.mode is RunMode.BACKTEST
    assert cfg.strategy.name is StrategyName.REGIME_HEDGE_V1
    assert cfg.strategy.regime_hedge.hedge_bear == D("0.75")
    assert cfg.risk.max_hedge_ratio == D("0.80")
    assert config_sha256(cfg) == config_sha256(load_config(p))
    assert config_sha256(cfg) != config_sha256(AppConfig())
    with pytest.raises(ValidationError):
        load_config(p, overrides={"strategy": {"unknown_key": 1}})


def test_live_flags_only_in_live_mode() -> None:
    with pytest.raises(ValidationError):
        AppConfig(mode=RunMode.BACKTEST, live={"live_trading_enabled": True})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AppConfig(mode=RunMode.LIVE, exchange={"environment": "demo"})  # type: ignore[arg-type]
    cfg = AppConfig(
        mode=RunMode.LIVE, exchange={"environment": "production", "name": "binance_coinm"}
    )  # type: ignore[arg-type]
    assert cfg.live.live_trading_enabled is False  # default remains off


def test_fixed_100pct_benchmark_requires_explicit_bypass() -> None:
    with pytest.raises(ValidationError):
        AppConfig(strategy={"name": "fixed_hedge", "fixed_hedge": {"ratio": "1.0"}})  # type: ignore[arg-type]
    cfg = AppConfig(
        strategy={
            "name": "fixed_hedge",
            "fixed_hedge": {"ratio": "1.0", "bypass_max_hedge_cap": True},
        }
    )  # type: ignore[arg-type]
    assert cfg.strategy.fixed_hedge.ratio == D("1")
