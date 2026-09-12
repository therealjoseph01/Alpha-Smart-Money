"""PRD 55 - emergency controls, and PRD 56 - observability."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, HTTPException, Request, Response, status
from sqlalchemy import desc, select

from asm.config import settings
from asm.db import models as M
from asm.db import repo
from asm.domain.enums import SystemState
from asm.services.api.deps import DB, Auth, Control, Ledger
from asm.state.bus import EventBus

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(control: Control, ledger: Ledger):
    from asm.adapters.gmgn import gmgn
    from asm.adapters.helius import helius
    from asm.adapters.jupiter import jupiter

    state = await control.get()
    snap = await ledger.snapshot()
    return {
        "status": "ok",
        "mode": settings.mode.value,
        "system_state": state.value,
        "started": state.is_started,
        "accepting_entries": state.allows_entries,
        "managing_exits": state.allows_exits,
        "equity_usd": str(snap.total_equity_usd),
        "open_positions": snap.open_positions,
        "providers": {
            "helius": "degraded" if helius().degraded else "ok",
            "jupiter": "degraded" if jupiter().degraded else "ok",
            "gmgn": "disabled" if not settings.gmgn_enabled
                    else ("degraded" if gmgn().degraded else "ok"),
        },
    }


@router.get("/risk")
async def risk_config(ledger: Ledger):
    snap = await ledger.snapshot()
    r, c = settings.risk, settings.copyability
    return {
        "preset": r.preset.value,
        "limits": {
            "base_position_pct": str(r.base_position_pct),
            "max_position_pct": str(r.max_position_pct),
            "max_daily_loss_pct": str(r.max_daily_loss_pct),
            "max_drawdown_pct": str(r.max_drawdown_pct),
            "max_portfolio_deployed_pct": str(r.max_portfolio_deployed_pct),
            "max_trader_risk_budget_pct": str(r.max_trader_risk_budget_pct),
            "max_token_exposure_pct": str(c.max_token_exposure_pct),
            "max_open_positions": c.max_open_positions,
            "stop_loss_pct": str(r.stop_loss_pct),
            "trailing_stop_pct": str(r.trailing_stop_pct),
        },
        "current": {
            "daily_loss_pct": str(
                -snap.realized_pnl_today_usd / snap.total_equity_usd * 100
                if snap.total_equity_usd > 0 else 0
            ),
            "drawdown_pct": str(snap.drawdown_pct),
            "deployed_pct": str(snap.deployed_pct),
            "open_positions": snap.open_positions,
        },
    }


@router.get("/risk/events")
async def risk_events(db: DB, limit: int = 100):
    rows = (await db.execute(
        select(M.RiskEvent).order_by(desc(M.RiskEvent.occurred_at)).limit(limit)
    )).scalars()
    return {"events": [
        {"kind": e.kind, "severity": e.severity, "message": e.message,
         "detail": e.detail, "at": e.occurred_at.isoformat()} for e in rows
    ]}


@router.post("/system/start")
async def start(_: Auth, db: DB, control: Control):
    """Begin trading. Nothing is copied until this is called.

    The state lives in Redis, so once started the system keeps running across service
    restarts and regardless of whether any browser is open - the dashboard is a window
    onto the system, not the thing running it.
    """
    before = await control.get()
    try:
        await control.start(actor="api")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None
    await repo.log_audit(db, actor="api", action="system_start",
                         before={"state": before.value}, after={"state": "running"})
    await db.commit()
    return {"system_state": "running", "was": before.value}


@router.post("/system/stop")
async def stop(_: Auth, db: DB, control: Control):
    """Stop opening new positions. Exits keep running so nothing is stranded."""
    before = await control.get()
    await control.stop(actor="api")
    await repo.log_audit(db, actor="api", action="system_stop",
                         before={"state": before.value}, after={"state": "stopped"})
    await db.commit()
    return {"system_state": "stopped", "was": before.value,
            "note": "Open positions are still managed and will still exit."}


@router.post("/system/pause")
async def pause(_: Auth, db: DB, control: Control, hard: bool = Body(default=False, embed=True)):
    """Soft pause blocks new entries; hard pause also blocks the signer."""
    state = SystemState.HARD_PAUSED if hard else SystemState.SOFT_PAUSED
    before = await control.get()
    await control.set(state, actor="api")
    await repo.log_audit(db, actor="api", action="system_pause",
                         before={"state": before.value}, after={"state": state.value})
    await db.commit()
    return {"system_state": state.value}


@router.post("/system/resume")
async def resume(_: Auth, db: DB, control: Control):
    before = await control.get()
    if before is SystemState.KILLED:
        raise HTTPException(409, "kill switch is active; it must be cleared deliberately")
    await control.set(SystemState.RUNNING, actor="api")
    await repo.log_audit(db, actor="api", action="system_resume",
                         before={"state": before.value}, after={"state": "running"})
    await db.commit()
    return {"system_state": "running"}


@router.post("/system/emergency-stop")
async def emergency_stop(_: Auth, db: DB, control: Control,
                         exit_positions: bool = Body(default=True, embed=True)):
    """PRD 55 - global kill switch. The signer checks this flag itself, so it holds
    even if every other service is unresponsive."""
    await control.kill(actor="api")
    await repo.log_risk_event(db, "EMERGENCY_STOP", "kill switch activated via API",
                              severity="critical", detail={"exit_positions": exit_positions})
    await repo.log_audit(db, actor="api", action="emergency_stop",
                         after={"exit_positions": exit_positions})
    await db.commit()
    exit_queued = False
    if exit_positions:
        from arq import create_pool

        from asm.services.worker import redis_settings
        pool = await create_pool(redis_settings())
        try:
            job = await pool.enqueue_job("emergency_exit_all")
            exit_queued = job is not None
        except Exception:
            pass
        finally:
            await pool.aclose()
    return {"system_state": "killed", "exit_queued": exit_queued}


@router.post("/system/clear-kill-switch")
async def clear_kill(_: Auth, db: DB, control: Control,
                     confirm: str = Body(..., embed=True)):
    if confirm != "I understand the risk":
        raise HTTPException(400, "confirmation phrase required")
    await control.clear_kill(actor="api")
    await repo.log_audit(db, actor="api", action="clear_kill_switch")
    await db.commit()
    return {"system_state": "hard_paused",
            "note": "resume explicitly when you are ready"}


@router.get("/events")
async def events(limit: int = 50):
    return {"events": await EventBus().tail(limit)}


@router.get("/alerts")
async def alerts(db: DB, limit: int = 50, unacknowledged_only: bool = False):
    q = select(M.Alert).order_by(desc(M.Alert.created_at)).limit(limit)
    if unacknowledged_only:
        q = q.where(M.Alert.acknowledged.is_(False))
    rows = (await db.execute(q)).scalars()
    return {"alerts": [
        {"id": str(a.id), "level": a.level, "category": a.category, "title": a.title,
         "body": a.body, "detail": a.detail, "acknowledged": a.acknowledged,
         "at": a.created_at.isoformat()} for a in rows
    ]}


@router.post("/auth/login")
async def login(request: Request, response: Response,
                password: str = Body(..., embed=True)):
    """Exchange the password for an httpOnly session cookie.

    The token is never handed to the page, so no script can read it - and the session
    expires on its own rather than living in localStorage forever.
    """
    from asm.services.api import session
    from asm.services.api.deps import token_is_valid

    if await session.attempts_remaining(request) <= 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed attempts; try again in 15 minutes")

    if not token_is_valid(password.strip()):
        # Same shape of failure whichever way it is wrong, and slow enough not to be
        # a comfortable thing to guess against.
        await asyncio.sleep(0.4)
        left = await session.record_failure(request)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"incorrect password ({left} attempt{'' if left == 1 else 's'} left)"
            if left else "too many failed attempts; locked for 15 minutes")

    await session.clear_failures(request)
    await session.create(request, response)
    return {"ok": True, "expires_in": settings.session_ttl_seconds}


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    from asm.services.api import session

    await session.destroy(request, response)
    return {"ok": True}


@router.post("/auth/revoke-all")
async def revoke_all(_: Auth):
    """Kill every session everywhere - use after changing the password."""
    from asm.services.api import session

    return {"ok": True, "revoked": await session.destroy_all()}


@router.get("/auth/check")
async def auth_check(request: Request, _: Auth):
    """Verify the caller is authenticated, without changing anything."""
    from asm.services.api import session

    return {"ok": True, "expires_in": await session.remaining_seconds(request)}
