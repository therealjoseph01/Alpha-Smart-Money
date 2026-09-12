from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from asm import runtime_config
from asm.config import settings
from asm.db.session import close_engine
from asm.logging import get_logger, setup_logging
from asm.services.api.routes import (
    backtests,
    config,
    portfolio,
    research,
    setup,
    system,
    traders,
    trades,
)
from asm.state.client import close_redis
from asm.state.ledger import RiskLedger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await runtime_config.refresh(force=True)
    await RiskLedger().bootstrap(settings.starting_capital_usd)
    log.info("api_started", mode=settings.mode.value)
    yield
    await close_redis()
    await close_engine()


app = FastAPI(
    title="Adaptive Smart-Money Copy Trading",
    version="0.1.0",
    description=(
        "Copy-trading intelligence for Solana. Read endpoints are open; every mutation "
        "requires a bearer token and is written to the audit log."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (traders, trades, portfolio, system, backtests, research, setup,
               config):
    app.include_router(module.router)


STATIC = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
async def dashboard():
    """PRD 42 - the operator control center."""
    return FileResponse(STATIC / "index.html")


@app.get("/api")
async def root():
    return {
        "service": "asm",
        "mode": settings.mode.value,
        "docs": "/docs",
        "dashboard": "/",
        "warning": "speculative trading system; no returns are guaranteed",
    }
