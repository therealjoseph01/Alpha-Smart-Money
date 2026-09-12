"""Server-issued sessions for the dashboard (PRD 49/54).

Why a session cookie rather than the token in localStorage:

  * localStorage is readable by any script on the page, so one XSS and the API token
    is gone - and that token never expires.
  * A cookie set from JavaScript is no better; it is readable by the same script.
  * An httpOnly cookie cannot be read by JavaScript at all. The browser holds an opaque
    session id, the real token never reaches the page, and the session expires on its
    own.

The session id is random and meaningless by itself: it is a key into Redis, so
revoking a session is a DEL, and every session dies when Redis is flushed.

The bearer token still works for the CLI and scripts. Only the browser uses cookies.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import Request, Response

from asm.config import settings
from asm.logging import get_logger
from asm.state.client import get_redis

log = get_logger(__name__)

COOKIE_NAME = "asm_session"
PREFIX = "asm:session:"


def _key(sid: str) -> str:
    return f"{PREFIX}{sid}"


async def create(request: Request, response: Response) -> str:
    """Issue a session and set it as an httpOnly cookie."""
    sid = secrets.token_urlsafe(32)
    ttl = settings.session_ttl_seconds
    client = request.client.host if request.client else "unknown"

    await get_redis().hset(_key(sid), mapping={
        "created_at": datetime.now(UTC).isoformat(),
        "ip": client,
        "ua": (request.headers.get("user-agent") or "")[:200],
    })
    await get_redis().expire(_key(sid), ttl)

    response.set_cookie(
        COOKIE_NAME, sid,
        max_age=ttl,
        httponly=True,                       # unreadable from JavaScript
        samesite="lax",                      # blocks cross-site form CSRF
        # Secure would break plain-http localhost, which is the normal case here.
        secure=settings.session_cookie_secure,
        path="/",
    )
    log.info("session_created", ip=client, ttl_seconds=ttl)
    return sid


async def verify(request: Request) -> bool:
    """True if the request carries a live session. Sliding expiry on each use."""
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return False
    r = get_redis()
    if not await r.exists(_key(sid)):
        return False
    # Active use extends the session; walking away still expires it.
    await r.expire(_key(sid), settings.session_ttl_seconds)
    return True


async def destroy(request: Request, response: Response) -> None:
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        await get_redis().delete(_key(sid))
    response.delete_cookie(COOKIE_NAME, path="/")


async def destroy_all() -> int:
    """Revoke every session - used when the password changes or on demand."""
    r = get_redis()
    n = 0
    async for key in r.scan_iter(match=f"{PREFIX}*"):
        await r.delete(key)
        n += 1
    log.warning("all_sessions_revoked", count=n)
    return n


async def remaining_seconds(request: Request) -> int | None:
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return None
    ttl = await get_redis().ttl(_key(sid))
    return ttl if ttl and ttl > 0 else None
