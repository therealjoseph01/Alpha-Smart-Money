"""PRD 55 - emergency exit.

The kill switch stops the system taking NEW risk immediately (the signer refuses to
sign). Unwinding what is already open is a separate, slower job: it needs routes,
confirmations and retries, so it belongs on ARQ rather than in the API request.

Exits run sequentially and keep going past individual failures - one illiquid position
that cannot be routed out must not strand the other nineteen.
"""
from __future__ import annotations

from decimal import Decimal

from asm.db import repo
from asm.db.session import session_scope
from asm.domain.enums import Action, EventType, ExitReason
from asm.domain.models import ApprovedOrder, LatencyBreakdown, TradeCandidate
from asm.domain.money import SOL_MINT, raw_to_ui
from asm.engine.exits import realized_pnl
from asm.engine.market import get_market_state
from asm.execution.factory import get_executor
from asm.logging import get_logger
from asm.state.bus import EventBus
from asm.state.ledger import RiskLedger

log = get_logger(__name__)

# Emergency exits accept much worse pricing: being out matters more than the last 5%.
EMERGENCY_SLIPPAGE_BPS = 1500


async def emergency_exit_all(ctx: dict, reason: str = "kill_switch") -> dict:
    """Liquidate every open position. Safe to re-run; already-closed positions are skipped."""
    ledger = RiskLedger()
    executor = get_executor()
    bus = EventBus()

    async with session_scope() as s:
        rows = await repo.open_positions(s)
        positions = [repo.position_to_domain(r) for r in rows]

    if not positions:
        log.info("emergency_exit_nothing_open")
        return {"closed": 0, "failed": 0, "positions": 0}

    log.warning("emergency_exit_started", positions=len(positions), reason=reason)
    closed, failed = 0, 0
    errors: list[dict] = []

    for position in positions:
        try:
            ok = await _force_close(position, executor, ledger, bus, reason)
            closed += int(ok)
            failed += int(not ok)
            if not ok:
                errors.append({"position": str(position.id), "mint": position.token_mint})
        except Exception as exc:
            failed += 1
            errors.append({"position": str(position.id), "error": repr(exc)[:200]})
            log.exception("emergency_exit_position_failed",
                          position=str(position.id)[:8], error=repr(exc))

    async with session_scope() as s:
        await repo.log_risk_event(
            s, "EMERGENCY_EXIT",
            f"emergency exit: {closed} closed, {failed} failed",
            severity="critical",
            detail={"reason": reason, "closed": closed, "failed": failed, "errors": errors},
        )
        if failed:
            await repo.add_alert(
                s, "critical", "risk",
                f"Emergency exit incomplete: {failed} position(s) still open",
                body="These could not be routed out and need manual attention.",
                detail={"errors": errors},
            )

    log.warning("emergency_exit_complete", closed=closed, failed=failed)
    return {"closed": closed, "failed": failed, "positions": len(positions), "errors": errors}


async def _force_close(position, executor, ledger: RiskLedger, bus: EventBus,
                       reason: str) -> bool:
    market = await get_market_state(position.token_mint, decimals=position.decimals)
    if position.amount_raw <= 0:
        return True

    candidate = TradeCandidate(
        source_trade=_synthetic(position, market),
        trader=_synthetic_trader(position),
        market=market,
    )
    order = ApprovedOrder(
        idempotency_key=f"emergency:{position.id}:{position.amount_raw}",
        decision_id=position.id, candidate=candidate, action=Action.SELL,
        input_mint=position.token_mint, output_mint=SOL_MINT,
        in_amount_raw=position.amount_raw,
        size_usd=raw_to_ui(position.amount_raw, position.decimals) * market.price_usd,
        slippage_bps=EMERGENCY_SLIPPAGE_BPS,
        position_id=position.id, exit_reason=ExitReason.EMERGENCY,
        latency=LatencyBreakdown(),
    )

    async with session_scope() as s:
        await repo.save_order(s, order)

    ex = await executor.execute(order)

    async with session_scope() as s:
        await repo.save_execution(s, ex)
        if not ex.succeeded:
            return False

        exit_price = ex.actual_price or market.price_usd
        proceeds, pnl = realized_pnl(position, position.amount_raw, exit_price)
        await repo.reduce_position(
            s, position.id, ex=ex, amount_raw=position.amount_raw,
            proceeds_usd=proceeds, realized_pnl=pnl,
            exit_reason=ExitReason.EMERGENCY.value, fully_closed=True,
        )

    await ledger.release_position(
        mint=position.token_mint, wallet=position.trader_wallet,
        cost_released_usd=proceeds - pnl, realized_pnl_usd=pnl,
        proceeds_usd=proceeds, fully_closed=True,
    )
    await bus.publish(
        EventType.POSITION_CLOSED,
        {"mint": position.token_mint, "reason": ExitReason.EMERGENCY.value,
         "realized_pnl_usd": str(pnl), "trigger": reason},
        aggregate_id=position.trader_wallet,
    )
    log.warning("emergency_position_closed", mint=position.token_mint[:8],
                pnl_usd=str(pnl.quantize(Decimal("0.01"))))
    return True


def _synthetic(position, market):
    from datetime import UTC, datetime

    from asm.domain.models import SourceTrade

    return SourceTrade(
        wallet=position.trader_wallet, signature=f"emergency-{position.id.hex[:12]}",
        action=Action.SELL, token_mint=position.token_mint, quote_mint=SOL_MINT,
        token_amount_raw=position.amount_raw, token_decimals=position.decimals,
        source_price=market.price_usd, source_confirmed_at=datetime.now(UTC),
    )


def _synthetic_trader(position):
    from asm.domain.enums import TraderStatus
    from asm.domain.models import TraderProfile

    return TraderProfile(wallet=position.trader_wallet, status=TraderStatus.ACTIVE)
