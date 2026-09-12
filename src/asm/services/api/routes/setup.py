"""PRD 42 - guided setup, so the onboarding steps are buttons rather than commands.

Everything here maps onto an existing CLI path; the UI just drives it. The long jobs
(backfill, backtest) are queued on ARQ and this endpoint reports their progress by
counting what has landed in the database, rather than trying to track job state.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body
from sqlalchemy import desc, func, select

from asm.config import settings
from asm.db import models as M
from asm.db import repo
from asm.domain.enums import TraderStatus
from asm.services.api.deps import DB, Auth

router = APIRouter(prefix="/setup", tags=["setup"])


def _count_seed_file(path: str) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    return len([
        ln for ln in p.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ])


async def _enqueue(job: str, *args) -> bool:
    from arq import create_pool

    from asm.services.worker import redis_settings

    pool = await create_pool(redis_settings())
    try:
        await pool.enqueue_job(job, *args)
        return True
    finally:
        await pool.aclose()


@router.get("")
async def setup_state(db: DB) -> dict[str, Any]:
    """Everything the setup screen needs to show which step you are on."""
    # The seed file is now optional - wallets are normally pasted into the UI - but
    # count it if present. Off the event loop: this handler polls every few seconds.
    seed_lines = await asyncio.to_thread(_count_seed_file, settings.seed_file)

    total = (await db.execute(select(func.count()).select_from(M.Trader))).scalar_one()
    scored = (await db.execute(
        select(func.count()).select_from(M.Trader).where(M.Trader.composite_score > 0)
    )).scalar_one()
    copyable = (await db.execute(
        select(func.count()).select_from(M.Trader).where(
            M.Trader.status.in_([TraderStatus.ACTIVE.value, TraderStatus.PROBATION.value])
        )
    )).scalar_one()

    latest = (await db.execute(
        select(M.Backtest).order_by(desc(M.Backtest.created_at)).limit(1)
    )).scalar_one_or_none()

    steps = [
        {
            "key": "helius",
            "title": "Helius API key",
            "done": bool(settings.helius_api_key),
            "detail": ("Key is set." if settings.helius_api_key
                       else "Add HELIUS_API_KEY to .env and restart. "
                            "Without it no wallet history can be fetched."),
            "action": None,
        },
        {
            "key": "seed",
            "title": "Seed wallets",
            "done": total > 0,
            "detail": (f"{total} wallet(s) imported."
                       if total else
                       f"{seed_lines} address(es) in {settings.seed_file}. "
                       "Paste wallets there, then import."),
            "action": "seed" if seed_lines or total == 0 else None,
            "count": total,
            "file_count": seed_lines,
        },
        {
            "key": "backfill",
            "title": "Score wallets",
            "done": scored > 0,
            "detail": (f"{scored} of {total} wallet(s) scored, {copyable} copyable."
                       if total else "Seed wallets first."),
            "action": "backfill" if total > 0 else None,
            "count": scored,
        },
        {
            "key": "backtest",
            "title": "Backtest",
            "done": bool(latest and latest.status == "complete"),
            "detail": (
                f"{latest.status}"
                + (f" — {(latest.results or {}).get('verdict', '')}"
                   if latest.status == "complete" else "")
                if latest else "Not run yet. This gates live trading."
            ),
            "action": "backtest" if scored > 0 else None,
            "verdict": (latest.results or {}).get("verdict") if latest else None,
            "backtest_id": str(latest.id) if latest else None,
        },
    ]

    return {
        "mode": settings.mode.value,
        "ready": all(s["done"] for s in steps),
        "steps": steps,
        "seed_file": settings.seed_file,
    }


@router.post("/seed")
async def run_seed(_: Auth) -> dict[str, Any]:
    """Import seeds/wallets.txt and queue scoring for anything new."""
    from asm.seeding import SeedError, import_seed_file

    try:
        return {"ok": True, **await import_seed_file(settings.seed_file,
                                                     enqueue_backfill=True)}
    except SeedError as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/wallets")
async def add_wallets(_: Auth, text: str = Body(default="", embed=True),
                      backfill: bool = Body(default=True, embed=True)):
    """Paste wallet addresses; they are stored in the database directly.

    No file editing. One address per line, or `address,label,note`.
    """
    from asm.seeding import SeedError, import_wallets, parse_seed_text

    try:
        entries = parse_seed_text(text)
    except SeedError as exc:
        return {"ok": False, "error": str(exc)}
    if not entries:
        return {"ok": False, "error": "no valid Solana addresses found"}

    result = await import_wallets(entries, source="ui", enqueue_backfill=backfill)
    if result["imported"] == 0 and result.get("rejected"):
        first = result["rejected"][0]
        return {"ok": False,
                "error": f"{first['wallet'][:8]}… {first['reason']}",
                **result}
    return {"ok": True, **result}


@router.delete("/wallets/{wallet}")
async def remove_wallet(wallet: str, _: Auth, db: DB):
    """Blacklist a wallet so it is never watched or copied again.

    Deliberately not a hard delete: its trade history stays part of the copyability
    dataset, and a removed wallet must not silently reappear on the next import.
    """
    row = await repo.get_trader(db, wallet)
    if row is None:
        return {"ok": False, "error": "wallet not found"}
    await repo.set_trader_status(db, wallet, TraderStatus.BLACKLISTED)
    await repo.log_audit(db, actor="ui", action="wallet_blacklisted", target=wallet,
                         before={"status": row.status})
    await db.commit()
    return {"ok": True, "wallet": wallet, "status": "blacklisted"}


@router.get("/wallets")
async def list_wallets(db: DB, include_removed: bool = False):
    """Everything on (or off) the watchlist, with why it is where it is."""
    q = select(M.Trader).order_by(desc(M.Trader.composite_score),
                                  desc(M.Trader.created_at))
    if not include_removed:
        q = q.where(M.Trader.status != TraderStatus.BLACKLISTED.value)
    rows = (await db.execute(q)).scalars().all()
    return {
        "wallets": [{
            "wallet": t.wallet_address,
            "status": t.status,
            "source": t.source,
            "label": t.label,
            "score": str(t.composite_score),
            "copyability": str(t.copyability_score),
            "confidence": str(t.confidence),
            "trade_count": t.trade_count,
            "copyable": t.status in (TraderStatus.ACTIVE.value, TraderStatus.PROBATION.value),
            "gmgn_realized_profit": (t.meta or {}).get("gmgn_realized_profit"),
            "last_trade_at": t.last_trade_at.isoformat() if t.last_trade_at else None,
            "added_at": t.created_at.isoformat() if t.created_at else None,
        } for t in rows],
        "counts": {
            "total": len(rows),
            "copyable": sum(1 for t in rows
                            if t.status in (TraderStatus.ACTIVE.value,
                                            TraderStatus.PROBATION.value)),
        },
    }


@router.post("/discover")
async def run_discovery(_: Auth, auto_add: bool = Body(default=True, embed=True)):
    """Pull today's smart-money wallets from GMGN, screen them, add the survivors."""
    from asm.services.tasks.discovery import discover_wallets

    return await discover_wallets({}, auto_add=auto_add)


