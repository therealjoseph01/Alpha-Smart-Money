from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, func, select

from asm.config import settings
from asm.db import models as M
from asm.services.api.deps import DB

router = APIRouter(tags=["trades"])


@router.get("/trades")
async def list_trades(db: DB, decision: str | None = None, wallet: str | None = None,
                      limit: int = Query(100, le=500)):
    """PRD 42.3 - the trade feed, rejections included. Rejections are the dataset."""
    q = select(M.TradeDecision).where(M.TradeDecision.mode == settings.mode.value)
    if decision:
        q = q.where(M.TradeDecision.decision == decision)
    if wallet:
        q = q.where(M.TradeDecision.trader_wallet == wallet)
    rows = (await db.execute(q.order_by(desc(M.TradeDecision.decided_at)).limit(limit))).scalars()
    return {"trades": [_decision(r) for r in rows]}


@router.get("/trades/{decision_id}")
async def trade_detail(decision_id: str, db: DB):
    row = (await db.execute(
        select(M.TradeDecision).where(M.TradeDecision.id == decision_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "decision not found")
    return _decision(row) | {"gates": row.gates, "inputs": row.inputs}


@router.get("/trades/stats/rejections")
async def rejection_stats(db: DB, hours: int = Query(24, le=720)):
    """Which gate is actually doing the work. Tune thresholds from this, not intuition."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (await db.execute(
        select(func.unnest(M.TradeDecision.reason_codes).label("code"), func.count())
        .where(M.TradeDecision.decided_at >= since,
               M.TradeDecision.decision == "reject",
               M.TradeDecision.mode == settings.mode.value)
        .group_by("code").order_by(func.count().desc())
    )).all()
    return {"hours": hours, "reasons": [{"code": c, "count": n} for c, n in rows]}


@router.get("/trades/stats/latency")
async def latency_stats(db: DB, hours: int = Query(24, le=720)):
    """PRD 17.2 - where the copy latency actually goes."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    row = (await db.execute(
        select(
            func.count(),
            func.percentile_cont(0.5).within_group(M.TradeDecision.observation_latency_ms),
            func.percentile_cont(0.5).within_group(M.TradeDecision.analysis_latency_ms),
            func.percentile_cont(0.95).within_group(M.TradeDecision.analysis_latency_ms),
            func.percentile_cont(0.5).within_group(M.TradeDecision.total_copy_latency_ms),
            func.percentile_cont(0.95).within_group(M.TradeDecision.total_copy_latency_ms),
        ).where(M.TradeDecision.decided_at >= since,
                M.TradeDecision.mode == settings.mode.value)
    )).one()
    return {
        "samples": row[0],
        "observation_p50_ms": row[1], "analysis_p50_ms": row[2], "analysis_p95_ms": row[3],
        "total_copy_p50_ms": row[4], "total_copy_p95_ms": row[5],
    }


def _decision(r: M.TradeDecision) -> dict:
    return {
        "id": str(r.id), "decision": r.decision, "trader": r.trader_wallet,
        "mint": r.token_mint, "action": r.action, "reason_codes": r.reason_codes,
        "size_usd": str(r.size_usd), "size_pct": str(r.size_pct),
        "observation_latency_ms": r.observation_latency_ms,
        "analysis_latency_ms": r.analysis_latency_ms,
        "total_copy_latency_ms": r.total_copy_latency_ms,
        "decided_at": r.decided_at.isoformat(),
    }
