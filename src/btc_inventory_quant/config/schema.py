"""Typed configuration schema (YAML → Pydantic).

Every tunable lives here so that strategy/risk parameters are changed by editing a config
file, never code. Risk limits (e.g. the 85% hedge cap) are defined ONCE in :class:`RiskLimits`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Ratio = Annotated[Decimal, Field(ge=Decimal("0"), le=Decimal("1"))]
NonNegDecimal = Annotated[Decimal, Field(ge=Decimal("0"))]


class RunMode(StrEnum):
    BACKTEST = "backtest"
    SHADOW = "shadow"
    TESTNET = "testnet"
    LIVE = "live"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
class StrategyName(StrEnum):
    REGIME_HEDGE_V1 = "regime_hedge_v1"
    HODL = "hodl"
    FIXED_HEDGE = "fixed_hedge"
    EMA200_SIMPLE = "ema200_simple"


class RegimeHedgeParams(StrictModel):
    """V1 rule-based parameters (initial reference values, not permanently fixed)."""

    ema_fast: int = Field(20, ge=2)
    ema_mid: int = Field(50, ge=2)
    ema_slow: int = Field(200, ge=2)
    atr_period: int = Field(20, ge=2)
    slope_lookback: int = Field(5, ge=1)
    hedge_bull: Ratio = Decimal("0.10")
    hedge_neutral: Ratio = Decimal("0.45")
    hedge_bear: Ratio = Decimal("0.80")
    overbought_atr_mult: Decimal = Field(Decimal("2"), gt=0)
    oversold_atr_mult: Decimal = Field(Decimal("2"), gt=0)
    overbought_adjustment: Decimal = Decimal("0.10")
    oversold_adjustment: Decimal = Decimal("-0.10")
    regime_confirm_bars: int = Field(2, ge=1)


class FixedHedgeParams(StrictModel):
    ratio: Ratio = Decimal("0.50")
    bypass_max_hedge_cap: bool = False  # benchmarks like "100% fixed short" need this explicitly


class Ema200SimpleParams(StrictModel):
    ema_period: int = Field(200, ge=2)
    below_ratio: Ratio = Decimal("0.80")
    above_ratio: Ratio = Decimal("0.00")


class FundingGuardParams(StrictModel):
    """V2 step 1: shrink the short when the trailing settled funding rate turns adverse.

    A cost rule, not a direction forecast: a short receives funding when the rate is positive, so a
    negative trailing mean means the position is paying to stay on.
    """

    enabled: bool = False
    lookback_periods: int = Field(21, ge=1)  # settlements (3/day) → 21 ≈ 7 days
    min_periods: int = Field(21, ge=1)  # do not act before the window is full
    threshold_rate: Decimal = Decimal("0")  # per-settlement mean below this = adverse
    scale: Ratio = Decimal("0.5")  # multiply the target by this while adverse
    max_ratio_when_adverse: Ratio = Decimal("0.45")  # and cap it here

    @model_validator(mode="after")
    def _check(self) -> FundingGuardParams:
        if self.min_periods > self.lookback_periods:
            raise ValueError("min_periods must be <= lookback_periods")
        return self


class StrategyConfig(StrictModel):
    name: StrategyName = StrategyName.REGIME_HEDGE_V1
    regime_hedge: RegimeHedgeParams = RegimeHedgeParams()
    fixed_hedge: FixedHedgeParams = FixedHedgeParams()
    ema200_simple: Ema200SimpleParams = Ema200SimpleParams()
    funding_guard: FundingGuardParams = FundingGuardParams()
    timeframe: Literal["1d"] = "1d"


# ---------------------------------------------------------------------------
# Risk (single source of truth)
# ---------------------------------------------------------------------------
class RiskLimits(StrictModel):
    max_hedge_ratio: Ratio = Decimal("0.85")
    min_hedge_ratio: Ratio = Decimal("0.00")
    max_daily_hedge_change: Ratio = Decimal("0.20")
    min_rebalance_diff: Ratio = Decimal("0.10")
    allow_long: Literal[False] = False  # futures longs are forbidden by design
    max_effective_leverage: Decimal = Field(Decimal("1.0"), gt=0, le=1)
    max_short_notional_to_equity: Decimal = Field(Decimal("1.0"), gt=0, le=1)
    max_hedge_ratio_tolerance: Ratio = Decimal(
        "0.03"
    )  # tolerance for drift above the cap before SAFE_HOLD
    stale_price_max_age_seconds: int = Field(120, ge=1)
    stale_mark_max_age_seconds: int = Field(120, ge=1)
    max_consecutive_order_rejects: int = Field(3, ge=1)
    max_clock_skew_ms: int = Field(1000, ge=1)
    min_margin_ratio_buffer: Decimal = Field(
        Decimal("0.25"), ge=0
    )  # SAFE_HOLD if maint-margin/equity > buffer
    max_unexpected_balance_diff_btc: Decimal = Field(Decimal("0.00001"), ge=0)
    ws_max_silence_seconds: int = Field(900, ge=30)  # SAFE_HOLD when streams are enabled but silent

    @model_validator(mode="after")
    def _check(self) -> RiskLimits:
        if self.min_hedge_ratio > self.max_hedge_ratio:
            raise ValueError("min_hedge_ratio must be <= max_hedge_ratio")
        if self.min_rebalance_diff > self.max_daily_hedge_change:
            raise ValueError("min_rebalance_diff must be <= max_daily_hedge_change")
        return self


# ---------------------------------------------------------------------------
# Costs / execution model (backtest)
# ---------------------------------------------------------------------------
class CostModelConfig(StrictModel):
    taker_fee_rate: NonNegDecimal = Decimal("0.0005")
    maker_fee_rate: NonNegDecimal = Decimal("0.0002")
    use_maker: bool = False  # conservative default: taker fills
    slippage_bps: NonNegDecimal = Decimal("5")
    fee_multiplier: Decimal = Field(Decimal("1"), ge=0)
    slippage_multiplier: Decimal = Field(Decimal("1"), ge=0)
    funding_adverse_bps_per_period: Decimal = Decimal(
        "0"
    )  # extra funding paid by the short each period (stress)
    signal_delay_days: int = Field(0, ge=0)
    partial_fill_ratio: Decimal = Field(
        Decimal("1"), gt=0, le=1
    )  # fraction of requested qty filled per attempt
    partial_fill_probability: Decimal = Field(Decimal("0"), ge=0, le=1)
    missed_rebalance_every_n: int = Field(
        0, ge=0
    )  # 0 = none; N = every Nth rebalance is missed once
    missed_rebalance_indices: tuple[int, ...] = ()
    reorder_missed_next_day: bool = True


class BenchmarkSpec(StrictModel):
    name: str
    strategy: StrategyConfig


class DataConfig(StrictModel):
    raw_dir: Path = Path("data/raw")
    processed_dir: Path = Path("data/processed")
    exchange_info_path: Path = Path("data/raw/exchange_info.json")
    symbol: str = "BTCUSD_PERP"
    pair: str = "BTCUSD"
    funding_mark_source: Literal["funding_record_or_daily", "daily_only"] = (
        "funding_record_or_daily"
    )
    require_index_validation: bool = True
    max_mark_index_divergence: Decimal = Decimal("0.02")


class SplitConfig(StrictModel):
    research_fraction: Decimal = Field(Decimal("0.6"), gt=0, lt=1)
    validation_fraction: Decimal = Field(Decimal("0.2"), gt=0, lt=1)
    # out-of-sample = remainder
    walk_forward_train_days: int = Field(730, ge=100)
    walk_forward_test_days: int = Field(180, ge=30)
    walk_forward_step_days: int = Field(180, ge=30)


class BacktestConfig(StrictModel):
    start: date | None = None
    end: date | None = None
    initial_btc: Decimal = Field(Decimal("1"), gt=0)
    cost: CostModelConfig = CostModelConfig()
    seed: int = 42
    report_dir: Path = Path("reports")
    run_name: str = "backtest"
    benchmarks: tuple[BenchmarkSpec, ...] = ()
    split: SplitConfig = SplitConfig()
    warmup_bars: int = Field(
        210, ge=1
    )  # ≥ ema_slow + slope lookback; signals before this are ignored
    sensitivity: bool = True
    make_charts: bool = True


# ---------------------------------------------------------------------------
# Exchange / execution / operations
# ---------------------------------------------------------------------------
class ExchangeName(StrEnum):
    BINANCE_COINM = "binance_coinm"
    PAPER = "paper"


class ExchangeConfig(StrictModel):
    name: ExchangeName = ExchangeName.PAPER
    symbol: str = "BTCUSD_PERP"
    pair: str = "BTCUSD"
    environment: Literal["production", "demo"] = "demo"
    recv_window_ms: int = Field(5000, ge=1000, le=60000)
    request_timeout_seconds: float = Field(10.0, gt=0)
    expected_position_mode: Literal["ONE_WAY"] = "ONE_WAY"
    expected_margin_type: Literal["CROSSED", "ISOLATED"] = "CROSSED"
    expected_leverage: int = Field(1, ge=1, le=5)
    paper_initial_btc: Decimal = Field(Decimal("1"), gt=0)  # shadow-mode simulated account size
    use_websocket: bool = True  # user-data + mark-price streams (REST stays authoritative)
    max_retries: int = Field(5, ge=0)
    backoff_base_seconds: float = Field(0.5, gt=0)
    backoff_max_seconds: float = Field(30.0, gt=0)


class ExecutionPolicyName(StrEnum):
    MARKET = "market"
    MAKER_FIRST = "maker_first"
    TWAP = "twap"


class ExecutionConfig(StrictModel):
    policy: ExecutionPolicyName = ExecutionPolicyName.MARKET
    order_timeout_seconds: int = Field(30, ge=1)
    maker_wait_seconds: int = Field(120, ge=1)
    twap_slices: int = Field(4, ge=1)
    twap_interval_seconds: int = Field(300, ge=1)
    max_order_contracts: int = Field(60000, ge=1)  # overridden by MARKET_LOT_SIZE at runtime
    client_order_id_prefix: str = Field("biq", pattern=r"^[A-Za-z0-9_-]{1,8}$")
    decision_time_utc: str = Field(
        "00:05", pattern=r"^\d{2}:\d{2}$"
    )  # after the daily candle closes
    poll_interval_seconds: int = Field(30, ge=5)


class StorageConfig(StrictModel):
    database_url: str | None = None  # falls back to env BIQ_DATABASE_URL
    echo_sql: bool = False


class AlertChannel(StrEnum):
    LOG = "log"
    TELEGRAM = "telegram"
    EMAIL = "email"


class MonitoringConfig(StrictModel):
    alert_channels: tuple[AlertChannel, ...] = (AlertChannel.LOG,)
    metrics_snapshot_interval_seconds: int = Field(60, ge=5)
    heartbeat_interval_seconds: int = Field(300, ge=10)


class LiveConfig(StrictModel):
    live_trading_enabled: bool = False
    acknowledgement: str = ""  # must equal the env acknowledgement AND the expected phrase
    expected_acknowledgement: str = "I_ACCEPT_COIN_M_TRADING_RISK"
    max_initial_equity_btc: Decimal = Field(
        Decimal("0.05"), gt=0
    )  # small-size guard for first live runs
    require_dedicated_subaccount_ack: bool = True


class AppConfig(StrictModel):
    mode: RunMode = RunMode.BACKTEST
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskLimits = RiskLimits()
    data: DataConfig = DataConfig()
    backtest: BacktestConfig = BacktestConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    execution: ExecutionConfig = ExecutionConfig()
    storage: StorageConfig = StorageConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    live: LiveConfig = LiveConfig()

    @model_validator(mode="after")
    def _mode_consistency(self) -> AppConfig:
        if self.mode is RunMode.LIVE and self.exchange.environment != "production":
            raise ValueError("live mode requires exchange.environment: production")
        if self.mode is RunMode.TESTNET and self.exchange.environment != "demo":
            raise ValueError("testnet mode requires exchange.environment: demo")
        if self.mode is not RunMode.LIVE and self.live.live_trading_enabled:
            raise ValueError("live_trading_enabled may only be true in live mode")
        s = self.strategy
        if (
            s.name is StrategyName.FIXED_HEDGE
            and not s.fixed_hedge.bypass_max_hedge_cap
            and s.fixed_hedge.ratio > self.risk.max_hedge_ratio
        ):
            raise ValueError(
                "fixed hedge ratio exceeds risk.max_hedge_ratio (set bypass_max_hedge_cap for benchmarks)"
            )
        return self
