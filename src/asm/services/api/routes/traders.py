from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy import desc, select

from asm.config import settings
from asm.db import models as M
from asm.db import repo
from asm.domain.enums import TraderStatus
from asm.services.api.deps import DB, Auth

router = APIRouter(tags=["traders"])


@router.get("/traders")
async def list_traders(db: DB, status: str | None = None, limit: int = Query(100, le=500)):
    q = select(M.Trader)
    if status:
        q = q.where(M.Trader.status == status)
    q = q.order_by(desc(M.Trader.composite_score)).limit(limit)
    rows = (await db.execute(q)).scalars()
    return {"traders": [_trader(r) for r in rows]}


@router.get("/traders/{wallet}")
async def get_trader(wallet: str, db: DB):
    row = await repo.get_trader(db, wallet)
    if row is None:
        raise HTTPException(404, "trader not found")
    return _trader(row)


@router.get("/traders/{wallet}/performance")
async def trader_performance(wallet: str, db: DB, limit: int = 90):
    row = await repo.get_trader(db, wallet)
    if row is None:
        raise HTTPException(404, "trader not found")
    metrics = (await db.execute(
        select(M.TraderDailyMetric)
        .where(M.TraderDailyMetric.trader_id == row.id)
        .order_by(desc(M.TraderDailyMetric.day)).limit(limit)
    )).scalars()
    return {
        "wallet": wallet,
        "daily": [
            {"day": m.day.isoformat(), "trades": m.trades,
             "source_pnl_usd": str(m.source_pnl_usd),
             "copied_pnl_usd": str(m.copied_pnl_usd),
             "copyable_pnl_usd": str(m.copyable_pnl_usd)}
            for m in metrics
        ],
    }


@router.get("/traders/{wallet}/copyability")
async def trader_copyability(wallet: str, db: DB):
    """PRD 35 - source vs follower. The number that decides allocation."""
    row = await repo.get_trader(db, wallet)
    if row is None:
        raise HTTPException(404, "trader not found")
    scores = (await db.execute(
        select(M.TraderScore).where(M.TraderScore.trader_id == row.id)
        .order_by(desc(M.TraderScore.computed_at)).limit(20)
    )).scalars()
    history = list(scores)
    return {
        "wallet": wallet,
        "copyability_score": str(row.copyability_score),
        "live_replication_score": str(row.live_replication_score)
        if row.live_replication_score is not None else None,
        "composite": str(row.composite_score),
        "confidence": str(row.confidence),
        "latency_tolerance_ms": row.latency_tolerance_ms,
        "median_hold_seconds": row.median_hold_seconds,
        "history": [
            {"at": h.computed_at.isoformat(), "composite": str(h.composite),
             "copyability": str(h.copyability),
             "live_replication": str(h.live_replication) if h.live_replication else None,
             "sample_size": h.sample_size}
            for h in history
        ],
    }


@router.put("/traders/{wallet}/status")
async def set_status(wallet: str, _: Auth, db: DB, status: str = Body(..., embed=True)):
    """Manual override. Audited - PRD 58."""
    try:
        new = TraderStatus(status)
    except ValueError:
        raise HTTPException(400, f"invalid status: {status}") from None
    row = await repo.get_trader(db, wallet)
    if row is None:
        raise HTTPException(404, "trader not found")
    await repo.set_trader_status(db, wallet, new)
    await repo.log_audit(db, actor="api", action="set_trader_status", target=wallet,
                         before={"status": row.status}, after={"status": new.value})
    await db.commit()
    return {"wallet": wallet, "status": new.value}


@router.post("/traders/seed")
async def seed(_: Auth, path: str = Body(default="", embed=True)):
    """Import the manual wallet list and queue backfill scoring for new entries."""
    from asm.seeding import import_seed_file

    return await import_seed_file(path or settings.seed_file, enqueue_backfill=True)


def _trader(r: M.Trader) -> dict:
    return {
        "wallet": r.wallet_address, "status": r.status, "source": r.source,
        "label": r.label, "composite_score": str(r.composite_score),
        "confidence": str(r.confidence), "copyability_score": str(r.copyability_score),
        "live_replication_score": str(r.live_replication_score)
        if r.live_replication_score is not None else None,
        "win_rate": str(r.win_rate) if r.win_rate is not None else None,
        "trade_count": r.trade_count,
        "latency_tolerance_ms": r.latency_tolerance_ms,
        "median_hold_seconds": r.median_hold_seconds,
        "last_trade_at": r.last_trade_at.isoformat() if r.last_trade_at else None,
    }
