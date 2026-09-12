"""PRD 43 - alert delivery.

An alert sitting unread in a database table has not alerted anyone. This delivers to
whatever channels are configured, and always writes the row regardless, so the audit
trail survives a broken webhook.

Channels are best-effort and never raise into the caller: a Telegram outage must not
stop a trade from exiting.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

import httpx

from asm.config import settings
from asm.db import models as M
from asm.db.session import session_scope
from asm.logging import get_logger

log = get_logger(__name__)


class Level(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def emoji(self) -> str:
        return {"info": "•", "warning": "⚠", "critical": "🚨"}[self.value]

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "critical": 2}[self.value]


class Category(StrEnum):
    TRADE = "trade"
    RISK = "risk"
    SYSTEM = "system"
    TREASURY = "treasury"
    TRADER = "trader"
    REPORT = "report"


class Channel:
    name = "base"

    async def send(self, level: Level, title: str, body: str,
                   detail: dict[str, Any]) -> bool:
        raise NotImplementedError


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    async def send(self, level, title, body, detail) -> bool:
        text = f"{level.emoji} *{_escape(title)}*\n{_escape(body)}" if body else \
               f"{level.emoji} *{_escape(title)}*"
        if detail:
            lines = "\n".join(f"{k}: {v}" for k, v in list(detail.items())[:12])
            text += f"\n```\n{lines}\n```"
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text[:4000],
                      "parse_mode": "Markdown", "disable_web_page_preview": True},
            )
            return r.status_code == 200


class DiscordChannel(Channel):
    name = "discord"
    COLORS: ClassVar[dict[str, int]] = {
        "info": 0x3498DB, "warning": 0xF39C12, "critical": 0xE74C3C,
    }

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    async def send(self, level, title, body, detail) -> bool:
        embed: dict[str, Any] = {
            "title": f"{level.emoji} {title}"[:256],
            "description": body[:4000] or None,
            "color": self.COLORS[level.value],
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if detail:
            embed["fields"] = [
                {"name": str(k)[:256], "value": str(v)[:1024], "inline": True}
                for k, v in list(detail.items())[:10]
            ]
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(self.webhook_url, json={"embeds": [embed]})
            return r.status_code in (200, 204)


class WebhookChannel(Channel):
    name = "webhook"

    def __init__(self, url: str):
        self.url = url

    async def send(self, level, title, body, detail) -> bool:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(self.url, json={
                "level": level.value, "title": title, "body": body,
                "detail": detail, "mode": settings.mode.value,
                "at": datetime.now(UTC).isoformat(),
            })
            return 200 <= r.status_code < 300


def _escape(text: str) -> str:
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


class Alerter:
    """Fan-out with a severity floor and per-key rate limiting."""

    def __init__(self, channels: list[Channel] | None = None,
                 min_level: Level | None = None):
        self.channels = channels if channels is not None else _channels_from_settings()
        self.min_level = min_level or Level(settings.alert_min_level)

    async def send(self, level: Level, category: Category, title: str,
                   body: str = "", detail: dict[str, Any] | None = None,
                   dedupe_key: str | None = None,
                   dedupe_seconds: int = 300) -> bool:
        detail = detail or {}

        # Always persist, even if every channel is down (PRD 58).
        async with session_scope() as s:
            s.add(M.Alert(level=level.value, category=category.value, title=title[:256],
                          body=body, detail=detail, created_at=datetime.now(UTC)))

        if level.rank < self.min_level.rank or not self.channels:
            return False

        if dedupe_key and not await self._claim(dedupe_key, dedupe_seconds):
            log.debug("alert_suppressed_duplicate", key=dedupe_key)
            return False

        results = await asyncio.gather(
            *(self._deliver(c, level, title, body, detail) for c in self.channels),
            return_exceptions=True,
        )
        return any(r is True for r in results)

    async def _deliver(self, channel: Channel, level, title, body, detail) -> bool:
        try:
            ok = await channel.send(level, title, body, detail)
            if not ok:
                log.warning("alert_channel_rejected", channel=channel.name)
            return ok
        except Exception as exc:
            # Never let a notification failure propagate into trading logic.
            log.warning("alert_channel_failed", channel=channel.name, error=repr(exc))
            return False

    async def _claim(self, key: str, ttl: int) -> bool:
        from asm.state.client import get_redis

        return bool(await get_redis().set(f"asm:alert:{key}", "1", nx=True, ex=ttl))

    # ------------------------------------------------------- conveniences
    async def info(self, category: Category, title: str, **kw):
        return await self.send(Level.INFO, category, title, **kw)

    async def warning(self, category: Category, title: str, **kw):
        return await self.send(Level.WARNING, category, title, **kw)

    async def critical(self, category: Category, title: str, **kw):
        return await self.send(Level.CRITICAL, category, title, **kw)


def _channels_from_settings() -> list[Channel]:
    channels: list[Channel] = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        channels.append(TelegramChannel(settings.telegram_bot_token,
                                        settings.telegram_chat_id))
    if settings.discord_webhook_url:
        channels.append(DiscordChannel(settings.discord_webhook_url))
    if settings.alert_webhook_url:
        channels.append(WebhookChannel(settings.alert_webhook_url))
    return channels


_alerter: Alerter | None = None


def alerter() -> Alerter:
    global _alerter
    if _alerter is None:
        _alerter = Alerter()
    return _alerter
