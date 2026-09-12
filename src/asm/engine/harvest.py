"""PRD 30-33 - profit harvesting and treasury.

Principle 5.4: realized gains sitting in the trading wallet are still at risk. Harvesting
is therefore part of the risk system, not a reporting feature.

Trigger is high-water-mark based, so a round trip up and back down does not harvest
twice on the same dollars.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import update

from asm.config import settings
from asm.db import models as M
from asm.db.session import session_scope
from asm.domain.enums import EventType
from asm.domain.money import ZERO
from asm.engine.treasury import ProfitLock, Treasury, TreasuryTransferError
from asm.logging import get_logger
from asm.state.bus import EventBus
from asm.state.ledger import RiskLedger

log = get_logger(__name__)


class HarvestDecision:
    __slots__ = (
        "amount_usd",
        "new_hwm",
        "profit_usd",
        "reason",
        "retained_usd",
        "should_harvest",
    )

    def __init__(self, should_harvest: bool, amount_usd: Decimal = ZERO,
                 retained_usd: Decimal = ZERO, profit_usd: Decimal = ZERO,
                 new_hwm: Decimal = ZERO, reason: str = ""):
        self.should_harvest = should_harvest
        self.amount_usd = amount_usd
        self.retained_usd = retained_usd
        self.profit_usd = profit_usd
        self.new_hwm = new_hwm
        self.reason = reason


def evaluate_harvest(
    *, equity: Decimal, cash: Decimal, harvest_baseline: Decimal,
    threshold_pct: Decimal | None = None, take_pct: Decimal | None = None,
    min_active_usd: Decimal | None = None,
) -> HarvestDecision:
    """Pure. `harvest_baseline` is the equity level at the last harvest (PRD 30.3)."""
    cfg = settings.risk
    threshold_pct = threshold_pct if threshold_pct is not None else cfg.harvest_threshold_pct
    take_pct = take_pct if take_pct is not None else cfg.harvest_take_pct
    min_active = min_active_usd if min_active_usd is not None else cfg.min_active_capital_usd

    if harvest_baseline <= 0:
        return HarvestDecision(False, reason="no_baseline")

    gain_pct = (equity - harvest_baseline) / harvest_baseline * Decimal(100)
    if gain_pct < threshold_pct:
        return HarvestDecision(False, profit_usd=equity - harvest_baseline,
                               reason=f"gain_{gain_pct.quantize(Decimal('0.01'))}pct_below_threshold")

    profit = equity - harvest_baseline
    amount = profit * take_pct / Decimal(100)

    # 30.5 - never harvest below the minimum active capital floor.
    if equity - amount < min_active:
        amount = max(ZERO, equity - min_active)
    # Only free cash can leave; deployed capital is in positions.
    amount = min(amount, cash)

    if amount <= 0:
        return HarvestDecision(False, profit_usd=profit, reason="insufficient_free_cash")

    return HarvestDecision(
        True, amount_usd=amount.quantize(Decimal("0.01")),
        retained_usd=(profit - amount).quantize(Decimal("0.01")),
        profit_usd=profit, new_hwm=equity - amount, reason="threshold_crossed",
    )


async def maybe_harvest(ledger: RiskLedger, bus: EventBus | None = None) -> HarvestDecision:
    """Called on a timer by the position service. Idempotent per baseline."""
    snap = await ledger.snapshot()
    baseline = await _baseline(ledger, snap.total_equity_usd)

    decision = evaluate_harvest(
        equity=snap.total_equity_usd - snap.unrealized_pnl_usd,
        cash=snap.cash_usd, harvest_baseline=baseline
    )
    if not decision.should_harvest:
        return decision

    if bus:
        await bus.publish(EventType.HARVEST_THRESHOLD_REACHED,
                          {"equity": str(snap.total_equity_usd), "baseline": str(baseline),
                           "amount": str(decision.amount_usd)})

    # PRD 30.4 - the money actually leaves the trading wallet. A harvest that only
    # updates a number has protected nothing (principle 5.4).
    event = M.ProfitHarvestEvent(
        mode=settings.mode.value, equity_before_usd=snap.total_equity_usd,
        equity_after_usd=snap.total_equity_usd - decision.amount_usd,
        profit_since_hwm_usd=decision.profit_usd, harvested_usd=decision.amount_usd,
        retained_usd=decision.retained_usd, new_high_water_mark_usd=decision.new_hwm,
        destination=settings.treasury_wallet_pubkey or "paper-treasury",
        detail={"reason": decision.reason, "baseline": str(baseline)},
        occurred_at=datetime.now(UTC),
    )
    async with session_scope() as s:
        s.add(event)
        await s.flush()
        event_id = event.id

    transfer = None
    try:
        transfer = await Treasury(ledger).transfer(
            decision.amount_usd, harvest_event_id=event_id)
    except TreasuryTransferError as exc:
        # The transfer is the protection. If it fails, do NOT advance the baseline -
        # otherwise the system believes profit is safe when it is still at risk.
        log.error("harvest_transfer_failed_baseline_unchanged", error=str(exc))
        async with session_scope() as s:
            await s.execute(
                update(M.ProfitHarvestEvent)
                .where(M.ProfitHarvestEvent.id == event_id)
                .values(detail={**event.detail, "transfer_error": str(exc)[:400],
                                "baseline_advanced": False})
            )
        decision.should_harvest = False
        decision.reason = f"transfer_failed: {exc}"
        return decision

    await ledger.add_harvest(decision.amount_usd)
    await _set_baseline(ledger, snap.total_equity_usd - decision.amount_usd)

    # PRD 32 - ratchet the profit lock so the retained half is protected too.
    await ProfitLock(ledger).ratchet()

    if bus:
        await bus.publish(EventType.PROFIT_HARVESTED,
                          {"amount_usd": str(decision.amount_usd),
                           "retained_usd": str(decision.retained_usd),
                           "new_baseline": str(decision.new_hwm),
                           "transfer_status": transfer.status if transfer else None,
                           "signature": transfer.signature if transfer else None})
    log.info("profit_harvested", amount_usd=str(decision.amount_usd),
             retained_usd=str(decision.retained_usd))
    return decision


def _baseline_key(ledger: RiskLedger) -> str:
    return f"asm:{ledger.mode}:harvest:baseline"


async def _baseline(ledger: RiskLedger, current_equity: Decimal) -> Decimal:
    v = await ledger.r.get(_baseline_key(ledger))
    if v:
        return Decimal(v)
    start = settings.starting_capital_usd
    await ledger.r.set(_baseline_key(ledger), str(start))
    return start


async def _set_baseline(ledger: RiskLedger, value: Decimal) -> None:
    await ledger.r.set(_baseline_key(ledger), str(value))
