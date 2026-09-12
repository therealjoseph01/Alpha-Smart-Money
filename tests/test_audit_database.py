"""Repository checks in a transaction-local Postgres schema, rolled back in full."""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from asm.config import settings
from asm.db import models as M
from asm.db import repo
from asm.domain.enums import Action, OrderStatus
from asm.domain.models import ApprovedOrder, Execution
from asm.domain.money import SOL_MINT
from tests import factories as F


@pytest.fixture
async def db():
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            schema = 'asm_audit_' + uuid4().hex
            try:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                def migrate(sync_connection):
                    import importlib.util
                    from pathlib import Path

                    from alembic.migration import MigrationContext
                    from alembic.operations import Operations

                    migration_path = (Path(__file__).parents[1] / 'alembic' / 'versions' /
                                      '99f60d63a039_initial_schema.py')
                    spec = importlib.util.spec_from_file_location('audit_migration', migration_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    with Operations.context(MigrationContext.configure(sync_connection)):
                        module.upgrade()

                await connection.run_sync(migrate)
                async with AsyncSession(bind=connection, expire_on_commit=False,
                                        join_transaction_mode='create_savepoint') as session:
                    yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def test_duplicate_source_returns_persisted_identity(db):
    a = F.source_trade()
    b = F.source_trade()
    assert a.id != b.id
    assert await repo.save_source_trade(db, a) == a.id
    assert await repo.save_source_trade(db, b) == a.id


async def test_take_profit_level_survives_database_reload(db):
    c = F.candidate()
    order = ApprovedOrder(idempotency_key='db-audit', decision_id=c.id, candidate=c,
                          action=Action.BUY, input_mint=SOL_MINT, output_mint=F.MINT,
                          in_amount_raw=100, size_usd=Decimal(100), slippage_bps=300)
    await repo.save_order(db, order)
    ex = Execution(order_id=order.id, status=OrderStatus.CONFIRMED,
                   actual_price=Decimal(1), out_amount_raw=100_000_000,
                   value_usd=Decimal(100))
    await repo.save_execution(db, ex)
    p = await repo.open_position(db, token_mint=F.MINT, trader_wallet=F.WALLET,
                                 ex=ex, decimals=6)
    await db.flush()
    await repo.reduce_position(db, p.id, ex=ex, amount_raw=25_000_000,
                               proceeds_usd=Decimal(50), realized_pnl=Decimal(25),
                               exit_reason='take_profit', fully_closed=False, ladder_index=0)
    await db.flush()
    await db.refresh(p)
    assert p.amount_raw == 75_000_000
    assert p.cost_basis_usd == Decimal(75)
    assert repo.position_to_domain(p).tp_levels_hit == [0]


async def test_zero_trader_allocation_is_preserved(db):
    row = await repo.upsert_trader(db, F.WALLET)
    row.size_multiplier = Decimal(0)
    await db.flush()
    assert repo.trader_to_domain(row).size_multiplier == 0


async def test_retry_order_uses_existing_database_identity(db):
    c = F.candidate()
    def new_order():
        return ApprovedOrder(idempotency_key='same-order', decision_id=c.id, candidate=c,
                             action=Action.BUY, input_mint=SOL_MINT, output_mint=F.MINT,
                             in_amount_raw=100, size_usd=Decimal(100), slippage_bps=300)
    a, b = new_order(), new_order()
    await repo.save_order(db, a)
    await repo.save_order(db, b)
    assert b.id == a.id
    await repo.save_execution(db, Execution(order_id=b.id, status=OrderStatus.FAILED))
    await db.flush()
    assert len((await db.execute(select(M.Order))).scalars().all()) == 1


async def test_dashboard_read_endpoints_against_postgres(db, ledger, monkeypatch):
    from contextlib import asynccontextmanager

    import httpx

    import asm.services.tasks.research as research
    from asm.services.api.deps import get_session
    from asm.services.api.deps import ledger as ledger_dependency
    from asm.services.api.main import app

    async def database():
        yield db

    @asynccontextmanager
    async def scope():
        yield db

    monkeypatch.setattr(research, 'session_scope', scope)
    app.dependency_overrides[get_session] = database
    app.dependency_overrides[ledger_dependency] = lambda: ledger
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='http://test') as client:
            for endpoint in ['/', '/api', '/health', '/risk', '/risk/events', '/portfolio',
                             '/portfolio/performance', '/positions', '/traders', '/trades',
                             '/trades/stats/rejections', '/trades/stats/latency', '/harvesting',
                             '/events', '/alerts', '/backtests', '/research', '/research/graph']:
                response = await client.get(endpoint)
                assert response.status_code == 200, (endpoint, response.text)
            assert (await client.post('/system/resume')).status_code == 401
    finally:
        app.dependency_overrides.clear()
