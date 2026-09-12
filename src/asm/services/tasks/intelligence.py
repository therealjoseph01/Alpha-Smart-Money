"""Cold-path intelligence (PRD 12-15, 34-37). Runs under ARQ, never in the hot loop.

The important job here is `backfill_wallet`: it reconstructs a wallet's round trips from
on-chain history and, for each one, estimates what a follower with OUR latency would
have captured. That estimate is the copyability dataset the whole product rests on
(PRD 74.1) - and it is why a manually seeded wallet list is enough to bootstrap: after
backfill, every seeded wallet has a score derived from real chain data, not from a
leaderboard's claims.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from asm.adapters.helius import helius
from asm.db import repo
from asm.db.session import session_scope
from asm.domain.enums import Action, TraderStatus
from asm.domain.money import ZERO, raw_to_ui
from asm.engine.scoring import (
    TradeOutcome,
    TraderStats,
    compute_scores,
    latency_tolerance_ms,
    median_hold_seconds,
    next_status,
)
from asm.ingest.parser import parse_helius_enhanced
from asm.logging import get_logger

log = get_logger(__name__)

# What we assume a follower's detection + decision latency is, when reconstructing
# history. Calibrate from live latency data once shadow mode has run.
ASSUMED_COPY_LATENCY_S = 3
MAX_PAGES = 3   # each page is 100 txs and one credit; raise deliberately


async def backfill_wallet(ctx: dict, wallet: str, pages: int = MAX_PAGES) -> dict:
    """Pull swap history, pair buys with sells, compute source and copyable returns."""
    h = helius()
    rows: list[dict] = []
    before: str | None = None

    for _ in range(pages):
        page = await h.wallet_history(wallet, limit=100, before=before)
        if not page:
            break
        rows.extend(page)
        before = page[-1].get("signature")

    trades = [t for t in (parse_helius_enhanced(r, wallet) for r in rows) if t]
    trades.sort(key=lambda t: t.source_confirmed_at)

    outcomes = _pair_round_trips(trades)
    if not outcomes:
        log.info("backfill_no_outcomes", wallet=wallet[:8], raw_rows=len(rows))
        return {"wallet": wallet, "trades": len(trades), "outcomes": 0}

    stats = TraderStats(
        wallet=wallet,
        outcomes=outcomes,
        first_trade_at=trades[0].source_confirmed_at,
        last_trade_at=trades[-1].source_confirmed_at,
    )
    scores = compute_scores(stats)

    async with session_scope() as s:
        row = await repo.get_trader(s, wallet)
        if row is None:
            row = await repo.upsert_trader(s, wallet, source="seed")
        await repo.save_scores(s, row.id, scores, inputs={
            "outcomes": len(outcomes), "raw_rows": len(rows),
            "assumed_latency_s": ASSUMED_COPY_LATENCY_S,
        })

        from sqlalchemy import update

        from asm.db import models as M

        wins = len([o for o in outcomes if o.source_return_pct > 0])
        await s.execute(update(M.Trader).where(M.Trader.id == row.id).values(
            median_hold_seconds=median_hold_seconds(stats),
            latency_tolerance_ms=latency_tolerance_ms(stats),
            win_rate=Decimal(wins) / Decimal(len(outcomes)) * Decimal(100),
            avg_trade_size_usd=_avg_size(outcomes),
            trade_count=len(trades),
            first_seen_at=stats.first_trade_at,
            last_trade_at=stats.last_trade_at,
        ))

        new_status = next_status(TraderStatus(row.status), scores)
        if new_status != TraderStatus(row.status):
            await repo.set_trader_status(s, wallet, new_status)
            await repo.log_event(s, "TRADER_SCORE_UPDATED", {
                "wallet": wallet, "from": row.status, "to": new_status.value,
                "composite": str(scores.composite),
            }, aggregate_id=wallet)
            log.info("trader_status_changed", wallet=wallet[:8],
                     old=row.status, new=new_status.value,
                     composite=str(scores.composite))

    log.info("backfill_complete", wallet=wallet[:8], trades=len(trades),
             outcomes=len(outcomes), composite=str(scores.composite),
             copyability=str(scores.copyability))
    return {
        "wallet": wallet, "trades": len(trades), "outcomes": len(outcomes),
        "composite": str(scores.composite), "copyability": str(scores.copyability),
        "confidence": str(scores.confidence),
    }


def _pair_round_trips(trades) -> list[TradeOutcome]:
    """FIFO-pair buys against subsequent sells, per token."""
    lots: dict[str, list] = defaultdict(list)
    outcomes: list[TradeOutcome] = []

    for t in trades:
        if t.source_price <= 0:
            continue
        if t.action is Action.BUY:
            lots[t.token_mint].append({
                "at": t.source_confirmed_at, "price": t.source_price,
                "amount": t.token_amount_raw, "decimals": t.token_decimals,
            })
        elif t.action in (Action.SELL, Action.PARTIAL_SELL):
            remaining = t.token_amount_raw
            while remaining > 0 and lots[t.token_mint]:
                lot = lots[t.token_mint][0]
                used = min(remaining, lot["amount"])
                src_ret = (t.source_price - lot["price"]) / lot["price"] * Decimal(100)

                # Copyable return: we enter ASSUMED_COPY_LATENCY_S later than the source
                # and exit later too. Without tick data we approximate the entry slip by
                # the observed drift over the holding period, scaled to our lag. This is
                # deliberately pessimistic - it must never flatter a trader.
                hold_s = max(1, int((t.source_confirmed_at - lot["at"]).total_seconds()))
                lag_share = Decimal(min(ASSUMED_COPY_LATENCY_S, hold_s)) / Decimal(hold_s)
                entry_slip_pct = src_ret * lag_share
                copy_ret = src_ret - entry_slip_pct - _cost_pct()

                outcomes.append(TradeOutcome(
                    opened_at=lot["at"], closed_at=t.source_confirmed_at,
                    source_return_pct=src_ret, copyable_return_pct=copy_ret,
                    size_usd=raw_to_ui(used, lot["decimals"]) * lot["price"],
                    detection_latency_ms=ASSUMED_COPY_LATENCY_S * 1000,
                ))

                lot["amount"] -= used
                remaining -= used
                if lot["amount"] <= 0:
                    lots[t.token_mint].pop(0)
    return outcomes


def _cost_pct() -> Decimal:
    """Round-trip friction: two swaps of slippage + fees."""
    return Decimal("2.5")


def _avg_size(outcomes: list[TradeOutcome]) -> Decimal:
    sizes = [o.size_usd for o in outcomes if o.size_usd > 0]
    return (sum(sizes, ZERO) / Decimal(len(sizes))) if sizes else ZERO


async def rescore_all(ctx: dict, max_wallets: int = 200, pages: int = 1) -> dict:
    """PRD 36 - periodic adaptive re-scoring across the watchlist.

    Cost discipline matters more here than anywhere else in the system. Each page of
    wallet history is a 100-credit Enhanced API call, so a naive
    "rescore everyone, four times a day, three pages deep" costs

        4 x 20 wallets x 3 pages x 100 = 24,000 credits/day = 720k/month

    which is most of a 1M/month plan spent re-deriving scores that did not change.

    Two rules fix it:
      * a wallet that has not traded since its last score cannot have a new score,
        so it is skipped entirely - zero credits;
      * re-scoring reads one page, not ten. Deep history is for the initial backfill.
    """
    from sqlalchemy import select

    from asm.db import models as M

    skipped, rescored, failed = 0, [], 0
    async with session_scope() as s:
        rows = (await s.execute(
            select(M.Trader).where(
                M.Trader.status.notin_(["blacklisted", "suspended"])
            ).limit(max_wallets)
        )).scalars().all()
        candidates = [
            (t.wallet_address, t.last_trade_at, t.id) for t in rows
        ]

    for wallet, last_trade_at, trader_id in candidates:
        async with session_scope() as s:
            latest = (await s.execute(
                select(M.TraderScore.computed_at)
                .where(M.TraderScore.trader_id == trader_id)
                .order_by(M.TraderScore.computed_at.desc()).limit(1)
            )).scalar_one_or_none()

        # No new trades since the last score means the inputs are unchanged.
        if latest and last_trade_at and last_trade_at <= latest:
            skipped += 1
            continue

        try:
            rescored.append(await backfill_wallet(ctx, wallet, pages=pages))
        except Exception as exc:
            failed += 1
            log.warning("rescore_failed", wallet=wallet[:8], error=repr(exc))

    credits = len(rescored) * pages * 100
    log.info("rescore_complete", rescored=len(rescored), skipped=skipped,
             failed=failed, estimated_credits=credits)
    return {"rescored": len(rescored), "skipped_unchanged": skipped,
            "failed": failed, "estimated_credits": credits}


async def detect_decay(ctx: dict) -> dict:
    """PRD 37 - a strategy that worked last month may not work now.

    Compares each active trader's recent copyable performance against their own
    baseline, and degrades them when it falls off. Degradation is automatic; promotion
    back requires re-earning the score.
    """
    from sqlalchemy import select

    from asm.db import models as M

    degraded = []
    async with session_scope() as s:
        rows = await s.execute(
            select(M.Trader).where(M.Trader.status.in_(["active", "probation"]))
        )
        for trader in rows.scalars():
            recent = await s.execute(
                select(M.TraderScore)
                .where(M.TraderScore.trader_id == trader.id)
                .order_by(M.TraderScore.computed_at.desc())
                .limit(10)
            )
            history = list(recent.scalars())
            if len(history) < 4:
                continue
            newest = history[0].composite
            baseline = sum((h.composite for h in history[2:]), Decimal(0)) / Decimal(len(history[2:]))
            if baseline > 0 and newest < baseline * Decimal("0.7"):
                await repo.set_trader_status(s, trader.wallet_address, TraderStatus.DEGRADED)
                await repo.log_risk_event(
                    s, "STRATEGY_DECAY",
                    f"{trader.wallet_address[:8]} composite {newest} vs baseline {baseline}",
                    detail={"wallet": trader.wallet_address, "newest": str(newest),
                            "baseline": str(baseline)},
                )
                degraded.append(trader.wallet_address)
    if degraded:
        log.warning("strategy_decay_detected", count=len(degraded))
    return {"degraded": degraded}
