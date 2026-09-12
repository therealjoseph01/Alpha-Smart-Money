"""Mode progression (PRD 72). backtest -> paper -> shadow -> live."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from asm import promotion
from asm.config import Mode
from asm.services.api.deps import Auth

router = APIRouter(prefix="/mode", tags=["mode"])

CONFIRM_PHRASE = "trade real money"


@router.get("")
async def get_mode():
    """Current stage, what it would advance to, and what is blocking it."""
    state = await promotion.readiness()
    return {
        **state.as_dict(),
        "open_positions": await promotion.open_positions(state.current.value),
        "note": ("paper and shadow advance on their own once the evidence is in. "
                 "Live never does - it has to be chosen."),
    }


@router.post("/advance")
async def advance(_: Auth):
    """Advance one stage, if entitled. Will not reach live."""
    result = await promotion.advance(actor="api")
    if not result["changed"]:
        raise HTTPException(409, result["reason"])
    return result


@router.post("/live")
async def go_live(_: Auth, confirm: str = Body(..., embed=True)):
    """Switch to live trading.

    Separate from /advance on purpose: the same gates apply, but this one cannot be
    reached by anything automatic, and it asks the caller to type what they are about
    to do. Real money should require an intention, not a click.
    """
    if confirm.strip().lower() != CONFIRM_PHRASE:
        raise HTTPException(
            400, f'type "{CONFIRM_PHRASE}" to confirm')

    result = await promotion.advance(force_target=Mode.LIVE, actor="api")
    if not result["changed"]:
        raise HTTPException(409, result["reason"])
    return {**result, "warning": "live trading is on; real money is now at risk"}


@router.post("/set")
async def set_mode(_: Auth, mode: str = Body(..., embed=True)):
    """Move DOWN a stage - live to shadow, shadow to paper.

    Always allowed and never gated: stepping back from risk should be as easy as
    possible. Moving up still goes through /advance or /live.
    """
    try:
        target = Mode(mode)
    except ValueError:
        raise HTTPException(400, f"unknown mode: {mode}") from None

    order = [Mode.BACKTEST, Mode.PAPER, Mode.SHADOW, Mode.LIVE]
    from asm.config import settings

    if order.index(target) > order.index(settings.mode):
        raise HTTPException(
            409, "use /mode/advance or /mode/live to move up a stage")

    held = await promotion.open_positions(settings.mode.value)
    if held:
        raise HTTPException(
            409, f"{held} position(s) still open in {settings.mode.value} - "
                 "close them before changing mode")

    previous = settings.mode
    settings.mode = target
    await promotion._persist_mode(target)

    from asm.db import repo
    from asm.db.session import session_scope

    async with session_scope() as s:
        await repo.log_audit(s, actor="api", action="mode_lowered",
                             before={"mode": previous.value},
                             after={"mode": target.value})
    return {"changed": True, "previous": previous.value, "current": target.value}
