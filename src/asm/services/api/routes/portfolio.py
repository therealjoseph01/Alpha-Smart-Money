from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import desc, select

from asm.config import settings
from asm.db import models as M
from asm.services.api.deps import DB, Ledger

router = APIRouter(tags=["portfolio"])


@router.get("/portfolio")
async def portfolio(ledger: Ledger):
    snap = await ledger.snapshot()
    exposures = await ledger.exposures()
    return {
        "mode": settings.mode.value,
        "equity_usd": str(snap.total_equity_usd),
        "cash_usd": str(snap.cash_usd),
        "deployed_usd": str(snap.deployed_usd),
        "deployed_pct": str(snap.deployed_pct),
        "unrealized_pnl_usd": str(snap.unrealized_pnl_usd),
        "realized_pnl_today_usd": str(snap.realized_pnl_today_usd),
        "high_water_mark_usd": str(snap.high_water_mark_usd),
        "drawdown_pct": str(snap.drawdown_pct),
        "harvested_total_usd": str(snap.harvested_total_usd),
        "open_positions": snap.open_positions,
        "exposure": {
            "by_token": {k: str(v) for k, v in exposures["token"].items()},
            "by_trader": {k: str(v) for k, v in exposures["trader"].items()},
        },
    }


@router.get("/portfolio/performance")
async def performance(db: DB, hours: int = Query(168, le=24 * 90)):
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = await db.execute(
        select(M.PortfolioSnapshot)
        .where(M.PortfolioSnapshot.mode == settings.mode.value,
               M.PortfolioSnapshot.captured_at >= since)
        .order_by(M.PortfolioSnapshot.captured_at)
    )
    return {
        "series": [
            {"t": r.captured_at.isoformat(), "equity": str(r.total_equity_usd),
             "drawdown_pct": str(r.drawdown_pct), "open_positions": r.open_positions}
            for r in rows.scalars()
        ]
    }


@router.get("/positions")
async def positions(db: DB, status: str | None = None, limit: int = Query(100, le=500)):
    q = select(M.PositionRow).where(M.PositionRow.mode == settings.mode.value)
    if status:
        q = q.where(M.PositionRow.status == status)
    q = q.order_by(desc(M.PositionRow.opened_at)).limit(limit)
    rows = (await db.execute(q)).scalars()
    return {"positions": [_pos(r) for r in rows]}


@router.get("/positions/{position_id}")
async def position_detail(position_id: str, db: DB):
    row = (await db.execute(
        select(M.PositionRow).where(M.PositionRow.id == position_id)
    )).scalar_one_or_none()
    if row is None:
        return {"error": "not_found"}
    events = (await db.execute(
        select(M.PositionEvent)
        .where(M.PositionEvent.position_id == position_id)
        .order_by(M.PositionEvent.seq)
    )).scalars()
    return {
        "position": _pos(row),
        "events": [
            {"seq": e.seq, "type": e.event_type, "price": str(e.price),
             "usd_delta": str(e.usd_delta), "realized_pnl_usd": str(e.realized_pnl_usd),
             "exit_reason": e.exit_reason, "at": e.occurred_at.isoformat()}
            for e in events
        ],
    }


@router.get("/harvesting")
async def harvesting(db: DB, limit: int = 50):
    rows = (await db.execute(
        select(M.ProfitHarvestEvent)
        .where(M.ProfitHarvestEvent.mode == settings.mode.value)
        .order_by(desc(M.ProfitHarvestEvent.occurred_at)).limit(limit)
    )).scalars()
    return {
        "config": {
            "threshold_pct": str(settings.risk.harvest_threshold_pct),
            "take_pct": str(settings.risk.harvest_take_pct),
            "min_active_capital_usd": str(settings.risk.min_active_capital_usd),
            "destination": settings.treasury_wallet_pubkey or "paper-treasury",
        },
        "events": [
            {"at": e.occurred_at.isoformat(), "harvested_usd": str(e.harvested_usd),
             "retained_usd": str(e.retained_usd),
             "equity_before": str(e.equity_before_usd),
             "equity_after": str(e.equity_after_usd)}
            for e in rows
        ],
    }


def _pos(r: M.PositionRow) -> dict:
    return {
        "id": str(r.id), "mint": r.token_mint, "symbol": r.symbol,
        "trader": r.trader_wallet, "status": r.status,
        "amount_raw": str(r.amount_raw), "cost_basis_usd": str(r.cost_basis_usd),
        "entry_price": str(r.entry_price), "current_price": str(r.current_price),
        "peak_price": str(r.peak_price), "trailing_armed": r.trailing_armed,
        "realized_pnl_usd": str(r.realized_pnl_usd),
        "opened_at": r.opened_at.isoformat() if r.opened_at else None,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
    }
