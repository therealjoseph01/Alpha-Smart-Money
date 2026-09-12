"""PRD 66 research mode + PRD 39 graph + PRD 69 retention, exposed read-only."""
from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import desc, select

from asm.db import models as M
from asm.engine.graph import Relationship, cluster_risk_multiplier, find_clusters
from asm.services.api.deps import DB, Auth
from asm.services.tasks.research import (
    gate_effectiveness,
    latency_profile,
    research_summary,
    trader_contribution,
)

router = APIRouter(prefix="/research", tags=["research"])


@router.get("")
async def summary(days: int = Query(30, le=365)):
    return await research_summary({}, days)


@router.get("/gates")
async def gates(days: int = Query(30, le=365)):
    return await gate_effectiveness({}, days)


@router.get("/latency")
async def latency(days: int = Query(7, le=90)):
    return await latency_profile({}, days)


@router.get("/traders")
async def traders(days: int = Query(30, le=365)):
    return await trader_contribution({}, days)


@router.get("/graph")
async def graph(db: DB, min_strength: float = 60.0):
    """PRD 39 - relationships and the clusters they imply."""
    from decimal import Decimal

    rows = (await db.execute(
        select(M.TraderRelationship)
        .where(M.TraderRelationship.strength >= min_strength)
        .order_by(desc(M.TraderRelationship.strength)).limit(500)
    )).scalars()

    rels = [
        Relationship(r.wallet_a, r.wallet_b, r.relation, r.strength, r.evidence or {})
        for r in rows
    ]
    clusters = find_clusters(rels, Decimal(str(min_strength)))
    return {
        "relationships": [
            {"a": r.wallet_a, "b": r.wallet_b, "relation": r.relation,
             "strength": str(r.strength), "evidence": r.evidence}
            for r in rels
        ],
        "clusters": [
            {"wallets": sorted(c), "size": len(c),
             "size_multiplier": str(cluster_risk_multiplier(len(c)))}
            for c in clusters
        ],
        "note": "Wallets in one cluster are one risk unit, not several (PRD 38).",
    }


@router.get("/retention")
async def retention_preview(_: Auth):
    """What the retention job would delete, without deleting it."""
    from asm.services.tasks.retention import retention_report

    return await retention_report({})


@router.get("/credits")
async def credits(days: int = Query(14, le=40)):
    """Helius credit usage - the number that decides whether 24/7 is sustainable."""
    from datetime import UTC, datetime, timedelta

    from asm.config import settings
    from asm.state.client import get_redis

    r = get_redis()
    today = datetime.now(UTC)
    rows, total = [], 0
    for i in range(days):
        day = (today - timedelta(days=i)).strftime("%Y%m%d")
        data = await r.hgetall(f"asm:credits:{day}")
        if not data:
            continue
        used = int(data.get("_total", 0))
        total += used
        rows.append({
            "day": day, "total": used,
            "by_method": {k: int(v) for k, v in sorted(data.items())
                          if k != "_total" and int(v) > 0},
        })

    today_used = rows[0]["total"] if rows and rows[0]["day"] == today.strftime("%Y%m%d") else 0
    daily_budget = settings.monthly_credit_budget / 30
    projection = today_used * 30

    return {
        "monthly_budget": settings.monthly_credit_budget,
        "daily_budget": int(daily_budget),
        "today": today_used,
        "days_recorded": len(rows),
        "month_projection": int(projection),
        "within_budget": projection <= settings.monthly_credit_budget,
        "headroom_pct": (
            round((1 - projection / settings.monthly_credit_budget) * 100, 1)
            if settings.monthly_credit_budget else None),
        "history": rows,
        "note": ("Detection scales with how ACTIVE your wallets are, not how many you "
                 "watch. One hyperactive bot can cost more than twenty swing traders."),
    }
