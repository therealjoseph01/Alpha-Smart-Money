"""Operational cold-path tasks: snapshots, attribution, daily report, resets."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from asm.config import settings
from asm.db import models as M
from asm.db import repo
from asm.db.session import session_scope
from asm.domain.money import ZERO
from asm.logging import get_logger
from asm.state.ledger import RiskLedger

log = get_logger(__name__)


async def snapshot_portfolio(ctx: dict) -> dict:
    """PRD 42.1 / 59 - periodic equity curve point."""
    ledger = RiskLedger()
    snap = await ledger.snapshot()
    async with session_scope() as s:
        s.add(M.PortfolioSnapshot(
            mode=settings.mode.value,
            active_capital_usd=snap.active_capital_usd, cash_usd=snap.cash_usd,
            deployed_usd=snap.deployed_usd, unrealized_pnl_usd=snap.unrealized_pnl_usd,
            realized_pnl_today_usd=snap.realized_pnl_today_usd,
            total_equity_usd=snap.total_equity_usd,
            high_water_mark_usd=snap.high_water_mark_usd,
            drawdown_pct=snap.drawdown_pct, harvested_total_usd=snap.harvested_total_usd,
            open_positions=snap.open_positions, sol_balance=snap.sol_balance,
            captured_at=datetime.now(UTC),
        ))
    return {"equity": str(snap.total_equity_usd), "drawdown_pct": str(snap.drawdown_pct)}


async def reset_daily_counters(ctx: dict) -> dict:
    """PRD 23 - the daily loss circuit breaker resets on the day boundary."""
    await RiskLedger().reset_daily()
    log.info("daily_counters_reset")
    return {"ok": True}


async def attribute_performance(ctx: dict) -> dict:
    """PRD 34/35 - source vs follower comparison, per trader, per day.

    This is the feedback loop that makes the system adaptive: realized copy P&L flows
    back into live_replication_score, which then dominates the composite.
    """
    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_scope() as s:
        rows = await s.execute(
            select(
                M.PositionRow.trader_wallet,
                func.count(M.PositionRow.id),
                func.sum(M.PositionRow.realized_pnl_usd),
            )
            .where(
                M.PositionRow.mode == settings.mode.value,
                M.PositionRow.closed_at >= day,
            )
            .group_by(M.PositionRow.trader_wallet)
        )
        updated = 0
        for wallet, count, pnl in rows:
            trader = await repo.get_trader(s, wallet)
            if not trader:
                continue
            existing = await s.execute(
                select(M.TraderDailyMetric).where(
                    M.TraderDailyMetric.trader_id == trader.id,
                    M.TraderDailyMetric.day == day,
                )
            )
            metric = existing.scalar_one_or_none()
            if metric:
                metric.trades = count
                metric.copied_pnl_usd = pnl or ZERO
            else:
                s.add(M.TraderDailyMetric(
                    trader_id=trader.id, day=day, trades=count,
                    copied_pnl_usd=pnl or ZERO,
                ))
            updated += 1
    return {"traders_attributed": updated}


async def daily_report(ctx: dict) -> dict:
    """PRD 67 - the operator's morning read."""
    since = datetime.now(UTC) - timedelta(days=1)
    async with session_scope() as s:
        decisions = (await s.execute(
            select(M.TradeDecision.decision, func.count())
            .where(M.TradeDecision.decided_at >= since,
                   M.TradeDecision.mode == settings.mode.value)
            .group_by(M.TradeDecision.decision)
        )).all()

        top_rejects = (await s.execute(
            select(func.unnest(M.TradeDecision.reason_codes).label("code"), func.count())
            .where(M.TradeDecision.decided_at >= since,
                   M.TradeDecision.decision == "reject")
            .group_by("code").order_by(func.count().desc()).limit(10)
        )).all()

        closed = (await s.execute(
            select(func.count(), func.sum(M.PositionRow.realized_pnl_usd))
            .where(M.PositionRow.closed_at >= since,
                   M.PositionRow.mode == settings.mode.value)
        )).one()

        snap = await RiskLedger().snapshot()
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": settings.mode.value,
            "decisions": {d: c for d, c in decisions},
            "top_rejection_reasons": [{"code": c, "count": n} for c, n in top_rejects],
            "positions_closed": closed[0] or 0,
            "realized_pnl_usd": str(closed[1] or ZERO),
            "equity_usd": str(snap.total_equity_usd),
            "drawdown_pct": str(snap.drawdown_pct),
            "open_positions": snap.open_positions,
            "harvested_total_usd": str(snap.harvested_total_usd),
        }
        await repo.add_alert(s, "info", "report", "Daily system report", detail=report)
    log.info("daily_report", **{k: v for k, v in report.items() if not isinstance(v, (dict, list))})
    return report


async def seed_from_file(ctx: dict, path: str | None = None) -> dict:
    """Import the manual wallet list, then queue a backfill for each new wallet."""
    from asm.seeding import import_seed_file

    return await import_seed_file(path or settings.seed_file, enqueue_backfill=True)


async def weekly_review(ctx: dict) -> dict:
    """PRD 68 - weekly strategy review, drafted by the AI layer when it is enabled.

    The AI never decides anything here; it summarises the week's numbers for a human.
    Without an API key the raw statistics are still produced and alerted.
    """
    from asm.alerts import Category, Level, alerter
    from asm.services.tasks.research import research_summary

    stats = await research_summary(ctx, days=7)
    commentary = None

    try:
        from asm.ai import Analyst
        from asm.ai import weekly_review as ai_weekly_review

        if Analyst().enabled:
            commentary = (await ai_weekly_review(stats)).as_dict()
    except Exception as exc:
        log.warning("weekly_review_ai_failed", error=repr(exc))

    payload = {"stats": stats, "commentary": commentary}
    async with session_scope() as s:
        await repo.add_alert(s, "info", "report", "Weekly strategy review",
                             body=(commentary or {}).get("text", "")[:4000],
                             detail=payload)
    await alerter().send(Level.INFO, Category.REPORT, "Weekly strategy review",
                         body=(commentary or {}).get("text", "")[:1500])
    return payload
