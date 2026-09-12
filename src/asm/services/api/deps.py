from __future__ import annotations

from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from asm.config import settings
from asm.db.session import get_session
from asm.state.ledger import RiskLedger, SystemControl

DB = Annotated[AsyncSession, Depends(get_session)]


def token_is_valid(token: str) -> bool:
    """Constant-time comparison, and a refusal to authenticate with the placeholder.

    A default password everyone knows is the same as no password, and this system can
    move real money - so it fails closed rather than pretending to be protected.
    """
    if not token or not settings.api_token:
        return False
    if settings.api_token == "dev-token-change-me":
        return False
    return compare_digest(token.encode(), settings.api_token.encode())


async def require_token(request: Request,
                        authorization: str = Header(default="")) -> str:
    """PRD 49 - two ways in, for two different callers.

      * the browser presents an httpOnly session cookie, so the token itself never
        reaches JavaScript and the session expires on its own;
      * the CLI and scripts present a bearer token, which is what they have.
    """
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token_is_valid(token):
            return token
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

    from asm.services.api import session

    if await session.verify(request):
        return "session"

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")


Auth = Annotated[str, Depends(require_token)]


def ledger() -> RiskLedger:
    return RiskLedger()


def control() -> SystemControl:
    return SystemControl()


Ledger = Annotated[RiskLedger, Depends(ledger)]
Control = Annotated[SystemControl, Depends(control)]
