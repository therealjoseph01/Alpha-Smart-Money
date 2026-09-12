"""PRD 51 - duplicate signals must never produce duplicate orders.

Geyser/websocket streams replay on reconnect. This is the first thing the hot path does.
"""
from __future__ import annotations

from redis.asyncio import Redis

from asm.state import keys
from asm.state.client import get_redis

SIGNAL_TTL = 3600
ORDER_TTL = 86_400


class Dedupe:
    def __init__(self, redis: Redis | None = None):
        self.r = redis or get_redis()

    async def claim_signal(self, dedupe_key: str, ttl: int = SIGNAL_TTL) -> bool:
        """True if this is the first time we have seen this source transaction."""
        return bool(await self.r.set(keys.dedupe(dedupe_key), "1", nx=True, ex=ttl))

    async def claim_order(self, idempotency_key: str, ttl: int = ORDER_TTL) -> bool:
        """True if no order has been created for this key. Claimed before signing."""
        return bool(await self.r.set(keys.idem(idempotency_key), "pending", nx=True, ex=ttl))

    async def finish_order(self, idempotency_key: str, signature: str) -> None:
        await self.r.set(keys.idem(idempotency_key), signature)

    async def lookup_order(self, idempotency_key: str) -> str | None:
        return await self.r.get(keys.idem(idempotency_key))

    async def abandon_order(self, idempotency_key: str) -> None:
        """Only safe when we are certain nothing was broadcast."""
        await self.r.delete(keys.idem(idempotency_key))
