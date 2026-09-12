"""Internal domain objects. External API shapes are normalized into these (PRD 9)."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from asm.domain.enums import (
    Action,
    Chain,
    Decision,
    ExitReason,
    OrderStatus,
    PositionStatus,
    Reason,
    TraderStatus,
)
from asm.domain.money import ZERO


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True, use_enum_values=False, validate_assignment=False
    )


# --------------------------------------------------------------------------
# Trader intelligence (PRD 12-15)
# --------------------------------------------------------------------------
class TraderScores(Base):
    """PRD 13 - component scores, 0..100."""

    performance: Decimal = ZERO
    consistency: Decimal = ZERO
    longevity: Decimal = ZERO
    risk_adjusted: Decimal = ZERO
    copyability: Decimal = ZERO
    live_replication: Decimal | None = None
    composite: Decimal = ZERO
    confidence: Decimal = ZERO
    sample_size: int = 0
    computed_at: datetime = Field(default_factory=utcnow)


class TraderProfile(Base):
    """PRD 12 - everything we know about a followed wallet."""

    wallet: str
    chain: Chain = Chain.SOLANA
    status: TraderStatus = TraderStatus.DISCOVERED
    source: str = "unknown"
    scores: TraderScores = Field(default_factory=TraderScores)

    # behavioural profile, learned (PRD 12, 17.2)
    median_hold_seconds: int | None = None
    latency_tolerance_ms: int | None = None
    avg_trade_size_usd: Decimal | None = None
    win_rate: Decimal | None = None
    trade_count: int = 0

    risk_budget_pct: Decimal = ZERO
    size_multiplier: Decimal = Decimal(1)
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_trade_at: datetime | None = None
    notes: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Detection (PRD 16)
# --------------------------------------------------------------------------
class SourceTrade(Base):
    """A parsed swap by a followed wallet."""

    id: UUID = Field(default_factory=uuid4)
    chain: Chain = Chain.SOLANA
    wallet: str
    signature: str
    slot: int = 0
    action: Action
    token_mint: str
    quote_mint: str
    token_amount_raw: int = 0
    quote_amount_raw: int = 0
    token_decimals: int = 9
    quote_decimals: int = 9
    source_price: Decimal = ZERO
    source_value_usd: Decimal = ZERO
    source_confirmed_at: datetime
    detected_at: datetime = Field(default_factory=utcnow)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        """PRD 51 - unique internal identity of a source transaction."""
        blob = f"{self.chain}|{self.wallet}|{self.signature}|{self.token_mint}|{self.action}"
        return hashlib.sha256(blob.encode()).hexdigest()[:40]

    @property
    def observation_latency_ms(self) -> int:
        return int((self.detected_at - self.source_confirmed_at).total_seconds() * 1000)


class MarketState(Base):
    """Snapshot of the token at decision time (PRD 16.8)."""

    mint: str
    price: Decimal = ZERO
    price_usd: Decimal = ZERO
    liquidity_usd: Decimal = ZERO
    market_cap_usd: Decimal = ZERO
    volume_24h_usd: Decimal = ZERO
    holder_count: int | None = None
    token_age_seconds: int | None = None
    decimals: int = 9
    symbol: str | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
    stale: bool = False


class TokenRisk(Base):
    """PRD 18-19. Higher score = more dangerous."""

    mint: str
    score: int = 100
    mint_authority_active: bool | None = None
    freeze_authority_active: bool | None = None
    lp_burned: bool | None = None
    top10_holder_pct: Decimal | None = None
    creator_holds_pct: Decimal | None = None
    is_honeypot: bool | None = None
    insider_flags: list[str] = Field(default_factory=list)
    reasons: list[Reason] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=utcnow)
    unknown: bool = False


class Quote(Base):
    """Normalized router quote (Jupiter). Doubles as the liquidity/slippage oracle."""

    input_mint: str
    output_mint: str
    in_amount_raw: int
    out_amount_raw: int
    other_amount_threshold_raw: int = 0
    price_impact_pct: Decimal = ZERO
    slippage_bps: int = 0
    route_label: str = ""
    route_plan: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=utcnow)

    @property
    def effective_price(self) -> Decimal:
        if not self.out_amount_raw:
            return ZERO
        return Decimal(self.in_amount_raw) / Decimal(self.out_amount_raw)


class TradeCandidate(Base):
    """PRD 17 - a source trade promoted into something we may act on."""

    id: UUID = Field(default_factory=uuid4)
    source_trade: SourceTrade
    trader: TraderProfile
    market: MarketState
    token_risk: TokenRisk | None = None
    quote: Quote | None = None
    confirmations: list[str] = Field(default_factory=list)  # PRD 20
    created_at: datetime = Field(default_factory=utcnow)


class LatencyBreakdown(Base):
    """PRD 17.2 - every stage timestamped."""

    source_confirmed_at: datetime | None = None
    detected_at: datetime | None = None
    decision_completed_at: datetime | None = None
    transaction_built_at: datetime | None = None
    signed_at: datetime | None = None
    submitted_at: datetime | None = None
    confirmed_at: datetime | None = None

    def _ms(self, a: datetime | None, b: datetime | None) -> int | None:
        if not a or not b:
            return None
        return int((b - a).total_seconds() * 1000)

    @property
    def observation_ms(self) -> int | None:
        return self._ms(self.source_confirmed_at, self.detected_at)

    @property
    def analysis_ms(self) -> int | None:
        return self._ms(self.detected_at, self.decision_completed_at)

    @property
    def execution_ms(self) -> int | None:
        return self._ms(self.decision_completed_at, self.submitted_at)

    @property
    def total_copy_ms(self) -> int | None:
        return self._ms(self.source_confirmed_at, self.confirmed_at or self.submitted_at)


class GateResult(Base):
    """One gate's verdict. size_mult < 1 reduces rather than rejects (PRD 17.1)."""

    name: str
    passed: bool
    size_mult: Decimal = Decimal(1)
    reasons: list[Reason] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def ok(cls, name: str, *reasons: Reason, mult: Decimal = Decimal(1), **detail):
        return cls(name=name, passed=True, size_mult=mult, reasons=list(reasons), detail=detail)

    @classmethod
    def fail(cls, name: str, *reasons: Reason, **detail):
        return cls(name=name, passed=False, size_mult=Decimal(0), reasons=list(reasons), detail=detail)


