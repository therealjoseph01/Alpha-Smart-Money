"""PRD 66 - research mode.

Read-only analysis over the decision dataset. It answers the questions an operator
actually asks when tuning: which gate is costing me trades, is that gate right to reject
them, where is my latency going, and which traders are worth their allocation.

Nothing here can place a trade or change a config. It reads, aggregates and reports.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from asm.config import settings
from asm.db import analytics
from asm.db import models as M
from asm.db.session import session_scope
from asm.domain.money import ZERO
from asm.logging import get_logger

log = get_logger(__name__)


async def gate_effectiveness(ctx: dict, days: int = 30) -> dict:
    """Which gates reject the most, and what those rejections would have been worth.

    A gate that rejects constantly but only ever avoids winners is costing money. This
    cannot be answered perfectly - we never see the counterfactual fill - but pairing
    rejection counts with the token's subsequent price action is the honest approximation.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as s:
        rows = await analytics.rejection_counts(
            s, since=since, mode=settings.mode.value)

        total = (await s.execute(
            select(func.count()).select_from(M.TradeDecision)
            .where(M.TradeDecision.decided_at >= since,
                   M.TradeDecision.mode == settings.mode.value)
        )).scalar_one()

        approved = (await s.execute(
            select(func.count()).select_from(M.TradeDecision)
            .where(M.TradeDecision.decided_at >= since,
                   M.TradeDecision.decision == "copy",
                   M.TradeDecision.mode == settings.mode.value)
        )).scalar_one()

    return {
        "window_days": days,
        "signals": total,
        "approved": approved,
        "approval_rate_pct": str(
            (Decimal(approved) / Decimal(total) * 100).quantize(Decimal("0.01"))
            if total else ZERO),
        "rejections_by_reason": rows,
        "note": ("A dominant reason code is where your thresholds actually bind. "
                 "Loosen it only with a controlled experiment (PRD 46)."),
    }


async def latency_profile(ctx: dict, days: int = 7) -> dict:
    """PRD 17.2 - where the copy latency actually goes, stage by stage."""
    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as s:
        p = await analytics.latency_percentiles(s, since=since, mode=settings.mode.value)

    obs, ana = p["observation_ms"]["p50"], p["analysis_ms"]["p50"]
    advice = "insufficient data"
    if obs:
        advice = (
            "Detection dominates - upgrade to Geyser/LaserStream gRPC."
            if obs > (ana or 0) * 2
            else "Analysis dominates - cache token risk and market state more aggressively."
        )
    return {"window_days": days, **p, "advice": advice}


async def trader_contribution(ctx: dict, days: int = 30) -> dict:
    """PRD 34 - who actually earned their allocation."""
    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as s:
        rows = (await s.execute(
            select(
                M.PositionRow.trader_wallet,
                func.count(M.PositionRow.id),
                func.sum(M.PositionRow.realized_pnl_usd),
                func.sum(M.PositionRow.cost_basis_usd),
            )
            .where(M.PositionRow.mode == settings.mode.value,
                   M.PositionRow.closed_at >= since)
            .group_by(M.PositionRow.trader_wallet)
            .order_by(func.sum(M.PositionRow.realized_pnl_usd).desc())
        )).all()

    contributors = []
    for wallet, trades, pnl, deployed in rows:
        pnl = pnl or ZERO
        contributors.append({
            "wallet": wallet, "closed_trades": trades,
            "realized_pnl_usd": str(Decimal(pnl).quantize(Decimal("0.01"))),
            "capital_deployed_usd": str(Decimal(deployed or 0).quantize(Decimal("0.01"))),
            "return_on_deployed_pct": str(
                (Decimal(pnl) / Decimal(deployed) * 100).quantize(Decimal("0.01"))
                if deployed else ZERO),
        })

    losers = [c for c in contributors if Decimal(c["realized_pnl_usd"]) < 0]
    return {
        "window_days": days,
        "contributors": contributors,
        "losing_traders": len(losers),
        "note": ("Traders with negative contribution over a full window should be "
                 "degraded automatically (PRD 36/37). If they are not, the re-scoring "
                 "cadence is too slow."),
    }


async def research_summary(ctx: dict, days: int = 30) -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": settings.mode.value,
        "gates": await gate_effectiveness(ctx, days),
        "latency": await latency_profile(ctx, min(days, 7)),
        "traders": await trader_contribution(ctx, days),
    }
