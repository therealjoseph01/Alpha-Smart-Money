"""PRD 30.4, 31, 32, 33 - capital actually leaving risk, not just being relabelled."""
from __future__ import annotations

from decimal import Decimal

import pytest

from asm.engine.harvest import maybe_harvest
from asm.engine.treasury import (
    CompoundingMode,
    ProfitLock,
    Treasury,
    TreasuryTransferError,
    allocate_buckets,
    compound_split,
)


# ---------------------------------------------------------------- buckets
def test_locked_and_gas_come_off_the_top():
    a = allocate_buckets(Decimal("10000"), locked_usd=Decimal("2000"),
                         gas_usd=Decimal("100"), reserve_pct=Decimal("20"))
    assert a.locked_usd == Decimal("2000")
    assert a.gas_usd == Decimal("100")
    assert a.active_usd + a.reserve_usd == Decimal("7900")
    assert a.reserve_usd == Decimal("1580")
    assert a.total_usd == Decimal("10000")


def test_buckets_never_go_negative():
    a = allocate_buckets(Decimal("100"), locked_usd=Decimal("500"))
    assert a.active_usd == Decimal(0)
    assert a.reserve_usd == Decimal(0)


# ------------------------------------------------------------ compounding
@pytest.mark.parametrize(
    ("mode", "expected_reinvest"),
    [
        (CompoundingMode.CONSERVATIVE, Decimal("250")),
        (CompoundingMode.BALANCED, Decimal("500")),
        (CompoundingMode.GROWTH, Decimal("850")),
    ],
)
def test_compounding_modes_split_profit(mode, expected_reinvest):
    reinvest, aside = compound_split(Decimal("1000"), mode)
    assert reinvest == expected_reinvest
    assert reinvest + aside == Decimal("1000")


def test_losses_are_never_compounded():
    assert compound_split(Decimal("-500"), CompoundingMode.GROWTH) == (Decimal(0), Decimal(0))


# ----------------------------------------------------------- profit lock
async def test_profit_lock_ratchets_up_only(ledger):
    lock = ProfitLock(ledger)
    assert await lock.get() == Decimal(0)
    await lock.set(Decimal("1000"))
    assert await lock.get() == Decimal("1000")

    await lock.set(Decimal("500"))
    assert await lock.get() == Decimal("1000"), "the lock must never fall on its own"

    await lock.set(Decimal("1500"))
    assert await lock.get() == Decimal("1500")


async def test_unlock_is_explicit_and_audited(ledger):
    lock = ProfitLock(ledger)
    await lock.set(Decimal("2000"))
    await lock.unlock(Decimal("500"), actor="operator")
    assert await lock.get() == Decimal("500")


async def test_tradeable_equity_excludes_the_lock(ledger):
    lock = ProfitLock(ledger)
    await lock.set(Decimal("4000"))
    assert await lock.tradeable_equity() == Decimal("6000")


async def test_ratchet_locks_half_of_gains_above_the_lock(ledger):
    lock = ProfitLock(ledger)
    await lock.set(Decimal("8000"))
    new = await lock.ratchet(lock_pct=Decimal("50"))
    assert new == Decimal("9000")   # 8000 + 50% of the 2000 above it


# -------------------------------------------------------------- treasury
async def test_transfer_refuses_without_a_destination(ledger, monkeypatch):
    """Harvesting into nowhere is worse than not harvesting."""
    from asm.config import settings

    monkeypatch.setattr(settings, "treasury_wallet_pubkey", "")
    with pytest.raises(TreasuryTransferError, match="not configured"):
        await Treasury(ledger).transfer(Decimal("100"))


async def test_paper_transfer_is_recorded_not_broadcast(ledger, monkeypatch):
    import asm.engine.treasury as T
    from asm.config import settings

    monkeypatch.setattr(settings, "treasury_wallet_pubkey", "T" * 43)

    async def fake_sol_price():
        return Decimal("150")

    import asm.engine.market as market
    monkeypatch.setattr(market, "sol_price_usd", fake_sol_price)

    saved = []

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def add(self, row):
            saved.append(row)

    monkeypatch.setattr(T, "session_scope", lambda: FakeSession())

    row = await Treasury(ledger).transfer(Decimal("300"))
    assert row.status == "simulated"
    assert row.amount_usd == Decimal("300")
    assert row.amount_raw == Decimal(2_000_000_000)   # $300 / $150 = 2 SOL
    assert saved


async def test_failed_transfer_does_not_advance_the_harvest_baseline(ledger, monkeypatch):
    """The transfer IS the protection. If it fails, the system must not believe the
    profit is safe - otherwise the same dollars are never harvested again."""
    import asm.engine.harvest as H
    from asm.config import settings

    monkeypatch.setattr(settings, "treasury_wallet_pubkey", "T" * 43)
    await ledger.bootstrap(Decimal("12500"), force=True)
    await ledger.r.set(f"asm:{ledger.mode}:harvest:baseline", "10000")

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def add(self, row):
            row.id = "fake-id"
        async def flush(self):
            return None
        async def execute(self, *a, **kw):
            return None

    monkeypatch.setattr(H, "session_scope", lambda: FakeSession())

    class BrokenTreasury:
        def __init__(self, *a, **kw):
            pass
        async def transfer(self, *a, **kw):
            raise H.TreasuryTransferError("rpc down")

    monkeypatch.setattr(H, "Treasury", BrokenTreasury)

    decision = await maybe_harvest(ledger)
    assert not decision.should_harvest
    assert "transfer_failed" in decision.reason

    baseline = await ledger.r.get(f"asm:{ledger.mode}:harvest:baseline")
    assert Decimal(baseline) == Decimal("10000"), "baseline advanced despite a failed transfer"
    snap = await ledger.snapshot()
    assert snap.harvested_total_usd == Decimal(0)
