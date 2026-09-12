from __future__ import annotations

from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from asm.config import settings
from asm.db.session import get_session
from asm.state.ledger import RiskLedger, SystemControl

DB = Annotated[AsyncSession, Depends(get_session)]


async def require_token(authorization: str = Header(default="")) -> str:
    """PRD 49 - sensitive mutations require strong auth. Swap for real OIDC in prod."""
    token = authorization.removeprefix("Bearer ").strip()
    if (not authorization.startswith("Bearer ") or not token
            or not settings.api_token or settings.api_token == "dev-token-change-me"
            or not compare_digest(token.encode(), settings.api_token.encode())):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    return token


Auth = Annotated[str, Depends(require_token)]


def ledger() -> RiskLedger:
    return RiskLedger()


def control() -> SystemControl:
    return SystemControl()


Ledger = Annotated[RiskLedger, Depends(ledger)]
Control = Annotated[SystemControl, Depends(control)]
