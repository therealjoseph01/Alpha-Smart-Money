"""PRD 50 - internal event bus on Redis Streams (durable, replayable, consumer groups)."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import orjson
from redis.asyncio import Redis

from asm.domain.enums import EventType
from asm.logging import get_logger
from asm.state import keys
from asm.state.client import get_redis

log = get_logger(__name__)
MAXLEN = 100_000


def _default(o: Any):
    return str(o)


class EventBus:
    def __init__(self, redis: Redis | None = None, stream: str = keys.STREAM_EVENTS):
        self.r = redis or get_redis()
        self.stream = stream

    async def publish(
        self, event: EventType, payload: dict[str, Any], aggregate_id: str | None = None
    ) -> str:
        return await self.r.xadd(
            self.stream,
            {
                "id": uuid.uuid4().hex,
                "type": event.value,
                "aggregate_id": aggregate_id or "",
                "occurred_at": datetime.now(UTC).isoformat(),
                "payload": orjson.dumps(payload, default=_default).decode(),
            },
            maxlen=MAXLEN,
            approximate=True,
        )

    async def ensure_group(self, group: str) -> None:
        try:
            await self.r.xgroup_create(self.stream, group, id="$", mkstream=True)
        except Exception as exc:  # BUSYGROUP
            if "BUSYGROUP" not in str(exc):
                raise

    async def consume(
        self, group: str, consumer: str, count: int = 32, block_ms: int = 1000
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        await self.ensure_group(group)
        while True:
            resp = await self.r.xreadgroup(
                group, consumer, {self.stream: ">"}, count=count, block=block_ms
            )
            if not resp:
                continue
            for _stream, msgs in resp:
                for msg_id, fields in msgs:
                    if fields.get("payload"):
                        fields["payload"] = orjson.loads(fields["payload"])
                    yield msg_id, fields

    async def ack(self, group: str, msg_id: str) -> None:
        await self.r.xack(self.stream, group, msg_id)

    async def tail(self, count: int = 50) -> list[dict[str, Any]]:
        msgs = await self.r.xrevrange(self.stream, count=count)
        out = []
        for msg_id, fields in msgs:
            if fields.get("payload"):
                fields["payload"] = orjson.loads(fields["payload"])
            out.append({"stream_id": msg_id, **fields})
        return out
