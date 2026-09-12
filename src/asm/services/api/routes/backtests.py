from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import desc, select

from asm.db import models as M
from asm.services.api.deps import DB, Auth

router = APIRouter(tags=["backtests"])


@router.post("/backtests")
async def create_backtest(_: Auth, db: DB, params: dict[str, Any] = Body(default_factory=dict)):
    """PRD 44. Queued on ARQ - a replay over months of history is not a request."""
    row = M.Backtest(
        name=params.get("name", f"backtest-{datetime.now(UTC):%Y%m%d-%H%M%S}"),
        status="queued", params=params,
    )
    db.add(row)
    await db.commit()

    from arq import create_pool

    from asm.services.worker import redis_settings

    pool = await create_pool(redis_settings())
    try:
        await pool.enqueue_job("run_backtest", str(row.id))
    finally:
        await pool.aclose()

    return {"id": str(row.id), "status": "queued", "params": params}


@router.get("/backtests")
async def list_backtests(db: DB, limit: int = 50):
    rows = (await db.execute(
        select(M.Backtest).order_by(desc(M.Backtest.created_at)).limit(limit)
    )).scalars()
    return {"backtests": [
        {"id": str(b.id), "name": b.name, "status": b.status,
         "verdict": (b.results or {}).get("verdict"),
         "edge_retention_pct": (b.results or {}).get("edge_retention_pct"),
         "total_return_pct": (b.results or {}).get("total_return_pct"),
         "created_at": b.created_at.isoformat()}
        for b in rows
    ]}


@router.get("/backtests/{backtest_id}")
async def get_backtest(backtest_id: str, db: DB):
    try:
        bid = uuid.UUID(backtest_id)
    except ValueError:
        raise HTTPException(400, "invalid id") from None
    row = (await db.execute(select(M.Backtest).where(M.Backtest.id == bid))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "backtest not found")
    return {
        "id": str(row.id), "name": row.name, "status": row.status,
        "params": row.params, "results": row.results, "error": row.error,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
