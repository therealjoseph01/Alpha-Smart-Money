"""Backtest jobs (PRD 44). Long-running by nature, so they live on ARQ."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update

from asm.backtest import (
    BacktestConfig,
    BacktestEngine,
    load_from_chain,
    load_traders,
)
from asm.db import models as M
from asm.db import repo
from asm.db.session import session_scope
from asm.engine.sizing import SizingMode
from asm.logging import get_logger

log = get_logger(__name__)


async def run_backtest(ctx: dict, backtest_id: str) -> dict:
    """Load history for the requested wallets, replay it, store the result."""
    async with session_scope() as s:
        row = (await s.execute(
            select(M.Backtest).where(M.Backtest.id == uuid.UUID(backtest_id))
        )).scalar_one_or_none()
        if row is None:
            return {"error": "not_found", "id": backtest_id}
        params = dict(row.params or {})
        await s.execute(update(M.Backtest).where(M.Backtest.id == row.id).values(
            status="running", started_at=datetime.now(UTC)))

    try:
        wallets = params.get("wallets") or []
        if not wallets:
            async with session_scope() as s:
                wallets = await repo.watchlist_wallets(s)
        if not wallets:
            raise ValueError("no wallets to backtest; seed some first")

        sol_price = Decimal(str(params.get("sol_price", "150")))
        trades, histories = await load_from_chain(
            wallets, pages=int(params.get("pages", 10)), sol_price=sol_price)
        if not trades:
            raise ValueError(
                "no on-chain history found - is HELIUS_API_KEY set? "
                "the enhanced transactions API is required for backfill")

        traders = await load_traders(wallets)
        if not traders:
            raise ValueError("wallets have no scored profiles; run backfill first")

        cfg = BacktestConfig(
            starting_capital_usd=Decimal(str(params.get("starting_capital_usd", "10000"))),
            assumed_detection_latency_ms=int(params.get("detection_latency_ms", 2500)),
            assumed_analysis_latency_ms=int(params.get("analysis_latency_ms", 400)),
            sizing_mode=SizingMode(params.get("sizing_mode", SizingMode.CONFIDENCE_WEIGHTED.value)),
            seed=int(params.get("seed", 1337)),
            sol_price=sol_price,
            name=params.get("name", "backtest"),
        )
        engine = BacktestEngine(histories=histories, traders=traders, config=cfg)
        result = await engine.run(trades)
        summary = result.summary()
        summary["equity_curve"] = result.equity_curve[:2000]
        summary["trades"] = [
            {"mint": t.mint, "trader": t.trader, "opened_at": t.opened_at.isoformat(),
             "closed_at": t.closed_at.isoformat(), "entry": str(t.entry_price),
             "exit": str(t.exit_price), "pnl_usd": str(t.realized_pnl_usd),
             "return_pct": str(t.return_pct.quantize(Decimal("0.01"))),
             "exit_reason": t.exit_reason}
            for t in result.trades[:1000]
        ]

        async with session_scope() as s:
            await s.execute(update(M.Backtest).where(M.Backtest.id == uuid.UUID(backtest_id))
                            .values(status="complete", results=summary,
                                    finished_at=datetime.now(UTC)))
        log.info("backtest_stored", id=backtest_id, verdict=summary["verdict"])
        return summary

    except Exception as exc:
        log.exception("backtest_failed", id=backtest_id, error=repr(exc))
        async with session_scope() as s:
            await s.execute(update(M.Backtest).where(M.Backtest.id == uuid.UUID(backtest_id))
                            .values(status="failed", error=repr(exc)[:2000],
                                    finished_at=datetime.now(UTC)))
        return {"error": repr(exc), "id": backtest_id}
