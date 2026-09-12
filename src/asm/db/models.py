"""PRD 47/48 schema.

Conventions:
  - token amounts: NUMERIC(78,0) raw integer units, never float
  - prices/usd:    NUMERIC(38,18)
  - positions are a projection over the append-only position_events table
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from asm.db.base import Base, Timestamps, UUIDPk

AMT = Numeric(78, 0)
PRICE = Numeric(38, 18)
USD = Numeric(38, 12)
PCT = Numeric(12, 6)
SCORE = Numeric(6, 2)


def _ts(**kw) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


# ---------------------------------------------------------------- traders
class Trader(Base, UUIDPk, Timestamps):
    __tablename__ = "traders"

    chain: Mapped[str] = mapped_column(String(16), default="solana")
    wallet_address: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="unknown")
    status: Mapped[str] = mapped_column(String(16), default="discovered", index=True)
    label: Mapped[str | None] = mapped_column(String(128))

    composite_score: Mapped[Decimal] = mapped_column(SCORE, default=0)
    confidence: Mapped[Decimal] = mapped_column(SCORE, default=0)
    copyability_score: Mapped[Decimal] = mapped_column(SCORE, default=0)
    live_replication_score: Mapped[Decimal | None] = mapped_column(SCORE)

    risk_budget_pct: Mapped[Decimal] = mapped_column(PCT, default=0)
    size_multiplier: Mapped[Decimal] = mapped_column(PCT, default=1)

    median_hold_seconds: Mapped[int | None] = mapped_column(Integer)
    latency_tolerance_ms: Mapped[int | None] = mapped_column(Integer)
    avg_trade_size_usd: Mapped[Decimal | None] = mapped_column(USD)
    win_rate: Mapped[Decimal | None] = mapped_column(PCT)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)

    first_seen_at: Mapped[datetime | None] = _ts()
    last_trade_at: Mapped[datetime | None] = _ts(index=True)
    promoted_at: Mapped[datetime | None] = _ts()
    degraded_at: Mapped[datetime | None] = _ts()
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("chain", "wallet_address", name="uq_traders_chain_wallet"),
        Index("ix_traders_status_score", "status", "composite_score"),
    )


class TraderScore(Base, UUIDPk):
    __tablename__ = "trader_scores"

    trader_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("traders.id", ondelete="CASCADE"), index=True
    )
    performance: Mapped[Decimal] = mapped_column(SCORE, default=0)
    consistency: Mapped[Decimal] = mapped_column(SCORE, default=0)
    longevity: Mapped[Decimal] = mapped_column(SCORE, default=0)
    risk_adjusted: Mapped[Decimal] = mapped_column(SCORE, default=0)
    copyability: Mapped[Decimal] = mapped_column(SCORE, default=0)
    live_replication: Mapped[Decimal | None] = mapped_column(SCORE)
    composite: Mapped[Decimal] = mapped_column(SCORE, default=0)
    confidence: Mapped[Decimal] = mapped_column(SCORE, default=0)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    strategy_version: Mapped[str] = mapped_column(String(32), default="v1")
    inputs: Mapped[dict] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = _ts(index=True)


class TraderDailyMetric(Base, UUIDPk):
    __tablename__ = "trader_daily_metrics"

    trader_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("traders.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[datetime] = _ts(index=True)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    source_pnl_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    copied_pnl_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    copyable_pnl_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(PCT, default=0)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (UniqueConstraint("trader_id", "day", name="uq_trader_daily"),)


class TraderRelationship(Base, UUIDPk, Timestamps):
    """PRD 39 - wallet graph."""

    __tablename__ = "trader_relationships"

    wallet_a: Mapped[str] = mapped_column(String(64), index=True)
    wallet_b: Mapped[str] = mapped_column(String(64), index=True)
    relation: Mapped[str] = mapped_column(String(32))
    strength: Mapped[Decimal] = mapped_column(PCT, default=0)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (UniqueConstraint("wallet_a", "wallet_b", "relation", name="uq_rel"),)


# ---------------------------------------------------------------- tokens
class Token(Base, UUIDPk, Timestamps):
    __tablename__ = "tokens"

    chain: Mapped[str] = mapped_column(String(16), default="solana")
    mint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(128))
    decimals: Mapped[int] = mapped_column(Integer, default=9)
    created_at_chain: Mapped[datetime | None] = _ts()
    deployer: Mapped[str | None] = mapped_column(String(64), index=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)


class TokenSnapshot(Base, UUIDPk):
    __tablename__ = "token_snapshots"

    mint: Mapped[str] = mapped_column(String(64), index=True)
    price_usd: Mapped[Decimal] = mapped_column(PRICE, default=0)
    liquidity_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    market_cap_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    volume_24h_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    holder_count: Mapped[int | None] = mapped_column(Integer)
    captured_at: Mapped[datetime] = _ts(index=True)


class TokenRiskScore(Base, UUIDPk):
    __tablename__ = "token_risk_scores"

    mint: Mapped[str] = mapped_column(String(64), index=True)
    score: Mapped[int] = mapped_column(Integer, default=100)
    mint_authority_active: Mapped[bool | None] = mapped_column(Boolean)
    freeze_authority_active: Mapped[bool | None] = mapped_column(Boolean)
    lp_burned: Mapped[bool | None] = mapped_column(Boolean)
    top10_holder_pct: Mapped[Decimal | None] = mapped_column(PCT)
    is_honeypot: Mapped[bool | None] = mapped_column(Boolean)
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    evaluated_at: Mapped[datetime] = _ts(index=True)


# ---------------------------------------------------------------- pipeline
class SourceTradeRow(Base, UUIDPk):
    __tablename__ = "source_trades"

    chain: Mapped[str] = mapped_column(String(16), default="solana")
    wallet: Mapped[str] = mapped_column(String(64), index=True)
    signature: Mapped[str] = mapped_column(String(128), index=True)
    slot: Mapped[int] = mapped_column(BigInteger, default=0)
    action: Mapped[str] = mapped_column(String(16))
    token_mint: Mapped[str] = mapped_column(String(64), index=True)
    quote_mint: Mapped[str] = mapped_column(String(64))
    token_amount_raw: Mapped[Decimal] = mapped_column(AMT, default=0)
    quote_amount_raw: Mapped[Decimal] = mapped_column(AMT, default=0)
    source_price: Mapped[Decimal] = mapped_column(PRICE, default=0)
    source_value_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_confirmed_at: Mapped[datetime] = _ts(index=True)
    detected_at: Mapped[datetime] = _ts()
    observation_latency_ms: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)


class TradeCandidateRow(Base, UUIDPk):
    __tablename__ = "trade_candidates"

    source_trade_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("source_trades.id", ondelete="CASCADE"), index=True
    )
    trader_wallet: Mapped[str] = mapped_column(String(64), index=True)
    token_mint: Mapped[str] = mapped_column(String(64), index=True)
    market_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    quote_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    confirmations: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    created_at: Mapped[datetime] = _ts(index=True)


class TradeDecision(Base, UUIDPk):
    """PRD 5.5 / 41 - full inputs retained so a decision can be re-run later."""

    __tablename__ = "trade_decisions"

    candidate_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    source_trade_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), index=True)
    trader_wallet: Mapped[str] = mapped_column(String(64), index=True)
    token_mint: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(16), index=True)
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    gates: Mapped[dict] = mapped_column(JSONB, default=dict)
    inputs: Mapped[dict] = mapped_column(JSONB, default=dict)
    size_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    size_pct: Mapped[Decimal] = mapped_column(PCT, default=0)
    size_multiplier: Mapped[Decimal] = mapped_column(PCT, default=1)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32), default="v1")

    observation_latency_ms: Mapped[int | None] = mapped_column(Integer)
    analysis_latency_ms: Mapped[int | None] = mapped_column(Integer)
    execution_latency_ms: Mapped[int | None] = mapped_column(Integer)
    total_copy_latency_ms: Mapped[int | None] = mapped_column(Integer)

    decided_at: Mapped[datetime] = _ts(index=True)

    __table_args__ = (Index("ix_decisions_mode_time", "mode", "decided_at"),)


class Order(Base, UUIDPk, Timestamps):
    __tablename__ = "orders"

    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), index=True)
    position_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), index=True)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), default="created", index=True)
    action: Mapped[str] = mapped_column(String(16))
    trader_wallet: Mapped[str] = mapped_column(String(64), index=True)
    token_mint: Mapped[str] = mapped_column(String(64), index=True)
    input_mint: Mapped[str] = mapped_column(String(64))
    output_mint: Mapped[str] = mapped_column(String(64))
    in_amount_raw: Mapped[Decimal] = mapped_column(AMT, default=0)
    size_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    slippage_bps: Mapped[int] = mapped_column(Integer, default=0)
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    reservation_id: Mapped[str | None] = mapped_column(String(64))


class ExecutionRow(Base, UUIDPk, Timestamps):
    __tablename__ = "executions"

    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    signature: Mapped[str | None] = mapped_column(String(128), index=True)

    expected_price: Mapped[Decimal] = mapped_column(PRICE, default=0)
    actual_price: Mapped[Decimal] = mapped_column(PRICE, default=0)
    source_price: Mapped[Decimal] = mapped_column(PRICE, default=0)
    expected_slippage_pct: Mapped[Decimal] = mapped_column(PCT, default=0)
    actual_slippage_pct: Mapped[Decimal] = mapped_column(PCT, default=0)
    in_amount_raw: Mapped[Decimal] = mapped_column(AMT, default=0)
    out_amount_raw: Mapped[Decimal] = mapped_column(AMT, default=0)
    value_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    network_fee_lamports: Mapped[int] = mapped_column(BigInteger, default=0)
    priority_fee_lamports: Mapped[int] = mapped_column(BigInteger, default=0)
    route: Mapped[str] = mapped_column(String(256), default="")
    failure_reason: Mapped[str | None] = mapped_column(Text)

    # PRD 17.2 - every stage timestamped
    source_confirmed_at: Mapped[datetime | None] = _ts()
    detected_at: Mapped[datetime | None] = _ts()
    decision_completed_at: Mapped[datetime | None] = _ts()
    transaction_built_at: Mapped[datetime | None] = _ts()
    signed_at: Mapped[datetime | None] = _ts()
    submitted_at: Mapped[datetime | None] = _ts()
    confirmed_at: Mapped[datetime | None] = _ts()
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)


class PositionRow(Base, UUIDPk, Timestamps):
    """Materialized projection. position_events is the source of truth."""

    __tablename__ = "positions"

    chain: Mapped[str] = mapped_column(String(16), default="solana")
    token_mint: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(64))
    decimals: Mapped[int] = mapped_column(Integer, default=9)
    trader_wallet: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)

    amount_raw: Mapped[Decimal] = mapped_column(AMT, default=0)
    cost_basis_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    realized_pnl_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    fees_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    entry_price: Mapped[Decimal] = mapped_column(PRICE, default=0)
    current_price: Mapped[Decimal] = mapped_column(PRICE, default=0)
    peak_price: Mapped[Decimal] = mapped_column(PRICE, default=0)
    trailing_armed: Mapped[bool] = mapped_column(Boolean, default=False)
    tp_levels_hit: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list)

    source_trade_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    opened_at: Mapped[datetime] = _ts(index=True)
    closed_at: Mapped[datetime | None] = _ts()

    __table_args__ = (Index("ix_positions_open", "mode", "status", "token_mint"),)


class PositionEvent(Base, UUIDPk):
    """Append-only. Never update. PRD 34/58."""

    __tablename__ = "position_events"

    position_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("positions.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    amount_raw_delta: Mapped[Decimal] = mapped_column(AMT, default=0)
    usd_delta: Mapped[Decimal] = mapped_column(USD, default=0)
    price: Mapped[Decimal] = mapped_column(PRICE, default=0)
    realized_pnl_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    fees_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = _ts(index=True)

    __table_args__ = (UniqueConstraint("position_id", "seq", name="uq_position_event_seq"),)


# ---------------------------------------------------------------- portfolio
class PortfolioSnapshot(Base, UUIDPk):
    __tablename__ = "portfolio_snapshots"

    mode: Mapped[str] = mapped_column(String(16), index=True)
    active_capital_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    cash_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    deployed_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    unrealized_pnl_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    realized_pnl_today_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    total_equity_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    high_water_mark_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    drawdown_pct: Mapped[Decimal] = mapped_column(PCT, default=0)
    harvested_total_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    sol_balance: Mapped[Decimal] = mapped_column(Numeric(38, 9), default=0)
    captured_at: Mapped[datetime] = _ts(index=True)


class RiskEvent(Base, UUIDPk):
    __tablename__ = "risk_events"

    kind: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = _ts(index=True)


class ProfitHarvestRule(Base, UUIDPk, Timestamps):
    __tablename__ = "profit_harvest_rules"

    name: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    threshold_pct: Mapped[Decimal] = mapped_column(PCT, default=25)
    take_pct: Mapped[Decimal] = mapped_column(PCT, default=40)
    min_active_capital_usd: Mapped[Decimal] = mapped_column(USD, default=500)
    destination: Mapped[str] = mapped_column(String(64), default="")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)


class ProfitHarvestEvent(Base, UUIDPk):
    __tablename__ = "profit_harvest_events"

    rule_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    mode: Mapped[str] = mapped_column(String(16), index=True)
    equity_before_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    equity_after_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    profit_since_hwm_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    harvested_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    retained_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    new_high_water_mark_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    destination: Mapped[str] = mapped_column(String(64), default="")
    signature: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = _ts(index=True)


class TreasuryTransfer(Base, UUIDPk):
    __tablename__ = "treasury_transfers"

    harvest_event_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    direction: Mapped[str] = mapped_column(String(16))
    amount_usd: Mapped[Decimal] = mapped_column(USD, default=0)
    amount_raw: Mapped[Decimal] = mapped_column(AMT, default=0)
    mint: Mapped[str] = mapped_column(String(64))
    destination: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    occurred_at: Mapped[datetime] = _ts(index=True)


# ---------------------------------------------------------------- system
class StrategyConfig(Base, UUIDPk, Timestamps):
    __tablename__ = "strategy_configs"

    name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    checksum: Mapped[str] = mapped_column(String(64), default="")

    __table_args__ = (UniqueConstraint("name", "version", name="uq_strategy_version"),)


class SystemEvent(Base, UUIDPk):
    __tablename__ = "system_events"

    event_type: Mapped[str] = mapped_column(String(48), index=True)
    aggregate_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = _ts(index=True)


class Alert(Base, UUIDPk):
    __tablename__ = "alerts"

    level: Mapped[str] = mapped_column(String(16), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts(index=True)


class AuditLog(Base, UUIDPk):
    """PRD 58 - append-only, who changed what."""

    __tablename__ = "audit_logs"

    actor: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128))
    before: Mapped[dict] = mapped_column(JSONB, default=dict)
    after: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = _ts(index=True)


class Backtest(Base, UUIDPk, Timestamps):
    __tablename__ = "backtests"

    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    results: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = _ts()
    finished_at: Mapped[datetime | None] = _ts()
    error: Mapped[str | None] = mapped_column(Text)
