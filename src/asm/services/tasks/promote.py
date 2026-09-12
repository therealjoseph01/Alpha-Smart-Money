"""Automatic stage advancement (PRD 72). Runs hourly."""
from __future__ import annotations

from asm import promotion
from asm.config import Mode
from asm.logging import get_logger

log = get_logger(__name__)


async def auto_advance(ctx: dict) -> dict:
    """Advance paper -> shadow once the evidence supports it. Never reaches live.

    Automatic here is safe precisely because shadow cannot spend: it builds the real
    transaction and stops before signing. So there is no reason to make someone sit
    and press a button once the backtest and the paper record agree.
    """
    state = await promotion.readiness()

    if state.target is Mode.LIVE:
        return {"changed": False,
                "reason": "live is never automatic",
                **state.as_dict()}

    result = await promotion.advance(actor="auto")

    if result["changed"]:
        from asm.alerts import Category, alerter
        await alerter().warning(
            Category.SYSTEM,
            f"Advanced to {result['current']}",
            body=f"Moved from {result['previous']} to {result['current']} - the gates "
                 "for that stage are satisfied. Nothing in shadow mode can spend.",
            detail=result.get("gates", {}),
        )
        log.warning("auto_advanced", **{k: v for k, v in result.items()
                                        if isinstance(v, str)})
    return result
