"""PRD 69 - data retention.

Two competing needs: the copyability dataset is the moat (PRD 74.1) and must not be
casually deleted, while high-frequency telemetry will grow without bound.

So the policy is asymmetric:
  * decisions, executions, positions, harvests, audit  -> keep indefinitely (the moat)
  * snapshots, token prices, raw events, alerts        -> aged out
  * raw payloads on old rows                           -> stripped, rows kept

Audit logs are never deleted by an automated job. If they can be, they are not audit.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update

from asm.db import models as M
from asm.db.session import session_scope
from asm.logging import get_logger

log = get_logger(__name__)

# table -> (timestamp column, days to keep). None means keep forever.
POLICY: dict[str, tuple[Any, int | None]] = {
    "system_events": (M.SystemEvent.occurred_at, 30),
    "token_snapshots": (M.TokenSnapshot.captured_at, 60),
    "portfolio_snapshots": (M.PortfolioSnapshot.captured_at, 365),
    "alerts": (M.Alert.created_at, 90),
    "risk_events": (M.RiskEvent.occurred_at, 365),
    "trader_scores": (M.TraderScore.computed_at, 365),
    "token_risk_scores": (M.TokenRiskScore.evaluated_at, 90),
    # The moat - never auto-deleted.
    "trade_decisions": (M.TradeDecision.decided_at, None),
    "executions": (M.ExecutionRow.created_at, None),
    "positions": (M.PositionRow.opened_at, None),
    "position_events": (M.PositionEvent.occurred_at, None),
    "profit_harvest_events": (M.ProfitHarvestEvent.occurred_at, None),
    "audit_logs": (M.AuditLog.occurred_at, None),
}

MODELS = {
    "system_events": M.SystemEvent, "token_snapshots": M.TokenSnapshot,
    "portfolio_snapshots": M.PortfolioSnapshot, "alerts": M.Alert,
    "risk_events": M.RiskEvent, "trader_scores": M.TraderScore,
    "token_risk_scores": M.TokenRiskScore,
}

# Strip bulky raw payloads from old rows we still want to keep.
STRIP_AFTER_DAYS = 90


async def apply_retention(ctx: dict, dry_run: bool = False) -> dict:
    now = datetime.now(UTC)
    deleted: dict[str, int] = {}

    async with session_scope() as s:
        for table, (column, days) in POLICY.items():
            if days is None or table not in MODELS:
                continue
            cutoff = now - timedelta(days=days)
            model = MODELS[table]
            count = (await s.execute(
                select(func.count()).select_from(model).where(column < cutoff)
            )).scalar_one()
            if not count:
                continue
            if not dry_run:
                await s.execute(delete(model).where(column < cutoff))
            deleted[table] = count

        stripped = 0
        if not dry_run:
            strip_cutoff = now - timedelta(days=STRIP_AFTER_DAYS)
            # Keep the execution record and its measured quality; drop the raw blob.
            result = await s.execute(
                update(M.ExecutionRow)
                .where(M.ExecutionRow.created_at < strip_cutoff,
                       M.ExecutionRow.raw != {})
                .values(raw={})
            )
            stripped += cast(CursorResult, result).rowcount or 0
            result = await s.execute(
                update(M.SourceTradeRow)
                .where(M.SourceTradeRow.detected_at < strip_cutoff,
                       M.SourceTradeRow.raw != {})
                .values(raw={})
            )
            stripped += cast(CursorResult, result).rowcount or 0

    log.info("retention_applied", dry_run=dry_run, deleted=deleted, stripped=stripped)
    return {"dry_run": dry_run, "deleted": deleted, "raw_payloads_stripped": stripped,
            "preserved_forever": [t for t, (_c, d) in POLICY.items() if d is None]}


async def retention_report(ctx: dict) -> dict:
    """What retention WOULD delete, without deleting it."""
    return await apply_retention(ctx, dry_run=True)