@router.post("/wallets/{wallet}/remove")
async def remove_wallet_post(wallet: str, _: Auth, db: DB):
    """POST mirror of the DELETE route, so the dashboard needs only one verb."""
    return await remove_wallet(wallet, _, db)


@router.post("/wallets/{wallet}/restore")
async def restore_wallet(wallet: str, _: Auth, db: DB):
    """Put a removed wallet back on the watchlist as an unproven candidate."""
    row = await repo.get_trader(db, wallet)
    if row is None:
        return {"ok": False, "error": "wallet not found"}
    await repo.set_trader_status(db, wallet, TraderStatus.DISCOVERED)
    await repo.log_audit(db, actor="ui", action="wallet_restored", target=wallet,
                         before={"status": row.status})
    await db.commit()
    return {"ok": True, "wallet": wallet, "status": "discovered"}


# Scoring reads wallet history through the Enhanced Transactions API at 100 credits a
# page, ~250 per wallet. The scheduled path is budgeted; this button was not, so a
# watchlist that had grown to a few hundred would queue tens of thousands of credits
# from one click with no warning.
CREDITS_PER_WALLET = 250
MAX_UNCONFIRMED_WALLETS = 40


@router.post("/backfill")
async def run_backfill(_: Auth, db: DB,
                       pages: int = Body(default=settings.backfill_pages, embed=True),
                       confirm: bool = Body(default=False, embed=True),
                       only_unscored: bool = Body(default=True, embed=True)):
    """Queue scoring for watched wallets, with the cost stated before it is spent."""
    from sqlalchemy import select

    if only_unscored:
        rows = (await db.execute(
            select(M.Trader.wallet_address).where(
                M.Trader.status.notin_([TraderStatus.BLACKLISTED.value,
                                        TraderStatus.SUSPENDED.value]),
                M.Trader.composite_score == 0,
            )
        )).scalars().all()
        wallets = list(rows)
    else:
        wallets = await repo.watchlist_wallets(db)

    if not wallets:
        return {"ok": True, "queued": 0,
                "note": "Nothing to score — every watched wallet already has a score."}

    estimate = len(wallets) * CREDITS_PER_WALLET
    if len(wallets) > MAX_UNCONFIRMED_WALLETS and not confirm:
        return {
            "ok": False,
            "needs_confirmation": True,
            "wallets": len(wallets),
            "estimated_credits": estimate,
            "error": (f"This would score {len(wallets)} wallets and use about "
                      f"{estimate:,} Helius credits. Confirm to continue."),
        }

    for wallet in wallets:
        await _enqueue("backfill_wallet", wallet, pages)
    return {"ok": True, "queued": len(wallets), "estimated_credits": estimate,
            "note": f"Scoring {len(wallets)} wallet(s), about {estimate:,} credits. "
                    "Runs on the worker and takes a few minutes."}


@router.post("/backtest")
async def run_backtest(_: Auth, db: DB, params: dict[str, Any] = Body(default_factory=dict)):
    """Queue a backtest over the watchlist. The verdict gates live trading."""
    from datetime import UTC, datetime

    row = M.Backtest(
        name=params.get("name", f"ui-{datetime.now(UTC):%Y%m%d-%H%M%S}"),
        status="queued", params=params,
    )
    db.add(row)
    await db.commit()
    await _enqueue("run_backtest", str(row.id))
    return {"ok": True, "id": str(row.id), "status": "queued"}
