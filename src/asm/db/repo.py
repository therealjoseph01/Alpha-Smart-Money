"""Persistence. Redis is working memory; this is the ledger of record."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from asm.config import settings
from asm.db import models as M
from asm.domain.enums import PositionStatus, TraderStatus
from asm.domain.models import (
    ApprovedOrder,
    DecisionResult,
    Execution,
    Position,
    SourceTrade,
    TraderProfile,
    TraderScores,
)
from asm.domain.money import ZERO


def _mode() -> str:
    return settings.mode.value


# ------------------------------------------------------------------ traders
async def upsert_trader(
    s: AsyncSession, wallet: str, *, source: str = "seed", label: str | None = None,
    status: TraderStatus | None = None, meta: dict | None = None,
) -> M.Trader:
    stmt = (
        insert(M.Trader)
        .values(
            wallet_address=wallet, chain="solana", source=source, label=label,
            status=(status or TraderStatus.DISCOVERED).value,
            first_seen_at=datetime.now(UTC), meta=meta or {},
        )
        .on_conflict_do_update(
            index_elements=[M.Trader.chain, M.Trader.wallet_address],
            set_={"source": source, "label": label, "updated_at": datetime.now(UTC)},
        )
        .returning(M.Trader)
    )
    return (await s.execute(stmt)).scalar_one()


async def get_trader(s: AsyncSession, wallet: str) -> M.Trader | None:
    return (
        await s.execute(select(M.Trader).where(M.Trader.wallet_address == wallet))
    ).scalar_one_or_none()


async def followed_wallets(s: AsyncSession) -> list[str]:
    rows = await s.execute(
        select(M.Trader.wallet_address).where(
            M.Trader.status.in_([TraderStatus.ACTIVE.value, TraderStatus.PROBATION.value])
        )
    )
    return [r[0] for r in rows]


async def watchlist_wallets(s: AsyncSession) -> list[str]:
    """Everything we stream, including candidates we are still evaluating.

    A candidate's trades are recorded but never copied - that is how the system
    earns the evidence to promote it (PRD 15).
    """
    rows = await s.execute(
        select(M.Trader.wallet_address).where(
            M.Trader.status.notin_(
                [TraderStatus.BLACKLISTED.value, TraderStatus.SUSPENDED.value]
            )
        )
    )
    return [r[0] for r in rows]


def trader_to_domain(row: M.Trader) -> TraderProfile:
    return TraderProfile(
        wallet=row.wallet_address,
        status=TraderStatus(row.status),
        source=row.source,
        scores=TraderScores(
            copyability=row.copyability_score or ZERO,
            live_replication=row.live_replication_score,
            composite=row.composite_score or ZERO,
            confidence=row.confidence or ZERO,
            sample_size=row.trade_count or 0,
        ),
        median_hold_seconds=row.median_hold_seconds,
        latency_tolerance_ms=row.latency_tolerance_ms,
        avg_trade_size_usd=row.avg_trade_size_usd,
        win_rate=row.win_rate,
        trade_count=row.trade_count or 0,
        risk_budget_pct=row.risk_budget_pct or ZERO,
        size_multiplier=row.size_multiplier if row.size_multiplier is not None else Decimal(1),
        first_seen_at=row.first_seen_at or datetime.now(UTC),
        last_trade_at=row.last_trade_at,
        notes=row.meta or {},
    )


async def save_scores(s: AsyncSession, trader_id: uuid.UUID, scores: TraderScores,
                      inputs: dict | None = None) -> None:
    s.add(M.TraderScore(
        trader_id=trader_id, performance=scores.performance, consistency=scores.consistency,
        longevity=scores.longevity, risk_adjusted=scores.risk_adjusted,
        copyability=scores.copyability, live_replication=scores.live_replication,
        composite=scores.composite, confidence=scores.confidence,
        sample_size=scores.sample_size, inputs=inputs or {}, computed_at=datetime.now(UTC),
    ))
    await s.execute(
        update(M.Trader).where(M.Trader.id == trader_id).values(
            composite_score=scores.composite, confidence=scores.confidence,
            copyability_score=scores.copyability,
            live_replication_score=scores.live_replication,
        )
    )


async def set_trader_status(s: AsyncSession, wallet: str, status: TraderStatus) -> None:
    values: dict[str, Any] = {"status": status.value}
    if status is TraderStatus.ACTIVE:
        values["promoted_at"] = datetime.now(UTC)
    if status in (TraderStatus.DEGRADED, TraderStatus.SUSPENDED):
        values["degraded_at"] = datetime.now(UTC)
    await s.execute(update(M.Trader).where(M.Trader.wallet_address == wallet).values(**values))


# ------------------------------------------------------------- source trades
async def save_source_trade(s: AsyncSession, t: SourceTrade) -> uuid.UUID:
    stmt = (
        insert(M.SourceTradeRow)
        .values(
            id=t.id, chain=t.chain.value, wallet=t.wallet, signature=t.signature, slot=t.slot,
            action=t.action.value, token_mint=t.token_mint, quote_mint=t.quote_mint,
            token_amount_raw=Decimal(t.token_amount_raw),
            quote_amount_raw=Decimal(t.quote_amount_raw),
            source_price=t.source_price, source_value_usd=t.source_value_usd,
            dedupe_key=t.dedupe_key, source_confirmed_at=t.source_confirmed_at,
            detected_at=t.detected_at, observation_latency_ms=t.observation_latency_ms,
            raw=t.raw,
        )
        .on_conflict_do_nothing(index_elements=[M.SourceTradeRow.dedupe_key])
        .returning(M.SourceTradeRow.id)
    )
    got = (await s.execute(stmt)).scalar_one_or_none()
    if got:
        await s.execute(
            update(M.Trader)
            .where(M.Trader.wallet_address == t.wallet)
            .values(last_trade_at=t.source_confirmed_at,
                    trade_count=M.Trader.trade_count + 1)
        )
    if got is not None:
        return got
    return (await s.execute(
        select(M.SourceTradeRow.id).where(M.SourceTradeRow.dedupe_key == t.dedupe_key)
    )).scalar_one()


# ---------------------------------------------------------------- decisions
async def save_decision(s: AsyncSession, d: DecisionResult, trade: SourceTrade,
                        source_trade_id: uuid.UUID | None = None) -> None:
    s.add(M.TradeDecision(
        id=d.id, candidate_id=d.candidate_id, source_trade_id=source_trade_id,
        trader_wallet=trade.wallet, token_mint=trade.token_mint, action=trade.action.value,
        decision=d.decision.value, reason_codes=[r.value for r in d.reasons],
        gates={g.name: {"passed": g.passed, "mult": str(g.size_mult),
                        "reasons": [r.value for r in g.reasons], "detail": g.detail}
               for g in d.gates},
        inputs=d.inputs, size_usd=d.size_usd, size_pct=d.size_pct,
        size_multiplier=d.size_multiplier, mode=_mode(),
        strategy_version=d.strategy_version,
        observation_latency_ms=d.latency.observation_ms,
        analysis_latency_ms=d.latency.analysis_ms,
        execution_latency_ms=d.latency.execution_ms,
        total_copy_latency_ms=d.latency.total_copy_ms,
        decided_at=d.decided_at,
    ))


# ------------------------------------------------------------------- orders
async def save_order(s: AsyncSession, o: ApprovedOrder) -> None:
    stmt = insert(M.Order).values(
        id=o.id, idempotency_key=o.idempotency_key, decision_id=o.decision_id if o.position_id is None else None,
        position_id=o.position_id, mode=_mode(), status="created", action=o.action.value,
        trader_wallet=o.candidate.trader.wallet, token_mint=o.candidate.source_trade.token_mint,
        input_mint=o.input_mint, output_mint=o.output_mint,
        in_amount_raw=Decimal(o.in_amount_raw), size_usd=o.size_usd,
        slippage_bps=o.slippage_bps,
        exit_reason=o.exit_reason.value if o.exit_reason else None,
        reservation_id=o.reservation_id,
    ).on_conflict_do_nothing(index_elements=[M.Order.idempotency_key])
    result = await s.execute(stmt.returning(M.Order.id))
    persisted_id = result.scalar_one_or_none()
    if persisted_id is None:
        persisted_id = (await s.execute(
            select(M.Order.id).where(M.Order.idempotency_key == o.idempotency_key)
        )).scalar_one()
    o.id = persisted_id


async def save_execution(s: AsyncSession, ex: Execution) -> None:
    s.add(M.ExecutionRow(
        id=ex.id, order_id=ex.order_id, mode=ex.mode, status=ex.status.value,
        signature=ex.signature, expected_price=ex.expected_price,
        actual_price=ex.actual_price, source_price=ex.source_price,
        expected_slippage_pct=ex.expected_slippage_pct,
        actual_slippage_pct=ex.actual_slippage_pct,
        in_amount_raw=Decimal(ex.in_amount_raw), out_amount_raw=Decimal(ex.out_amount_raw),
        value_usd=ex.value_usd, network_fee_lamports=ex.network_fee_lamports,
        priority_fee_lamports=ex.priority_fee_lamports, route=ex.route[:256],
        failure_reason=ex.failure_reason,
        source_confirmed_at=ex.latency.source_confirmed_at,
        detected_at=ex.latency.detected_at,
        decision_completed_at=ex.latency.decision_completed_at,
        transaction_built_at=ex.latency.transaction_built_at,
        signed_at=ex.latency.signed_at, submitted_at=ex.latency.submitted_at,
        confirmed_at=ex.latency.confirmed_at, raw=ex.raw,
    ))
    await s.execute(
        update(M.Order).where(M.Order.id == ex.order_id).values(status=ex.status.value)
    )


# ---------------------------------------------------------------- positions
async def open_position(s: AsyncSession, *, token_mint: str, trader_wallet: str,
                        ex: Execution, decimals: int, symbol: str | None = None,
                        source_trade_id: uuid.UUID | None = None) -> M.PositionRow:
    """Append the OPENED event and materialize the projection."""
    now = datetime.now(UTC)
    row = M.PositionRow(
        token_mint=token_mint, symbol=symbol, decimals=decimals,
        trader_wallet=trader_wallet, mode=_mode(), status=PositionStatus.OPEN.value,
        amount_raw=Decimal(ex.out_amount_raw), cost_basis_usd=ex.value_usd,
        entry_price=ex.actual_price, current_price=ex.actual_price,
        peak_price=ex.actual_price,
        fees_usd=ZERO, source_trade_id=source_trade_id, opened_at=now,
    )
    s.add(row)
    await s.flush()
    s.add(M.PositionEvent(
        position_id=row.id, seq=0, event_type="opened", execution_id=ex.id,
        amount_raw_delta=Decimal(ex.out_amount_raw), usd_delta=ex.value_usd,
        price=ex.actual_price, occurred_at=now,
        detail={"signature": ex.signature, "route": ex.route},
    ))
    return row


async def reduce_position(s: AsyncSession, position_id: uuid.UUID, *, ex: Execution,
                          amount_raw: int, proceeds_usd: Decimal, realized_pnl: Decimal,
                          exit_reason: str, fully_closed: bool,
                          ladder_index: int | None = None) -> None:
    now = datetime.now(UTC)
    seq = (await s.execute(
        select(func.coalesce(func.max(M.PositionEvent.seq), 0) + 1)
        .where(M.PositionEvent.position_id == position_id)
    )).scalar_one()

    s.add(M.PositionEvent(
        position_id=position_id, seq=seq,
        event_type="closed" if fully_closed else "reduced",
        execution_id=ex.id, amount_raw_delta=Decimal(-amount_raw),
        usd_delta=proceeds_usd, price=ex.actual_price, realized_pnl_usd=realized_pnl,
        exit_reason=exit_reason, occurred_at=now,
        detail={"signature": ex.signature},
    ))

    values: dict[str, Any] = {
        "amount_raw": M.PositionRow.amount_raw - Decimal(amount_raw),
        "realized_pnl_usd": M.PositionRow.realized_pnl_usd + realized_pnl,
        "current_price": ex.actual_price,
        "status": PositionStatus.CLOSED.value if fully_closed else PositionStatus.OPEN.value,
    }
    if ladder_index is not None:
        values["tp_levels_hit"] = func.array_append(M.PositionRow.tp_levels_hit, ladder_index)
    if fully_closed:
        values["closed_at"] = now
        values["amount_raw"] = Decimal(0)
    else:
        # Cost basis shrinks proportionally so remaining PnL stays honest.
        values["cost_basis_usd"] = M.PositionRow.cost_basis_usd - (proceeds_usd - realized_pnl)
    await s.execute(update(M.PositionRow).where(M.PositionRow.id == position_id).values(**values))


async def open_positions(s: AsyncSession, mode: str | None = None) -> list[M.PositionRow]:
    rows = await s.execute(
        select(M.PositionRow).where(
            M.PositionRow.mode == (mode or _mode()),
            M.PositionRow.status.in_([PositionStatus.OPEN.value, PositionStatus.REDUCING.value]),
        )
    )
    return list(rows.scalars())


async def position_for(s: AsyncSession, token_mint: str, trader_wallet: str | None = None):
    q = select(M.PositionRow).where(
        M.PositionRow.mode == _mode(), M.PositionRow.token_mint == token_mint,
        M.PositionRow.status == PositionStatus.OPEN.value,
    )
    if trader_wallet:
        q = q.where(M.PositionRow.trader_wallet == trader_wallet)
    return (await s.execute(q.limit(1))).scalar_one_or_none()


def position_to_domain(row: M.PositionRow) -> Position:
    return Position(
        id=row.id, token_mint=row.token_mint, symbol=row.symbol,
        trader_wallet=row.trader_wallet, status=PositionStatus(row.status),
        decimals=row.decimals, amount_raw=int(row.amount_raw),
        cost_basis_usd=row.cost_basis_usd, realized_pnl_usd=row.realized_pnl_usd,
        fees_usd=row.fees_usd, entry_price=row.entry_price,
        current_price=row.current_price, peak_price=row.peak_price,
        trailing_armed=row.trailing_armed, tp_levels_hit=list(row.tp_levels_hit or []),
        opened_at=row.opened_at, closed_at=row.closed_at,
        source_trade_id=row.source_trade_id, mode=row.mode,
    )


# ------------------------------------------------------------------- system
async def log_event(s: AsyncSession, event_type: str, payload: dict,
                    aggregate_id: str | None = None) -> None:
    s.add(M.SystemEvent(event_type=event_type, aggregate_id=aggregate_id,
                        payload=payload, occurred_at=datetime.now(UTC)))


async def log_risk_event(s: AsyncSession, kind: str, message: str,
                         severity: str = "warning", detail: dict | None = None) -> None:
    s.add(M.RiskEvent(kind=kind, severity=severity, message=message,
                      detail=detail or {}, occurred_at=datetime.now(UTC)))


async def log_audit(s: AsyncSession, actor: str, action: str, target: str | None = None,
                    before: dict | None = None, after: dict | None = None,
                    ip: str | None = None) -> None:
    s.add(M.AuditLog(actor=actor, action=action, target=target, before=before or {},
                     after=after or {}, ip=ip, occurred_at=datetime.now(UTC)))


async def add_alert(s: AsyncSession, level: str, category: str, title: str,
                    body: str = "", detail: dict | None = None) -> None:
    s.add(M.Alert(level=level, category=category, title=title, body=body,
                  detail=detail or {}, created_at=datetime.now(UTC)))