class DecisionResult(Base):
    """PRD 41 - deterministic, auditable, reproducible."""

    id: UUID = Field(default_factory=uuid4)
    candidate_id: UUID
    decision: Decision
    reasons: list[Reason] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    size_usd: Decimal = ZERO
    size_pct: Decimal = ZERO
    size_multiplier: Decimal = Decimal(1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    reservation_id: str | None = None
    latency: LatencyBreakdown = Field(default_factory=LatencyBreakdown)
    strategy_version: str = "v1"
    decided_at: datetime = Field(default_factory=utcnow)

    @property
    def approved(self) -> bool:
        return self.decision is Decision.COPY


class ApprovedOrder(Base):
    """Handed to an Executor. Identical shape in all four modes (PRD 8)."""

    id: UUID = Field(default_factory=uuid4)
    idempotency_key: str
    decision_id: UUID
    candidate: TradeCandidate
    action: Action
    input_mint: str
    output_mint: str
    in_amount_raw: int
    size_usd: Decimal
    slippage_bps: int
    position_id: UUID | None = None
    exit_reason: ExitReason | None = None
    reservation_id: str | None = None
    latency: LatencyBreakdown = Field(default_factory=LatencyBreakdown)
    created_at: datetime = Field(default_factory=utcnow)


class Execution(Base):
    """PRD 26 - execution record with full quality attribution."""

    id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    status: OrderStatus = OrderStatus.CREATED
    mode: str = "paper"
    signature: str | None = None
    expected_price: Decimal = ZERO
    actual_price: Decimal = ZERO
    source_price: Decimal = ZERO
    expected_slippage_pct: Decimal = ZERO
    actual_slippage_pct: Decimal = ZERO
    in_amount_raw: int = 0
    out_amount_raw: int = 0
    value_usd: Decimal = ZERO
    network_fee_lamports: int = 0
    priority_fee_lamports: int = 0
    route: str = ""
    failure_reason: str | None = None
    latency: LatencyBreakdown = Field(default_factory=LatencyBreakdown)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status is OrderStatus.CONFIRMED

    @property
    def total_fee_lamports(self) -> int:
        return self.network_fee_lamports + self.priority_fee_lamports


class Position(Base):
    """Projection over position_events."""

    id: UUID = Field(default_factory=uuid4)
    chain: Chain = Chain.SOLANA
    token_mint: str
    symbol: str | None = None
    trader_wallet: str
    status: PositionStatus = PositionStatus.PENDING
    decimals: int = 9

    amount_raw: int = 0
    cost_basis_usd: Decimal = ZERO
    realized_pnl_usd: Decimal = ZERO
    fees_usd: Decimal = ZERO

    entry_price: Decimal = ZERO
    current_price: Decimal = ZERO
    peak_price: Decimal = ZERO
    trailing_armed: bool = False
    tp_levels_hit: list[int] = Field(default_factory=list)

    opened_at: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None
    source_trade_id: UUID | None = None
    mode: str = "paper"

    @property
    def market_value_usd(self) -> Decimal:
        if not self.amount_raw:
            return ZERO
        return (Decimal(self.amount_raw) / (Decimal(10) ** self.decimals)) * self.current_price

    @property
    def unrealized_pnl_usd(self) -> Decimal:
        if self.status is PositionStatus.CLOSED or not self.amount_raw:
            return ZERO
        return self.market_value_usd - self.cost_basis_usd

    @property
    def total_pnl_usd(self) -> Decimal:
        return self.realized_pnl_usd + self.unrealized_pnl_usd - self.fees_usd

    @property
    def pnl_pct(self) -> Decimal:
        if not self.entry_price:
            return ZERO
        return (self.current_price - self.entry_price) / self.entry_price * Decimal(100)

    @property
    def age_seconds(self) -> int:
        end = self.closed_at or utcnow()
        return int((end - self.opened_at).total_seconds())


class PortfolioState(Base):
    """PRD 22 - live portfolio snapshot."""

    active_capital_usd: Decimal = ZERO
    cash_usd: Decimal = ZERO
    deployed_usd: Decimal = ZERO
    unrealized_pnl_usd: Decimal = ZERO
    realized_pnl_today_usd: Decimal = ZERO
    harvested_total_usd: Decimal = ZERO
    high_water_mark_usd: Decimal = ZERO
    open_positions: int = 0
    sol_balance: Decimal = ZERO
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def total_equity_usd(self) -> Decimal:
        return self.cash_usd + self.deployed_usd + self.unrealized_pnl_usd

    @property
    def drawdown_pct(self) -> Decimal:
        if self.high_water_mark_usd <= 0:
            return ZERO
        return (self.high_water_mark_usd - self.total_equity_usd) / self.high_water_mark_usd * Decimal(100)

    @property
    def deployed_pct(self) -> Decimal:
        eq = self.total_equity_usd
        return ZERO if eq <= 0 else self.deployed_usd / eq * Decimal(100)
