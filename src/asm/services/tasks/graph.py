"""PRD 39 - rebuild the wallet relationship graph from observed source trades."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from asm.db import models as M
from asm.db.session import session_scope
from asm.domain.enums import Action
from asm.engine.graph import (
    TradeEvent,
    cluster_risk_multiplier,
    detect_co_trading,
    find_clusters,
    merge,
)
from asm.logging import get_logger

log = get_logger(__name__)
LOOKBACK_DAYS = 30


async def rebuild_wallet_graph(ctx: dict, days: int = LOOKBACK_DAYS) -> dict:
    since = datetime.now(UTC) - timedelta(days=days)

    async with session_scope() as s:
        rows = (await s.execute(
            select(M.SourceTradeRow.wallet, M.SourceTradeRow.token_mint,
                   M.SourceTradeRow.source_confirmed_at)
            .where(M.SourceTradeRow.source_confirmed_at >= since,
                   M.SourceTradeRow.action == Action.BUY.value)
        )).all()

    events = [TradeEvent(wallet=w, mint=m, at=t) for w, m, t in rows]
    if len(events) < 10:
        log.info("wallet_graph_insufficient_data", events=len(events))
        return {"events": len(events), "relationships": 0, "clusters": 0}

    relationships = merge(detect_co_trading(events))
    clusters = find_clusters(relationships)

    async with session_scope() as s:
        for rel in relationships:
            await s.execute(
                insert(M.TraderRelationship)
                .values(wallet_a=rel.wallet_a, wallet_b=rel.wallet_b,
                        relation=rel.relation, strength=rel.strength,
                        evidence=rel.evidence)
                .on_conflict_do_update(
                    index_elements=[M.TraderRelationship.wallet_a,
                                    M.TraderRelationship.wallet_b,
                                    M.TraderRelationship.relation],
                    set_={"strength": rel.strength, "evidence": rel.evidence,
                          "updated_at": datetime.now(UTC)},
                )
            )

        # PRD 38 - linked wallets are one bet, so each gets a fraction of the budget.
        for cluster in clusters:
            multiplier = cluster_risk_multiplier(len(cluster))
            for wallet in cluster:
                trader = (await s.execute(
                    select(M.Trader).where(M.Trader.wallet_address == wallet)
                )).scalar_one_or_none()
                if trader is not None:
                    trader.size_multiplier = multiplier
                    trader.meta = {**(trader.meta or {}),
                                   "cluster": sorted(cluster),
                                   "cluster_size": len(cluster)}
            log.warning("wallet_cluster_detected", size=len(cluster),
                        multiplier=str(multiplier),
                        wallets=[w[:8] for w in sorted(cluster)])

    if clusters:
        from asm.alerts import Category, alerter

        await alerter().warning(
            Category.TRADER,
            f"{len(clusters)} wallet cluster(s) detected",
            body="Linked wallets are one risk unit; their allocations have been reduced.",
            detail={"clusters": [sorted(c)[:5] for c in clusters[:5]]},
            dedupe_key="wallet_clusters", dedupe_seconds=86_400,
        )

    log.info("wallet_graph_rebuilt", events=len(events),
             relationships=len(relationships), clusters=len(clusters))
    return {"events": len(events), "relationships": len(relationships),
            "clusters": len(clusters),
            "clustered_wallets": sum(len(c) for c in clusters)}
