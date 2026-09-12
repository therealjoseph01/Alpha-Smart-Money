"""PRD 11 - daily candidate discovery via the GMGN Agent API.

The funnel, and why it is shaped this way:

    GMGN smart-money feed      thousands of trades, free, unlimited
      -> distinct wallets      who is actually trading right now
      -> bulk profit screen    100 wallets per call, no monthly quota
      -> cheap rejects         dead, tiny, or losing wallets never cost us anything
      -> added as DISCOVERED   watched, never copied
      -> our own backfill      ~300 Helius credits each, only for survivors
      -> our own score         the only number allowed to move capital

The expensive step is last, on purpose. Helius wallet history costs 100 credits per
page and is capped at 1M/month; GMGN's screen has no monthly cap at all. Putting the
free filter first is what makes it possible to evaluate a thousand candidates instead
of fifty.

Nothing here promotes a wallet. Discovery grants attention; only observed copyable
performance grants capital (PRD 5.3).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from asm.adapters.gmgn import gmgn, screen, to_profile
from asm.config import settings
from asm.db import models as M
from asm.db import repo
from asm.db.session import session_scope
from asm.domain.enums import TraderStatus
from asm.logging import get_logger

log = get_logger(__name__)


async def discover_wallets(ctx: dict, limit: int | None = None,
                           auto_add: bool | None = None) -> dict:
    client = gmgn()
    if not client.enabled:
        log.info("discovery_skipped", reason="gmgn disabled or no API key")
        return {"enabled": False, "discovered": 0, "added": 0}

    auto_add = settings.gmgn_auto_add if auto_add is None else auto_add
    limit = limit or settings.gmgn_discovery_limit

    candidates = await client.discover_wallets(limit=limit)
    meta = client.discovery_meta()
    if not candidates:
        return {"enabled": True, "discovered": 0, "added": 0,
                "note": "GMGN returned no wallets"}

    # Skip anything we already know about, including wallets we previously rejected -
    # a blacklisted wallet must not silently return through discovery.
    async with session_scope() as s:
        known = {
            w for (w,) in (await s.execute(select(M.Trader.wallet_address))).all()
        }
    fresh = [w for w in candidates if w not in known]
    if not fresh:
        log.info("discovery_no_new_wallets", seen=len(candidates))
        return {"enabled": True, "discovered": len(candidates), "new": 0, "added": 0}

    # Verify these are wallets before anything else touches them.
    #
    # GMGN's `maker` field SHOULD always be the wallet that executed a trade. This does
    # not rely on that. The free suffix check runs on every candidate at zero cost, and
    # the on-chain check runs on the ones that survive the profit screen - so a bad
    # address cannot reach the watchlist through discovery any more than through the
    # paste box. Assuming an upstream field is well-formed is what cost 16,000 credits.
    from asm.ingest.verify import looks_like_launchpad_token

    suffix_rejects = [w for w in fresh if looks_like_launchpad_token(w)]
    if suffix_rejects:
        log.warning("discovery_suffix_rejects", count=len(suffix_rejects),
                    sample=[w[:8] for w in suffix_rejects[:5]])
        fresh = [w for w in fresh if w not in set(suffix_rejects)]
    if not fresh:
        return {"enabled": True, "discovered": len(candidates), "new": 0, "added": 0,
                "rejected_not_wallet": len(suffix_rejects)}

    # One bulk call for up to 100 wallets - the cheap screen.
    profits = await client.wallet_profits(fresh, period="30d")

    passed, rejected = [], []
    for wallet in fresh:
        row = profits.get(wallet)
        if row is None:
            rejected.append({"wallet": wallet, "reason": "no profit data"})
            continue
        ok, reason = screen(
            row,
            min_realized_usd=settings.gmgn_min_realized_usd,
            min_trades=settings.gmgn_min_trades,
            min_winrate=settings.gmgn_min_winrate_pct,
        )
        (passed if ok else rejected).append(
            {"wallet": wallet, "reason": reason, "profits": row})

    passed = passed[:settings.target_roster_size * 2]

    # On-chain verification, only for candidates that survived the free screen - one
    # getAccountInfo each, and never for wallets we were never going to add.
    not_wallets: list[dict] = []
    if passed:
        from asm.ingest.verify import verify_many

        verdicts = await verify_many([e["wallet"] for e in passed])
        kept = []
        for entry in passed:
            v = verdicts.get(entry["wallet"])
            if v is not None and not v.ok:
                not_wallets.append({"wallet": entry["wallet"], "kind": v.kind,
                                    "reason": v.reason})
                log.warning("discovery_rejected_non_wallet",
                            wallet=entry["wallet"][:8], kind=v.kind)
            else:
                kept.append(entry)
        passed = kept

    added: list[str] = []

    if auto_add and passed:
        async with session_scope() as s:
            for entry in passed:
                profile = to_profile(entry["wallet"], entry["profits"],
                                     meta.get(entry["wallet"]))
                await repo.upsert_trader(
                    s, profile.wallet, source=profile.source,
                    label=profile.source, status=TraderStatus.DISCOVERED,
                    meta=profile.notes,
                )
                added.append(profile.wallet)
            await repo.log_event(s, "TRADER_DISCOVERED",
                                 {"count": len(added), "source": "gmgn"},
                                 aggregate_id="gmgn")

        # NOTE: no backfill is queued here. Scoring is the expensive step (~300 Helius
        # credits each), so it is drained separately by `score_candidates` under a daily
        # budget. Discovery is free and can run often; scoring is rationed.

    log.info("discovery_complete", seen=len(candidates), new=len(fresh),
             passed=len(passed), rejected=len(rejected), added=len(added),
             not_wallets=len(suffix_rejects) + len(not_wallets))
    return {
        "enabled": True,
        "discovered": len(candidates),
        "new": len(fresh),
        "passed_screen": len(passed),
        "rejected": len(rejected),
        "rejected_not_wallet": len(suffix_rejects) + len(not_wallets),
        "added": added,
        "rejection_samples": [
            {"wallet": r["wallet"][:8], "reason": r["reason"]} for r in rejected[:8]
        ],
    }


async def refresh_watchlist(ctx: dict, days_idle: int = 14) -> dict:
    """Daily hygiene: retire wallets that have gone quiet, refresh GMGN metadata.

    A wallet that has not traded in two weeks cannot be copied, and keeping it in the
    stream costs a websocket subscription for nothing. Retiring is reversible - it sets
    SUSPENDED, not BLACKLISTED, so a wallet that comes back can be restored.
    """
    client = gmgn()
    cutoff = datetime.now(UTC) - timedelta(days=days_idle)
    retired, refreshed = [], 0

    async with session_scope() as s:
        rows = (await s.execute(
            select(M.Trader).where(
                M.Trader.status.notin_([TraderStatus.BLACKLISTED.value,
                                        TraderStatus.SUSPENDED.value])
            )
        )).scalars().all()
        wallets = [(t.wallet_address, t.last_trade_at) for t in rows]

    for wallet, last_trade_at in wallets:
        if last_trade_at and last_trade_at < cutoff:
            async with session_scope() as s:
                await repo.set_trader_status(s, wallet, TraderStatus.SUSPENDED)
                await repo.log_event(s, "TRADER_DEGRADED",
                                     {"wallet": wallet, "reason": f"idle > {days_idle}d"},
                                     aggregate_id=wallet)
            retired.append(wallet)

    # Refresh GMGN's figures as metadata for the operator - never as score input.
    active = [w for w, _last in wallets if w not in retired]
    if client.enabled and active:
        profits = await client.wallet_profits(active[:300], period="30d")
        async with session_scope() as s:
            for wallet, row in profits.items():
                trader = await repo.get_trader(s, wallet)
                if trader is None:
                    continue
                trader.meta = {**(trader.meta or {}),
                               "gmgn_realized_profit": str(row.get("realized_profit", "")),
                               "gmgn_total_profit": str(row.get("total_profit", "")),
                               "gmgn_refreshed_at": datetime.now(UTC).isoformat()}
                refreshed += 1

    log.info("watchlist_refreshed", retired=len(retired), refreshed=refreshed)
    return {"retired": retired, "retired_count": len(retired),
            "metadata_refreshed": refreshed, "idle_threshold_days": days_idle}


async def score_candidates(ctx: dict, budget: int | None = None) -> dict:
    """Spend the day's Helius budget scoring the most promising unscored candidates.

    Two speeds (PRD 5.3 - capital, and here attention, is earned):
      * while the roster is short, run at the bootstrap budget to fill it quickly;
      * once it is full, drop to the steady budget - we are only replacing churn, and
        there is no reason to keep paying to evaluate wallets we have no slot for.

    Candidates are ordered by GMGN realized profit purely as a tie-break for WHICH to
    look at first. It never affects the score they receive.
    """
    from asm.services.tasks.intelligence import backfill_wallet

    async with session_scope() as s:
        copyable = len((await s.execute(
            select(M.Trader.id).where(M.Trader.status.in_(
                [TraderStatus.ACTIVE.value, TraderStatus.PROBATION.value]))
        )).all())

        unscored = (await s.execute(
            select(M.Trader)
            .where(M.Trader.status == TraderStatus.DISCOVERED.value,
                   M.Trader.composite_score == 0)
            .order_by(M.Trader.created_at)
        )).scalars().all()
        queue = [(t.wallet_address, (t.meta or {}).get("gmgn_realized_profit"))
                 for t in unscored]

    if budget is None:
        budget = (settings.scoring_budget_steady
                  if copyable >= settings.target_roster_size
                  else settings.scoring_budget_bootstrap)

    def rank(entry):
        try:
            return -float(entry[1] or 0)
        except (TypeError, ValueError):
            return 0.0

    queue.sort(key=rank)
    todo = [w for w, _p in queue[:budget]]

    scored, failed = [], 0
    for wallet in todo:
        try:
            scored.append(await backfill_wallet(ctx, wallet))
        except Exception as exc:
            failed += 1
            log.warning("candidate_scoring_failed", wallet=wallet[:8], error=repr(exc))

    log.info("candidates_scored", scored=len(scored), failed=failed,
             budget=budget, waiting=max(0, len(queue) - budget),
             roster=copyable, target=settings.target_roster_size)
    return {
        "scored": len(scored), "failed": failed, "budget": budget,
        "still_waiting": max(0, len(queue) - budget),
        "roster_size": copyable, "roster_target": settings.target_roster_size,
        "estimated_credits": len(scored) * 300,
    }


async def maintain_roster(ctx: dict) -> dict:
    """Hold the roster at its target size by competition, not by threshold alone.

    Threshold-only promotion has a failure mode: in a good month forty wallets clear the
    bar and capital spreads so thin that nothing matters; in a bad month none do and the
    system idles. Ranking fixes both - the best N that ALSO clear the bar get copied,
    and a wallet that falls out of the top N is demoted to candidate rather than
    degraded, because being outranked is not the same as being bad.
    """
    target = settings.target_roster_size
    promoted, demoted = [], []

    async with session_scope() as s:
        eligible = (await s.execute(
            select(M.Trader)
            .where(M.Trader.status.notin_([TraderStatus.BLACKLISTED.value,
                                           TraderStatus.SUSPENDED.value]),
                   M.Trader.composite_score >= settings.copyability.min_trader_score,
                   M.Trader.confidence >= settings.copyability.min_trader_confidence)
            .order_by(M.Trader.composite_score.desc())
        )).scalars().all()

        keep = {t.wallet_address for t in eligible[:target]}

        for trader in eligible[:target]:
            if trader.status not in (TraderStatus.ACTIVE.value,
                                     TraderStatus.PROBATION.value):
                await repo.set_trader_status(s, trader.wallet_address,
                                             TraderStatus.PROBATION)
                promoted.append(trader.wallet_address)

        # Anyone currently copied but no longer in the top N steps down to candidate.
        current = (await s.execute(
            select(M.Trader).where(M.Trader.status.in_(
                [TraderStatus.ACTIVE.value, TraderStatus.PROBATION.value]))
        )).scalars().all()
        for trader in current:
            if trader.wallet_address not in keep:
                await repo.set_trader_status(s, trader.wallet_address,
                                             TraderStatus.CANDIDATE)
                demoted.append(trader.wallet_address)

        await repo.log_event(s, "TRADER_PROMOTED",
                             {"promoted": promoted, "demoted": demoted,
                              "roster": len(keep), "target": target},
                             aggregate_id="roster")

    if promoted or demoted:
        from asm.alerts import Category, alerter
        await alerter().info(
            Category.TRADER, f"Roster updated: {len(keep)}/{target} copyable",
            body=(f"Promoted {len(promoted)}, demoted {len(demoted)}."),
            detail={"promoted": promoted[:10], "demoted": demoted[:10]},
        )

    log.info("roster_maintained", roster=len(keep), target=target,
             promoted=len(promoted), demoted=len(demoted),
             eligible=len(eligible))
    return {"roster_size": len(keep), "target": target,
            "promoted": promoted, "demoted": demoted,
            "eligible_total": len(eligible),
            "short_by": max(0, target - len(keep))}
