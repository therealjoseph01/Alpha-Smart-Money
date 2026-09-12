"""PRD 55 - the kill switch must actually unwind, not just stop new risk."""
from __future__ import annotations

from decimal import Decimal

from asm.domain.enums import ExitReason, OrderStatus
from asm.services.tasks.emergency import emergency_exit_all


class FakeExecutor:
    mode = "paper"

    def __init__(self, fail_mints: set[str] | None = None):
        self.fail_mints = fail_mints or set()
        self.orders = []

    async def execute(self, order):
        from asm.execution.base import new_execution, stamp

        self.orders.append(order)
        ex = new_execution(order, self.mode)
        if order.input_mint in self.fail_mints:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = "no_route"
            return ex
        ex.status = OrderStatus.CONFIRMED
        ex.actual_price = order.candidate.market.price_usd
        ex.out_amount_raw = order.in_amount_raw
        ex.value_usd = order.size_usd
        ex.signature = "fake"
        stamp(ex, "confirmed_at")
        return ex


async def test_emergency_exit_is_a_registered_arq_function():
    """It is enqueued by POST /system/emergency-stop; if unregistered it silently no-ops."""
    from asm.services.worker import WorkerSettings

    assert emergency_exit_all in WorkerSettings.functions


async def test_emergency_exit_with_nothing_open_is_a_noop(redis, monkeypatch):
    import asm.services.tasks.emergency as E

    async def no_positions(s, mode=None):
        return []

    monkeypatch.setattr(E.repo, "open_positions", no_positions)
    result = await emergency_exit_all({})
    assert result == {"closed": 0, "failed": 0, "positions": 0}


async def test_emergency_exit_uses_wide_slippage_and_emergency_reason(redis, monkeypatch):
    """Being out matters more than the last few percent of price."""
    import asm.services.tasks.emergency as E
    from tests import factories as F

    async def one_position(s, mode=None):
        return [_row()]

    async def market(mint, decimals=9):
        return F.market()

    async def noop(*a, **kw):
        return None

    executor = FakeExecutor()
    monkeypatch.setattr(E.repo, "open_positions", one_position)
    monkeypatch.setattr(E, "get_market_state", market)
    monkeypatch.setattr(E, "get_executor", lambda: executor)
    monkeypatch.setattr(E.repo, "save_order", noop)
    monkeypatch.setattr(E.repo, "save_execution", noop)
    monkeypatch.setattr(E.repo, "reduce_position", noop)
    monkeypatch.setattr(E.repo, "log_risk_event", noop)
    monkeypatch.setattr(E.repo, "add_alert", noop)

    result = await emergency_exit_all({}, reason="test")
    assert result["closed"] == 1
    assert result["failed"] == 0

    order = executor.orders[0]
    assert order.exit_reason is ExitReason.EMERGENCY
    assert order.slippage_bps == E.EMERGENCY_SLIPPAGE_BPS
    assert order.slippage_bps > 1000


async def test_one_unroutable_position_does_not_strand_the_rest(redis, monkeypatch):
    """An illiquid bag that cannot be sold must not abort the whole liquidation."""
    import asm.services.tasks.emergency as E
    from tests import factories as F

    stuck = "STUCKSTUCKSTUCKSTUCKSTUCKSTUCKSTUCKSTUCKSTU"

    async def three(s, mode=None):
        return [_row(mint=F.MINT), _row(mint=stuck), _row(mint="C" * 43)]

    async def market(mint, decimals=9):
        return F.market(mint=mint)

    async def noop(*a, **kw):
        return None

    executor = FakeExecutor(fail_mints={stuck})
    monkeypatch.setattr(E.repo, "open_positions", three)
    monkeypatch.setattr(E, "get_market_state", market)
    monkeypatch.setattr(E, "get_executor", lambda: executor)
    for fn in ("save_order", "save_execution", "reduce_position",
               "log_risk_event", "add_alert"):
        monkeypatch.setattr(E.repo, fn, noop)

    result = await emergency_exit_all({})
    assert result["closed"] == 2
    assert result["failed"] == 1
    assert result["errors"][0]["mint"] == stuck


def _row(mint=None):
    """A PositionRow-shaped stand-in that repo.position_to_domain can convert."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from asm.db import models as M
    from tests import factories as F

    r = M.PositionRow(
        id=uuid4(), token_mint=mint or F.MINT, symbol="TEST", decimals=6,
        trader_wallet=F.WALLET, mode="paper", status="open",
        amount_raw=Decimal(1_000_000), cost_basis_usd=Decimal("100"),
        realized_pnl_usd=Decimal(0), fees_usd=Decimal(0),
        entry_price=Decimal("100"), current_price=Decimal("100"),
        peak_price=Decimal("100"), trailing_armed=False, tp_levels_hit=[],
        opened_at=datetime.now(UTC), closed_at=None, source_trade_id=None,
    )
    return r
