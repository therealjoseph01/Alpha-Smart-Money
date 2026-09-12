from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

# Never inherit a production Redis URL: this suite intentionally flushes its test DB.
os.environ["REDIS_URL"] = os.environ.get("ASM_TEST_REDIS_URL", "redis://localhost:6379/15")
if not os.environ["REDIS_URL"].rstrip("/").endswith("/15"):
    raise RuntimeError("ASM_TEST_REDIS_URL must use the isolated test database 15")

# Never the real database either. This has to happen BEFORE anything imports
# asm.config, because the settings object and the SQLAlchemy engine are both
# module-level singletons built from the environment at import time.
#
# It matters more than test hygiene: tests/test_promotion.py deliberately advances
# the mode to live, and promotion persists the mode. Run against data/asm.db, a test
# run leaves the real system believing it is in live mode.
_TEST_DB = Path(tempfile.gettempdir()) / f"asm-test-{os.getpid()}.db"
_TEST_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Build the schema once per run, and take the file with us on the way out."""
    import asyncio

    from asm.db import models as M  # noqa: F401  - registers the tables
    from asm.db.base import Base
    from asm.db.session import engine

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    yield
    _TEST_DB.unlink(missing_ok=True)


@pytest.fixture
async def redis():
    """Fresh client per test.

    pytest-asyncio gives each test its own event loop, and a redis connection pool is
    bound to the loop that created it. Reusing the module singleton across tests raises
    "Event loop is closed", so the singleton is reset around every test.
    """
    import asm.state.client as client

    if client._pool is not None:
        await client.close_redis()

    r = client.get_redis()
    await r.flushdb()
    try:
        yield r
    finally:
        await r.flushdb()
        await client.close_redis()
        # The SQLAlchemy engine's pool is bound to the loop that created it, exactly
        # like the redis pool. Tests that touch the database across loops otherwise
        # fail with "Event loop is closed".
        from asm.db import session as db_session

        await db_session.engine.dispose()


@pytest.fixture
async def ledger(redis):
    """A bootstrapped ledger on a STARTED system.

    The system now defaults to stopped, so tests that exercise risk logic must start it
    explicitly - exactly as a user does. Tests that check the start gate itself clear
    the state again.
    """
    from asm.state.ledger import RiskLedger, SystemControl

    lg = RiskLedger(redis, mode="test")
    await lg.bootstrap(Decimal("10000"), force=True)
    await SystemControl(redis).start(actor="test")
    return lg


@pytest.fixture
def relaxed_pending():
    """Raise the pending-order cap so a test can isolate a different limit."""
    from asm.config import settings

    original = settings.risk.max_concurrent_pending
    settings.risk.max_concurrent_pending = 1000
    yield
    settings.risk.max_concurrent_pending = original
