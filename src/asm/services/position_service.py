"""Position and exit manager (PRD 28-30).

Runs on a ~1Hz tick: mark every open position to market, evaluate exit rules, execute
exits, then update the risk ledger and check the harvest threshold.

Separate process from the entry hot path on purpose: an exit must still fire when the
decision service is paused, restarting, or saturated with signals. Capital preservation
does not get to depend on the thing that spends capital.
"""
from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal

from asm.adapters.jupiter import jupiter
from asm.config import settings
from asm.db import repo
from asm.db.session import session_scope
from asm.domain.enums import Action, EventType, ExitReason, PositionStatus
from asm.domain.models import ApprovedOrder, LatencyBreakdown, MarketState, Position, TradeCandidate
from asm.domain.money import SOL_MINT, ZERO, raw_to_ui
from asm.engine.exits import ExitIntent, evaluate_exits, realized_pnl, update_marks
from asm.engine.harvest import maybe_harvest
from asm.engine.market import get_market_state
from asm.execution.factory import get_executor
from asm.logging import get_logger
from asm.state.bus import EventBus
from asm.state.client import get_redis
from asm.state.ledger import RiskLedger, SystemControl

log = get_logger(__name__)

TICK_SECONDS = 1.0
HARVEST_EVERY = 60
MARK_CONCURRENCY = 12


