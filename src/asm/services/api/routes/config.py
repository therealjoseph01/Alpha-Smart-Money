"""Runtime settings (PRD 57). Code holds the defaults; the database holds the values."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from asm import runtime_config as rc
from asm.services.api.deps import Auth

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
async def get_config():
    """Every tunable field with its live value, bounds and explanation."""
    await rc.refresh(force=True)
    fields = rc.current()
    order = ["Wallets", "Capital", "Risk", "Gates"]
    sections: dict[str, list[dict[str, Any]]] = {s: [] for s in order}
    for key, meta in fields.items():
        sections[meta["section"]].append({"key": key, **meta})
    return {
        "sections": [{"name": s, "fields": sections[s]} for s in order if sections[s]],
        "note": ("Saved to the database, applied to the running services within a few "
                 "seconds. Reset restores the strategy defaults built into the "
                 "code - it does not clear your wallet addresses."),
    }


@router.put("")
async def update_config(_: Auth, changes: dict[str, Any] = Body(...)):
    """Validate and save. Out-of-range values are refused, not clamped.

    Refusing is deliberate: silently clamping a mistyped daily-loss limit would leave
    the operator believing a number that is not in force.
    """
    if not changes:
        raise HTTPException(400, "no changes supplied")
    try:
        applied = await rc.apply(changes, actor="api")
    except rc.ConfigError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True, "applied": applied}


@router.post("/reset")
async def reset_config(_: Auth):
    await rc.reset(actor="api")
    return {"ok": True, **{"note": "defaults from code restored"}}
