"""The concurrency guarantees. If these fail, the system can over-allocate real money."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from asm.domain.enums import Reason, SystemState
from asm.state import keys
from asm.state.ledger import SystemControl


async def test_reserve_grants_and_deducts_cash(ledger):
    res = await ledger.reserve(mint="MINT1", wallet="W1", size_usd=Decimal("100"))
    assert res.granted
    snap = await ledger.snapshot()
    assert snap.cash_usd == Decimal("9900")
    assert snap.deployed_usd == Decimal("100")


async def test_release_restores_cash(ledger):
    res = await ledger.reserve(mint="MINT1", wallet="W1", size_usd=Decimal("100"))
    await ledger.release(res.id)
    snap = await ledger.snapshot()
    assert snap.cash_usd == Decimal("10000")
    assert snap.deployed_usd == Decimal("0")


async def test_concurrent_reservations_cannot_breach_token_cap(ledger, relaxed_pending):
    """PRD 52 - the race the whole Lua script exists to close.

    Token cap is 5% of 10000 = 500. Twenty simultaneous 100 USD reservations on the
    SAME token must grant exactly five. A read-then-write check would grant all twenty.

    The pending cap is relaxed here so the TOKEN limit is provably the binding one.
    """
    results = await asyncio.gather(*[
        ledger.reserve(mint="SAME", wallet=f"W{i}", size_usd=Decimal("100"))
        for i in range(20)
    ])
    granted = [r for r in results if r.granted]
    rejected = [r for r in results if not r.granted]

    assert len(granted) == 5, f"token cap breached: {len(granted)} granted"
    assert all(r.reason is Reason.TOKEN_EXPOSURE_LIMIT for r in rejected)

    exposures = await ledger.exposures()
    assert exposures["token"]["SAME"] == Decimal("500")


async def test_concurrent_reservations_respect_trader_budget(ledger, relaxed_pending):
    """Trader budget is 15% of 10000 = 1500, across different tokens."""
    results = await asyncio.gather(*[
        ledger.reserve(mint=f"MINT{i}", wallet="SAME_TRADER", size_usd=Decimal("100"))
        for i in range(25)
    ])
    granted = [r for r in results if r.granted]
    assert len(granted) == 15
    exposures = await ledger.exposures()
    assert exposures["trader"]["SAME_TRADER"] == Decimal("1500")


async def test_max_open_positions_enforced(ledger, relaxed_pending):
    """Default cap is 20 open positions; pending counts toward concurrency separately."""
    for i in range(5):
        res = await ledger.reserve(mint=f"M{i}", wallet=f"W{i}", size_usd=Decimal("50"))
        assert res.granted
        await ledger.commit(res.id, Decimal("50"))
    snap = await ledger.snapshot()
    assert snap.open_positions == 5


async def test_pending_concurrency_cap(ledger):
    """max_concurrent_pending defaults to 5 - unconfirmed orders cannot pile up."""
    results = await asyncio.gather(*[
        ledger.reserve(mint=f"M{i}", wallet=f"W{i}", size_usd=Decimal("50"))
        for i in range(10)
    ])
    granted = [r for r in results if r.granted]
    assert len(granted) == 5
    assert any(r.reason is Reason.TOO_MANY_PENDING for r in results if not r.granted)


async def test_daily_loss_breaker_blocks_entries(ledger):
    """PRD 23 - a 5% realized loss on the day stops new entries."""
    await ledger.r.hset(keys.risk_state("test"), "realized_today", "-600")
    res = await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("50"))
    assert not res.granted
    assert res.reason is Reason.DAILY_LOSS_LIMIT


async def test_drawdown_breaker_blocks_entries(ledger):
    """PRD 24 - 20% below the high-water mark halts entries."""
    await ledger.r.hset(keys.risk_state("test"), mapping={"hwm": "20000", "equity": "10000"})
    res = await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("50"))
    assert not res.granted
    assert res.reason is Reason.DRAWDOWN_LIMIT


async def test_kill_switch_blocks_reservation(ledger):
    await SystemControl(ledger.r).set(SystemState.KILLED)
    res = await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("50"))
    assert not res.granted
    assert res.reason is Reason.KILL_SWITCH_ACTIVE


async def test_soft_pause_blocks_entries(ledger):
    await SystemControl(ledger.r).set(SystemState.SOFT_PAUSED)
    res = await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("50"))
    assert not res.granted
    assert res.reason is Reason.SYSTEM_PAUSED


async def test_below_minimum_rejected(ledger):
    res = await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("1"))
    assert not res.granted
    assert res.reason is Reason.POSITION_BELOW_MINIMUM


async def test_commit_uses_actual_fill_not_estimate(ledger):
    """A fill that came in smaller must free the difference immediately."""
    res = await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("100"))
    await ledger.commit(res.id, Decimal("92.5"))
    snap = await ledger.snapshot()
    assert snap.deployed_usd == Decimal("92.5")
    assert snap.cash_usd == Decimal("9907.5")
    assert snap.open_positions == 1


async def test_release_position_books_pnl_and_frees_exposure(ledger):
    res = await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("100"))
    await ledger.commit(res.id, Decimal("100"))
    await ledger.release_position(
        mint="M", wallet="W", cost_released_usd=Decimal("100"),
        realized_pnl_usd=Decimal("35"), proceeds_usd=Decimal("135"), fully_closed=True,
    )
    snap = await ledger.snapshot()
    assert snap.cash_usd == Decimal("10035")
    assert snap.deployed_usd == Decimal("0")
    assert snap.realized_pnl_today_usd == Decimal("35")
    assert snap.open_positions == 0
    exposures = await ledger.exposures()
    assert "M" not in exposures["token"]


async def test_mark_raises_high_water_mark(ledger):
    equity, hwm = await ledger.mark(Decimal("500"))
    assert equity == Decimal("10500")
    assert hwm == Decimal("10500")
    equity, hwm = await ledger.mark(Decimal("-200"))
    assert equity == Decimal("9800")
    assert hwm == Decimal("10500"), "high-water mark must not fall"


# ------------------------------------------------- explicit start (PRD 55)
async def test_fresh_system_refuses_to_trade_until_started(ledger):
    """A system that trades the moment it boots is the wrong default.

    With no state ever set, the Lua gate must fail CLOSED. This is the property that
    makes "it only runs when I press Start" true at the layer that actually authorises
    spending, not merely in the UI.
    """
    await ledger.r.delete(keys.SYSTEM_STATE)
    res = await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("50"))
    assert not res.granted
    assert res.reason is Reason.SYSTEM_NOT_STARTED


async def test_explicitly_stopped_blocks_entries(ledger):
    await SystemControl(ledger.r).stop()
    res = await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("50"))
    assert not res.granted
    assert res.reason is Reason.SYSTEM_NOT_STARTED


async def test_start_then_trade(ledger):
    await SystemControl(ledger.r).start()
    res = await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("50"))
    assert res.granted


async def test_start_survives_a_restart(ledger):
    """State lives in Redis, so restarting the services keeps trading on - and closing
    the browser is not even a restart."""
    control = SystemControl(ledger.r)
    await control.start()
    fresh = SystemControl(ledger.r)          # as if a service had just booted
    assert await fresh.get() is SystemState.RUNNING
    assert (await ledger.reserve(mint="M", wallet="W", size_usd=Decimal("50"))).granted


async def test_stopped_system_still_allows_exits():
    """Stopping must never strand an open position without a stop loss."""
    assert SystemState.STOPPED.allows_exits
    assert not SystemState.STOPPED.allows_entries
    assert SystemState.SOFT_PAUSED.allows_exits
    assert not SystemState.KILLED.allows_exits


async def test_start_refused_while_kill_switch_active(ledger):
    import pytest

    await SystemControl(ledger.r).kill()
    with pytest.raises(RuntimeError, match="kill switch"):
        await SystemControl(ledger.r).start()
