from __future__ import annotations

import os
from decimal import Decimal

import pytest

os.environ.setdefault("MODE", "paper")
# Never inherit a production Redis URL: this suite intentionally flushes its test DB.
os.environ["REDIS_URL"] = os.environ.get("ASM_TEST_REDIS_URL", "redis://localhost:6379/15")
if not os.environ["REDIS_URL"].rstrip("/").endswith("/15"):
    raise RuntimeError("ASM_TEST_REDIS_URL must use the isolated test database 15")


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
