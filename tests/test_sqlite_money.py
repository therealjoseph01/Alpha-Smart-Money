"""Money must survive the database exactly, on whichever backend is configured.

SQLite has no DECIMAL type. SQLAlchemy's Numeric round-trips through float there, so
balances are stored as exact TEXT instead — which brings its own trap, covered below.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from asm.config import settings
from asm.db import models as M
from asm.db import repo
from asm.db.base import Base
from asm.domain.enums import OrderStatus
from asm.domain.models import Execution


@pytest.fixture
async def db(tmp_path):
    url = settings.database_url
    if url.startswith("sqlite"):
        url = f"sqlite+aiosqlite:///{tmp_path / 'money.db'}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as s:
            yield s
    finally:
        await engine.dispose()


async def _position(db, **kw):
    row = M.PositionRow(
        token_mint="MINT", trader_wallet="W", mode="paper", status="open", decimals=6,
        amount_raw=Decimal(1_000_000), cost_basis_usd=Decimal("100"),
        realized_pnl_usd=Decimal(0), fees_usd=Decimal(0),
        entry_price=Decimal("100"), current_price=Decimal("100"),
        peak_price=Decimal("100"), tp_levels_hit=[], opened_at=datetime.now(UTC),
        **kw,
    )
    db.add(row)
    await db.flush()
    return row


async def test_float_error_never_reaches_the_database(db):
    """0.1 + 0.2 is 0.30000000000000004 as a float. It must not be."""
    row = await _position(db, )
    row.cost_basis_usd = Decimal("0.1") + Decimal("0.2")
    await db.flush()
    db.expunge_all()

    fresh = (await db.execute(
        select(M.PositionRow).where(M.PositionRow.id == row.id))).scalar_one()
    assert fresh.cost_basis_usd == Decimal("0.3")
    assert isinstance(fresh.cost_basis_usd, Decimal)


async def test_very_large_raw_amounts_stay_exact(db):
    """Token amounts are raw integer units and can exceed 64 bits."""
    huge = 123456789012345678901234567890
    row = await _position(db, )
    row.amount_raw = Decimal(huge)
    await db.flush()
    db.expunge_all()

    fresh = (await db.execute(
        select(M.PositionRow).where(M.PositionRow.id == row.id))).scalar_one()
    assert int(fresh.amount_raw) == huge


async def test_eighteen_decimal_prices_stay_exact(db):
    row = await _position(db, )
    row.entry_price = Decimal("0.000000000000000001")
    await db.flush()
    db.expunge_all()

    fresh = (await db.execute(
        select(M.PositionRow).where(M.PositionRow.id == row.id))).scalar_one()
    assert fresh.entry_price == Decimal("0.000000000000000001")


async def test_reduce_position_adds_pnl_rather_than_concatenating(db):
    """The regression this file exists for.

    Money is TEXT on SQLite, so `column + value` in SQL renders as `||` - string
    concatenation. 25 + 25 silently became "2525". All money arithmetic must therefore
    happen in Python, and this proves it does.
    """
    row = await _position(db, )
    ex = Execution(order_id=uuid.uuid4(), status=OrderStatus.CONFIRMED,
                   actual_price=Decimal("150"), signature="sig")

    for _ in range(2):
        await repo.reduce_position(
            db, row.id, ex=ex, amount_raw=250_000,
            proceeds_usd=Decimal("37.5"), realized_pnl=Decimal("25"),
            exit_reason="take_profit", fully_closed=False,
        )
    await db.flush()
    db.expunge_all()

    fresh = (await db.execute(
        select(M.PositionRow).where(M.PositionRow.id == row.id))).scalar_one()
    assert fresh.realized_pnl_usd == Decimal("50"), "25 + 25 must be 50, not '2525'"
    assert int(fresh.amount_raw) == 500_000
    assert fresh.cost_basis_usd == Decimal("75")


async def test_take_profit_ladder_survives_a_reload(db):
    """array_append() is PostgreSQL-only; the list is maintained in Python."""
    row = await _position(db, )
    ex = Execution(order_id=uuid.uuid4(), status=OrderStatus.CONFIRMED,
                   actual_price=Decimal("150"), signature="sig")

    await repo.reduce_position(
        db, row.id, ex=ex, amount_raw=250_000, proceeds_usd=Decimal("37.5"),
        realized_pnl=Decimal("25"), exit_reason="take_profit", fully_closed=False,
        ladder_index=0)
    await repo.reduce_position(
        db, row.id, ex=ex, amount_raw=250_000, proceeds_usd=Decimal("37.5"),
        realized_pnl=Decimal("25"), exit_reason="take_profit", fully_closed=False,
        ladder_index=1)
    await db.flush()
    db.expunge_all()

    fresh = (await db.execute(
        select(M.PositionRow).where(M.PositionRow.id == row.id))).scalar_one()
    assert fresh.tp_levels_hit == [0, 1]


async def test_uuid_round_trips_as_uuid(db):
    row = await _position(db, )
    assert isinstance(row.id, uuid.UUID)
    db.expunge_all()
    fresh = (await db.execute(
        select(M.PositionRow).where(M.PositionRow.id == row.id))).scalar_one()
    assert isinstance(fresh.id, uuid.UUID)
    assert fresh.id == row.id


async def test_negative_and_tiny_values_survive(db):
    row = await _position(db, )
    row.realized_pnl_usd = Decimal("-0.000000000001")
    await db.flush()
    db.expunge_all()
    fresh = (await db.execute(
        select(M.PositionRow).where(M.PositionRow.id == row.id))).scalar_one()
    assert fresh.realized_pnl_usd == Decimal("-0.000000000001")