class PositionService:
    def __init__(self):
        self.ledger = RiskLedger()
        self.control = SystemControl()
        self.executor = get_executor()
        self.bus = EventBus()
        self.redis = get_redis()
        self._stop = asyncio.Event()
        self._sem = asyncio.Semaphore(MARK_CONCURRENCY)
        self._ticks = 0

    async def run(self) -> None:
        await self.ledger.bootstrap(settings.starting_capital_usd)
        log.info("position_service_started", mode=settings.mode.value)
        while not self._stop.is_set():
            started = asyncio.get_event_loop().time()
            try:
                await self.tick()
            except Exception as exc:
                log.exception("position_tick_failed", error=repr(exc))
            elapsed = asyncio.get_event_loop().time() - started
            await asyncio.sleep(max(0.0, TICK_SECONDS - elapsed))

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> None:
        self._ticks += 1
        async with session_scope() as s:
            rows = await repo.open_positions(s)
            positions = [repo.position_to_domain(r) for r in rows]

        if positions:
            results = await asyncio.gather(
                *(self._evaluate(p) for p in positions), return_exceptions=True
            )
            for p, r in zip(positions, results, strict=False):
                if isinstance(r, BaseException):
                    log.warning("position_eval_failed", position=str(p.id)[:8], error=repr(r))

        unrealized = sum(
            (p.unrealized_pnl_usd for p in positions if p.status.value == "open"), ZERO
        )
        await self.ledger.mark(unrealized)

        if self._ticks % HARVEST_EVERY == 0:
            with contextlib.suppress(Exception):
                await maybe_harvest(self.ledger, self.bus)

    async def _evaluate(self, position: Position) -> None:
        async with self._sem:
            market = await get_market_state(position.token_mint, decimals=position.decimals)
            if market.price_usd <= 0:
                return

            if update_marks(position, market):
                await self._persist_marks(position)

            state = await self.control.get()
            if not state.allows_exits:
                return

            source_exit = await self.redis.get(
                f"asm:{settings.mode.value}:source_exit:{position.id}"
            )
            exit_liq = await self._exit_liquidity(position)

            async with session_scope() as s:
                trader_row = await repo.get_trader(s, position.trader_wallet)
                trader = repo.trader_to_domain(trader_row) if trader_row else None

            intent = evaluate_exits(
                position, market, trader,
                source_sold=source_exit is not None,
                source_sold_fraction=Decimal(source_exit) if source_exit else Decimal(1),
                exit_liquidity_usd=exit_liq,
            )
            if intent is None:
                return

            succeeded = await self._execute_exit(position, market, intent)
            if source_exit is not None and succeeded and intent.reason is ExitReason.MIRROR:
                await self.redis.delete(f"asm:{settings.mode.value}:source_exit:{position.id}")

    async def _exit_liquidity(self, position: Position) -> Decimal | None:
        """PRD 28.7 - quote the reverse direction. Can we actually get out at size?"""
        if position.amount_raw <= 0:
            return None
        q = await jupiter().quote(
            input_mint=position.token_mint, output_mint=SOL_MINT,
            amount_raw=position.amount_raw,
        )
        if q is None:
            return ZERO  # no route out at all is the worst case, not an unknown
        if q.price_impact_pct <= 0:
            return None
        return position.market_value_usd / (q.price_impact_pct / Decimal(100))

    async def _persist_marks(self, p: Position) -> None:
        from sqlalchemy import update

        from asm.db import models as M

        async with session_scope() as s:
            await s.execute(
                update(M.PositionRow).where(M.PositionRow.id == p.id).values(
                    current_price=p.current_price, peak_price=p.peak_price,
                    trailing_armed=p.trailing_armed,
                )
            )

    async def _execute_exit(self, position: Position, market: MarketState,
                            intent: ExitIntent) -> bool:
        amount_raw = int(Decimal(position.amount_raw) * intent.fraction)
        if amount_raw <= 0:
            return False
        fully = intent.is_full or amount_raw >= position.amount_raw
        if fully:
            amount_raw = position.amount_raw

        candidate = TradeCandidate(
            source_trade=_synthetic_source(position, market),
            trader=_synthetic_trader(position),
            market=market,
            quote=await jupiter().quote(
                input_mint=position.token_mint, output_mint=SOL_MINT,
                amount_raw=amount_raw,
                slippage_bps=settings.copyability.execution_slippage_bps * (2 if intent.urgent else 1),
            ),
        )
        order = ApprovedOrder(
            idempotency_key=f"exit:{position.id}:{intent.reason.value}:{amount_raw}",
            decision_id=position.id, candidate=candidate, action=Action.SELL,
            input_mint=position.token_mint, output_mint=SOL_MINT,
            in_amount_raw=amount_raw,
            size_usd=raw_to_ui(amount_raw, position.decimals) * market.price_usd,
            slippage_bps=settings.copyability.execution_slippage_bps * (2 if intent.urgent else 1),
            position_id=position.id, exit_reason=intent.reason,
            latency=LatencyBreakdown(),
        )

        async with session_scope() as s:
            await repo.save_order(s, order)

        ex = await self.executor.execute(order)
        if not ex.succeeded:
            log.warning("exit_failed", position=str(position.id)[:8],
                        reason=intent.reason.value, error=ex.failure_reason)
            async with session_scope() as s:
                await repo.save_execution(s, ex)
            return False

        exit_price = ex.actual_price or market.price_usd
        proceeds, pnl = realized_pnl(position, amount_raw, exit_price)
        cost_released = proceeds - pnl

        async with session_scope() as s:
            await repo.save_execution(s, ex)
            await repo.reduce_position(
                s, position.id, ex=ex, amount_raw=amount_raw, proceeds_usd=proceeds,
                realized_pnl=pnl, exit_reason=intent.reason.value, fully_closed=fully,
                ladder_index=intent.detail.get("ladder_index"),
            )
            await repo.log_event(
                s, (EventType.POSITION_CLOSED if fully else EventType.POSITION_REDUCED).value,
                {"mint": position.token_mint, "reason": intent.reason.value,
                 "realized_pnl_usd": str(pnl), "proceeds_usd": str(proceeds)},
                aggregate_id=position.trader_wallet,
            )

        await self.ledger.release_position(
            mint=position.token_mint, wallet=position.trader_wallet,
            cost_released_usd=cost_released, realized_pnl_usd=pnl,
            proceeds_usd=proceeds, fully_closed=fully,
        )
        # The tick marks these same objects after exits; keep them in sync.
        position.amount_raw -= amount_raw
        position.cost_basis_usd -= cost_released
        position.realized_pnl_usd += pnl
        if "ladder_index" in intent.detail:
            position.tp_levels_hit.append(intent.detail["ladder_index"])
        if fully:
            position.status = PositionStatus.CLOSED
        await self.bus.publish(
            EventType.POSITION_CLOSED if fully else EventType.POSITION_REDUCED,
            {"mint": position.token_mint, "reason": intent.reason.value,
             "realized_pnl_usd": str(pnl), "pnl_pct": intent.detail.get("pnl_pct")},
            aggregate_id=position.trader_wallet,
        )
        log.info("exit_executed", mint=position.token_mint[:8], reason=intent.reason.value,
                 pnl_usd=str(pnl.quantize(Decimal("0.01"))), full=fully)
        return True


def _synthetic_source(position: Position, market: MarketState):
    from datetime import UTC, datetime

    from asm.domain.models import SourceTrade

    return SourceTrade(
        wallet=position.trader_wallet, signature=f"exit-{position.id.hex[:12]}",
        action=Action.SELL, token_mint=position.token_mint, quote_mint=SOL_MINT,
        token_amount_raw=position.amount_raw, token_decimals=position.decimals,
        source_price=market.price_usd, source_confirmed_at=datetime.now(UTC),
    )


def _synthetic_trader(position: Position):
    from asm.domain.enums import TraderStatus
    from asm.domain.models import TraderProfile

    return TraderProfile(wallet=position.trader_wallet, status=TraderStatus.ACTIVE)


async def main() -> None:
    service = PositionService()
    try:
        await service.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
