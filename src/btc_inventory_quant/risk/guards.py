"""Runtime guards (prompt §12). Each returns a :class:`GuardResult`; ``critical`` results
must put the system into SAFE_HOLD, non-critical ones only block new orders this cycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from btc_inventory_quant.config.schema import RiskLimits
from btc_inventory_quant.domain.inverse_contract import ZERO, ContractSpec
from btc_inventory_quant.exchange.models import (
    AccountState,
    ExchangeOrder,
    ExchangePosition,
    MarkPriceInfo,
    PositionMode,
)


@dataclass(frozen=True, slots=True)
class GuardResult:
    ok: bool
    code: str
    message: str = ""
    critical: bool = False


class Guards:
    def __init__(
        self,
        limits: RiskLimits,
        spec: ContractSpec,
        symbol: str,
        *,
        expected_margin_type: str = "CROSSED",
        expected_leverage: int = 1,
    ) -> None:
        self.limits = limits
        self.spec = spec
        self.symbol = symbol
        self.expected_margin_type = expected_margin_type.lower().replace("crossed", "cross")
        self.expected_leverage = expected_leverage

    # --- data freshness ----------------------------------------------------
    def check_mark_freshness(self, mark: MarkPriceInfo, now: datetime) -> GuardResult:
        age = (now - mark.time).total_seconds()
        if age > self.limits.stale_mark_max_age_seconds:
            return GuardResult(False, "STALE_MARK", f"mark price is {age:.0f}s old", critical=True)
        if mark.mark_price <= ZERO:
            return GuardResult(False, "BAD_MARK", "mark price not positive", critical=True)
        return GuardResult(True, "MARK_OK")

    def check_candle_freshness(
        self, last_close_time: datetime, now: datetime, max_age_seconds: int
    ) -> GuardResult:
        age = (now - last_close_time).total_seconds()
        if age > max_age_seconds:
            return GuardResult(
                False,
                "STALE_CANDLES",
                f"last completed candle closed {age / 3600:.1f}h ago",
                critical=True,
            )
        return GuardResult(True, "CANDLES_OK")

    def check_clock(self, offset_ms: int) -> GuardResult:
        if abs(offset_ms) > self.limits.max_clock_skew_ms:
            return GuardResult(False, "CLOCK_SKEW", f"server offset {offset_ms} ms", critical=True)
        return GuardResult(True, "CLOCK_OK")

    # --- account state -----------------------------------------------------
    def check_position_mode(self, mode: PositionMode) -> GuardResult:
        if mode is not PositionMode.ONE_WAY:
            return GuardResult(
                False, "POSITION_MODE", f"expected ONE_WAY, got {mode.value}", critical=True
            )
        return GuardResult(True, "POSITION_MODE_OK")

    def check_position(self, position: ExchangePosition | None) -> list[GuardResult]:
        out: list[GuardResult] = []
        if position is None:
            return [GuardResult(True, "POSITION_OK")]
        if position.contracts > ZERO:
            out.append(
                GuardResult(False, "LONG_POSITION", "exchange position is LONG", critical=True)
            )
        if position.margin_type and position.margin_type.lower() != self.expected_margin_type:
            out.append(
                GuardResult(
                    False,
                    "MARGIN_TYPE",
                    f"expected {self.expected_margin_type}, got {position.margin_type}",
                    critical=False,
                )
            )
        if (
            position.leverage is not None
            and position.leverage > max(self.expected_leverage, 1)
            and position.contracts != ZERO
        ):
            out.append(
                GuardResult(
                    False,
                    "LEVERAGE_SETTING",
                    f"leverage setting {position.leverage} > expected {self.expected_leverage}",
                    critical=False,
                )
            )
        return out or [GuardResult(True, "POSITION_OK")]

    def check_hedge_cap(
        self, account: AccountState, position: ExchangePosition | None, mark_price: Decimal
    ) -> GuardResult:
        equity = account.wallet_balance + account.unrealized_pnl
        if equity <= ZERO:
            return GuardResult(False, "NON_POSITIVE_EQUITY", f"equity {equity}", critical=True)
        short = position.short_contracts if position else ZERO
        ratio = short * self.spec.contract_value_usd / (equity * mark_price)
        if ratio > self.limits.max_hedge_ratio + self.limits.max_hedge_ratio_tolerance:
            return GuardResult(
                False, "HEDGE_CAP_EXCEEDED", f"effective hedge ratio {ratio:.4f}", critical=True
            )
        if (
            short * self.spec.contract_value_usd
            > equity
            * mark_price
            * self.limits.max_short_notional_to_equity
            * (1 + self.limits.max_hedge_ratio_tolerance)
        ):
            return GuardResult(
                False, "LEVERAGE_EXCEEDED", "short notional above equity", critical=True
            )
        return GuardResult(True, "HEDGE_CAP_OK")

    def check_margin_buffer(self, account: AccountState) -> GuardResult:
        equity = account.wallet_balance + account.unrealized_pnl
        if equity <= ZERO:
            return GuardResult(False, "NON_POSITIVE_EQUITY", "", critical=True)
        if (
            account.maint_margin > ZERO
            and account.maint_margin / equity > self.limits.min_margin_ratio_buffer
        ):
            return GuardResult(
                False,
                "MARGIN_RATIO",
                f"maintenance margin / equity = {account.maint_margin / equity:.3f}",
                critical=True,
            )
        return GuardResult(True, "MARGIN_OK")

    def check_unknown_orders(
        self, open_orders: list[ExchangeOrder], known_ids: set[str]
    ) -> GuardResult:
        unknown = [o.client_order_id for o in open_orders if o.client_order_id not in known_ids]
        if unknown:
            return GuardResult(
                False, "UNKNOWN_ORDERS", f"unknown open orders: {unknown}", critical=True
            )
        return GuardResult(True, "ORDERS_OK")

    def check_rejects(self, consecutive_rejects: int) -> GuardResult:
        if consecutive_rejects >= self.limits.max_consecutive_order_rejects:
            return GuardResult(
                False,
                "REPEATED_REJECTS",
                f"{consecutive_rejects} consecutive rejects",
                critical=True,
            )
        return GuardResult(True, "REJECTS_OK")

    def check_unexpected_transfers(self, unexpected_amount_btc: Decimal) -> GuardResult:
        if abs(unexpected_amount_btc) > self.limits.max_unexpected_balance_diff_btc:
            return GuardResult(
                False,
                "UNEXPECTED_TRANSFER",
                f"unexpected deposit/withdrawal {unexpected_amount_btc} BTC",
                critical=True,
            )
        return GuardResult(True, "TRANSFERS_OK")

    def check_contract_spec(self, live_spec: ContractSpec) -> GuardResult:
        if (live_spec.contract_value_usd, live_spec.qty_step, live_spec.price_tick) != (
            self.spec.contract_value_usd,
            self.spec.qty_step,
            self.spec.price_tick,
        ):
            return GuardResult(
                False, "CONTRACT_SPEC_CHANGED", f"{self.spec} -> {live_spec}", critical=True
            )
        return GuardResult(True, "SPEC_OK")

    def check_stream_silence(self, silence_seconds: float | None, connected: bool) -> GuardResult:
        """WebSocket streams enabled but silent/disconnected for too long → SAFE_HOLD."""
        if silence_seconds is None:
            return GuardResult(True, "WS_NOT_ENABLED")
        if silence_seconds > self.limits.ws_max_silence_seconds:
            return GuardResult(
                False,
                "WS_SILENT",
                f"no stream message for {silence_seconds:.0f}s (connected={connected})",
                critical=True,
            )
        return GuardResult(True, "WS_OK")

    def check_ws_position(
        self, ws_position_amt: Decimal | None, rest_position: ExchangePosition | None
    ) -> GuardResult:
        """REST positionRisk vs the latest ACCOUNT_UPDATE position must agree."""
        if ws_position_amt is None:
            return GuardResult(True, "WS_POSITION_NOT_AVAILABLE")
        rest_amt = rest_position.contracts if rest_position else ZERO
        if ws_position_amt != rest_amt:
            return GuardResult(
                False,
                "WS_REST_POSITION_MISMATCH",
                f"ws={ws_position_amt} rest={rest_amt}",
                critical=True,
            )
        return GuardResult(True, "WS_POSITION_OK")


def failures(results: list[GuardResult]) -> list[GuardResult]:
    return [r for r in results if not r.ok]


def critical_failures(results: list[GuardResult]) -> list[GuardResult]:
    return [r for r in results if not r.ok and r.critical]
