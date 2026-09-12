"""The hot path (PRD 16 -> 26).

Detection to submission in one process, with no broker hop. ARQ is deliberately absent
from this file: a Redis round-trip plus serialization plus worker prefetch costs
50-500ms, and on a 1%-price-move gate that is the entire edge.

Flow:
    logsSubscribe -> asyncio.Queue -> N workers -> gates -> reserve -> execute -> persist

Backpressure is explicit: the queue is bounded and drops the OLDEST signal when full,
because in copy trading a stale signal is worthless while a fresh one is not.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal

from asm import runtime_config
from asm.adapters.helius import WalletSubscription, helius
from asm.config import settings
from asm.db import repo
from asm.db.session import session_scope
from asm.domain.enums import Action, EventType, OrderStatus
from asm.domain.models import ApprovedOrder, DecisionResult, Execution, SourceTrade
from asm.engine.decision import DecisionEngine
from asm.engine.market import sol_price_usd
from asm.execution.factory import get_executor
from asm.ingest.parser import parse_transaction
from asm.logging import get_logger
from asm.state import keys
from asm.state.bus import EventBus
from asm.state.client import get_redis
from asm.state.dedupe import Dedupe
from asm.state.ledger import RiskLedger

log = get_logger(__name__)

QUEUE_SIZE = 2048
WORKERS = 8
CONFIRM_WINDOW = 180  # seconds for PRD 20 multi-trader confirmation


class DecisionService:
    def __init__(self):
        self.queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.ledger = RiskLedger()
        self.engine = DecisionEngine(self.ledger)
        self.executor = get_executor()
        self.dedupe = Dedupe()
        self.bus = EventBus()
        self.redis = get_redis()
        self.wallets: set[str] = set()
        self._sub: WalletSubscription | None = None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.dropped = 0
        self.processed = 0

    # ------------------------------------------------------------------ setup
    async def refresh_wallets(self) -> None:
        async with session_scope() as s:
            wallets = set(await repo.watchlist_wallets(s))
            followed = set(await repo.followed_wallets(s))
        # Promotions can change eligibility without changing the watchlist.
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.delete(keys.FOLLOWED_SET)
            if followed:
                pipeline.sadd(keys.FOLLOWED_SET, *followed)
            await pipeline.execute()
        if wallets != self.wallets:
            log.info("watchlist_changed", watching=len(wallets), copyable=len(followed))
            self.wallets = wallets
            await self._restart_subscription()

    async def _restart_subscription(self) -> None:
        if self._sub:
            await self._sub.stop()
        if not self.wallets:
            log.warning("no_wallets_to_watch", hint="run: make seed")
            return
        self._sub = WalletSubscription(sorted(self.wallets), self._on_signature)
        self._tasks.append(asyncio.create_task(self._sub.run(), name="ws-subscription"))

    async def _on_signature(self, signature: str, slot: int, _logs: list[str]) -> None:
        try:
            self.queue.put_nowait((signature, slot))
        except asyncio.QueueFull:
            # Drop the oldest: a stale copy signal has no value, a fresh one does.
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
                self.queue.task_done()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait((signature, slot))
            if self.dropped % 50 == 1:
                log.error("signal_queue_saturated", dropped=self.dropped)

    # ------------------------------------------------------------------- run
    async def run(self) -> None:
        await runtime_config.refresh(force=True)
        await self.ledger.bootstrap(settings.starting_capital_usd)
        await self.refresh_wallets()

        for i in range(WORKERS):
            self._tasks.append(asyncio.create_task(self._worker(i), name=f"decision-{i}"))
        self._tasks.append(asyncio.create_task(self._watchlist_poller(), name="watchlist"))

        if not self.wallets:
            log.warning(
                "watchlist_empty",
                hint="add wallet addresses to seeds/wallets.txt then run: make seed",
            )
        log.info("decision_service_started", mode=settings.mode.value,
                 wallets=len(self.wallets), workers=WORKERS)
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()
        if self._sub:
            await self._sub.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _watchlist_poller(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            with contextlib.suppress(Exception):
                await runtime_config.refresh()
                await self.refresh_wallets()

    async def _worker(self, idx: int) -> None:
        while not self._stop.is_set():
            try:
                signature, slot = await self.queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._handle(signature, slot)
            except Exception as exc:
                log.exception("signal_handler_failed", signature=signature[:16], error=repr(exc))
            finally:
                self.queue.task_done()
                self.processed += 1

    # ---------------------------------------------------------------- handler
    async def _handle(self, signature: str, slot: int) -> None:
        # PRD 51 - dedupe on the signature before any network work.
        if not await self.dedupe.claim_signal(f"sig:{signature}"):
            return

        tx = await helius().get_transaction(signature)
        if not tx or (tx.get("meta") or {}).get("err"):
            return

        sol_usd = await sol_price_usd()
        trades = self._parse_for_watched(tx, signature, sol_usd)
        for trade in trades:
            await self._process_trade(trade)

    def _parse_for_watched(self, tx: dict, signature: str,
                           sol_usd: Decimal) -> list[SourceTrade]:
        """One transaction can touch several watched wallets."""
        out: list[SourceTrade] = []
        message = (tx.get("transaction") or {}).get("message") or {}
        present = set()
        for k in message.get("accountKeys") or []:
            addr = k.get("pubkey") if isinstance(k, dict) else k
            if addr in self.wallets:
                present.add(addr)
        for wallet in present:
            trade = parse_transaction(tx, wallet, signature=signature, sol_price_usd=sol_usd)
            if trade and trade.action is not Action.UNKNOWN:
                out.append(trade)
        return out

    async def _process_trade(self, trade: SourceTrade) -> None:
        if not await self.dedupe.claim_signal(trade.dedupe_key):
            return

        async with session_scope() as s:
            source_trade_id = await repo.save_source_trade(s, trade)
            row = await repo.get_trader(s, trade.wallet)
            trader = repo.trader_to_domain(row) if row else None

        await self.bus.publish(
            EventType.SOURCE_TRADE_DETECTED,
            {"wallet": trade.wallet, "mint": trade.token_mint, "action": trade.action.value,
             "value_usd": str(trade.source_value_usd),
             "observation_latency_ms": trade.observation_latency_ms},
            aggregate_id=trade.wallet,
        )

        if trader is None:
            return

        if trade.action is Action.BUY:
            await self._record_confirmation(trade)
            if not trader.status.is_followed:
                return  # watched for evidence, not copied (PRD 15)
            await self._handle_entry(trade, trader, source_trade_id)
        elif trade.action in (Action.SELL, Action.PARTIAL_SELL):
            await self._handle_source_exit(trade)

    async def _record_confirmation(self, trade: SourceTrade) -> None:
        """PRD 20 - independent buys of the same token inside a short window."""
        k = keys.recent_buys(trade.token_mint)
        async with self.redis.pipeline(transaction=True) as p:
            p.zadd(k, {trade.wallet: datetime.now(UTC).timestamp()})
            p.expire(k, CONFIRM_WINDOW)
            await p.execute()

    async def _confirmations(self, trade: SourceTrade) -> list[str]:
        cutoff = datetime.now(UTC).timestamp() - CONFIRM_WINDOW
        members = await self.redis.zrangebyscore(keys.recent_buys(trade.token_mint), cutoff, "+inf")
        return [w for w in members if w != trade.wallet]

    async def _handle_entry(self, trade: SourceTrade, trader, source_trade_id) -> None:
        confirmations = await self._confirmations(trade)
        decision, order = await self.engine.evaluate_entry(trade, trader, confirmations)

        async with session_scope() as s:
            await repo.save_decision(s, decision, trade, source_trade_id)

        if not decision.approved or order is None:
            await self.bus.publish(
                EventType.TRADE_REJECTED,
                {"wallet": trade.wallet, "mint": trade.token_mint,
                 "reasons": [r.value for r in decision.reasons]},
                aggregate_id=trade.wallet,
            )
            return

        await self.bus.publish(
            EventType.TRADE_APPROVED,
            {"wallet": trade.wallet, "mint": trade.token_mint, "size_usd": str(order.size_usd)},
            aggregate_id=trade.wallet,
        )
        await self._execute_entry(order, decision, trade, source_trade_id)

    async def _execute_entry(self, order: ApprovedOrder, decision: DecisionResult,
                             trade: SourceTrade, source_trade_id) -> None:
        if order.reservation_id is None:
            raise ValueError("entry order has no risk reservation")
        async with session_scope() as s:
            await repo.save_order(s, order)

        ex: Execution = await self.executor.execute(order)

        async with session_scope() as s:
            await repo.save_execution(s, ex)

            if ex.succeeded and ex.out_amount_raw > 0:
                # Reservation becomes real exposure at the ACTUAL filled value.
                await self.ledger.commit(order.reservation_id, ex.value_usd)
                await repo.open_position(
                    s, token_mint=order.output_mint, trader_wallet=trade.wallet, ex=ex,
                    decimals=order.candidate.market.decimals,
                    symbol=order.candidate.market.symbol, source_trade_id=source_trade_id,
                )
                await repo.log_event(s, EventType.POSITION_OPENED.value,
                                     {"mint": order.output_mint, "size_usd": str(ex.value_usd)},
                                     aggregate_id=trade.wallet)
            elif ex.status is OrderStatus.SUBMITTED:
                # Ambiguous broadcasts may still land; retain exposure until reconciliation.
                await repo.log_risk_event(
                    s, "EXECUTION_RECONCILIATION_REQUIRED",
                    "Submission outcome is unknown; capital remains reserved",
                    severity="critical", detail={"signature": ex.signature,
                                                 "order_id": str(order.id)},
                )
            else:
                # Release the hold so the capital is immediately usable again.
                await self.ledger.release(order.reservation_id)
                await repo.log_event(s, EventType.ORDER_FAILED.value,
                                     {"mint": order.output_mint, "reason": ex.failure_reason})

        await self.bus.publish(
            EventType.POSITION_OPENED if ex.succeeded else (
                EventType.ORDER_SUBMITTED if ex.status is OrderStatus.SUBMITTED else EventType.ORDER_FAILED),
            {"mint": order.output_mint, "signature": ex.signature,
             "value_usd": str(ex.value_usd), "slippage_pct": str(ex.actual_slippage_pct),
             "failure_reason": ex.failure_reason,
             "total_copy_latency_ms": ex.latency.total_copy_ms},
            aggregate_id=trade.wallet,
        )

    async def _handle_source_exit(self, trade: SourceTrade) -> None:
        """Hand the mirror signal to the position service via Redis; it owns exits."""
        async with session_scope() as s:
            pos = await repo.position_for(s, trade.token_mint, trade.wallet)
        if pos is None:
            return
        fraction = "1" if trade.action is Action.SELL else str(trade.raw.get("sold_fraction") or "0.5")
        await self.redis.setex(
            f"asm:{settings.mode.value}:source_exit:{pos.id}", 300, fraction
        )
        log.info("source_exit_signalled", mint=trade.token_mint[:8],
                 position=str(pos.id)[:8], fraction=fraction)


async def main() -> None:
    service = DecisionService()
    try:
        await service.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
