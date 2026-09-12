"""PRD 28/29 - exits are the half of the system that protects capital."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from asm.config import settings
from asm.domain.enums import ExitReason, PositionStatus, TraderStatus
from asm.domain.models import Position
from asm.engine.exits import evaluate_exits, realized_pnl, update_marks
from tests import factories as F

CFG = settings.risk


def position(**kw) -> Position:
    defaults = dict(
        token_mint=F.MINT, trader_wallet=F.WALLET, status=PositionStatus.OPEN,
        decimals=6, amount_raw=1_000_000, cost_basis_usd=Decimal("100"),
        entry_price=Decimal("100"), current_price=Decimal("100"),
        peak_price=Decimal("100"), opened_at=datetime.now(UTC),
    )
    return Position(**(defaults | kw))


def test_stop_loss_fires_first():
    p = position()
    m = F.market(price_usd=Decimal("70"))   # -30%, below the -25% stop
    intent = evaluate_exits(p, m)
    assert intent.reason is ExitReason.STOP_LOSS
    assert intent.is_full
    assert intent.urgent


def test_stop_loss_beats_take_profit_when_both_would_apply():
    """Ordering matters: capital preservation outranks profit taking."""
    p = position(tp_levels_hit=[])
    m = F.market(price_usd=Decimal("60"))
    assert evaluate_exits(p, m).reason is ExitReason.STOP_LOSS


def test_take_profit_ladder_takes_partial():
    p = position()
    m = F.market(price_usd=Decimal("160"))  # +60%, first rung is +50%
    intent = evaluate_exits(p, m)
    assert intent.reason is ExitReason.TAKE_PROFIT
    assert intent.fraction == Decimal("0.25")
    assert not intent.is_full


def test_take_profit_rung_not_repeated():
    p = position(tp_levels_hit=[0])
    m = F.market(price_usd=Decimal("160"))
    intent = evaluate_exits(p, m)
    assert intent is None or intent.detail.get("ladder_index") != 0


def test_second_rung_fires_at_higher_gain():
    p = position(tp_levels_hit=[0])
    m = F.market(price_usd=Decimal("210"))  # +110%, second rung is +100%
    intent = evaluate_exits(p, m)
    assert intent.reason is ExitReason.TAKE_PROFIT
    assert intent.detail["ladder_index"] == 1


def test_trailing_stop_requires_arming():
    """A 20% drop from peak must NOT exit if the position never ran up first."""
    p = position(peak_price=Decimal("110"), trailing_armed=False)
    m = F.market(price_usd=Decimal("88"))  # -20% from peak, but only +10% peak gain
    intent = evaluate_exits(p, m)
    assert intent is None or intent.reason is not ExitReason.TRAILING_STOP


def test_trailing_stop_fires_once_armed():
    p = position(peak_price=Decimal("200"), trailing_armed=True)
    m = F.market(price_usd=Decimal("150"))  # -25% from peak, still +50% overall
    intent = evaluate_exits(p, m)
    assert intent.reason is ExitReason.TRAILING_STOP
    assert intent.is_full


def test_trader_degradation_exits_position():
    """PRD 28.6 - we no longer trust the thesis that opened this position."""
    p = position()
    m = F.market(price_usd=Decimal("105"))
    t = F.trader(status=TraderStatus.DEGRADED)
    assert evaluate_exits(p, m, t).reason is ExitReason.TRADER_DEGRADED


def test_liquidity_deterioration_exits_urgently():
    p = position()
    m = F.market(price_usd=Decimal("100"))
    intent = evaluate_exits(p, m, exit_liquidity_usd=Decimal("50"))
    assert intent.reason is ExitReason.LIQUIDITY_DETERIORATION
    assert intent.urgent


def test_no_route_out_is_worst_case_not_unknown():
    p = position()
    m = F.market(price_usd=Decimal("100"))
    assert evaluate_exits(p, m, exit_liquidity_usd=Decimal("0")).urgent


def test_mirror_exit_scales_to_source_fraction():
    p = position()
    m = F.market(price_usd=Decimal("110"))
    intent = evaluate_exits(p, m, source_sold=True, source_sold_fraction=Decimal("0.5"))
    assert intent.reason is ExitReason.MIRROR
    assert intent.fraction == Decimal("0.5")


def test_time_exit_after_max_hold():
    p = position(opened_at=datetime.now(UTC) - timedelta(days=2))
    m = F.market(price_usd=Decimal("101"))
    assert evaluate_exits(p, m).reason is ExitReason.TIME_EXIT


def test_healthy_position_is_left_alone():
    p = position()
    m = F.market(price_usd=Decimal("115"))
    assert evaluate_exits(p, m) is None


def test_update_marks_tracks_peak_and_arms_trailing():
    p = position()
    changed = update_marks(p, F.market(price_usd=Decimal("145")))
    assert changed
    assert p.peak_price == Decimal("145")
    assert p.trailing_armed, "+45% peak gain should arm the 40% trailing threshold"


def test_peak_never_falls():
    p = position(peak_price=Decimal("200"))
    update_marks(p, F.market(price_usd=Decimal("120")))
    assert p.peak_price == Decimal("200")
    assert p.current_price == Decimal("120")


def test_realized_pnl_on_partial_exit_is_proportional():
    p = position(amount_raw=1_000_000, cost_basis_usd=Decimal("100"),
                 decimals=6, entry_price=Decimal("100"))
    proceeds, pnl = realized_pnl(p, 500_000, Decimal("150"))
    assert proceeds == Decimal("75")   # 0.5 units * $150
    assert pnl == Decimal("25")        # cost of that half was $50


def test_realized_pnl_negative_on_loss():
    p = position(amount_raw=1_000_000, cost_basis_usd=Decimal("100"), decimals=6)
    proceeds, pnl = realized_pnl(p, 1_000_000, Decimal("60"))
    assert proceeds == Decimal("60")
    assert pnl == Decimal("-40")


# ------------------------------------------- Jupiter call rate (PRD 28.7)
async def test_exit_liquidity_is_cached(redis, monkeypatch):
    """The exit-liquidity probe runs once a second per position.

    Uncached that is 180 Jupiter requests a minute with three positions open, past the
    free tier's limit - and being rate limited on the EXIT path is the worst possible
    place for it. A pool does not drain between one second and the next, so the probe
    is cached while prices stay live.
    """
    from decimal import Decimal

    from asm.domain.models import Quote
    from asm.services.position_service import PositionService

    calls = {"n": 0}

    class FakeJup:
        async def quote(self, **kw):
            calls["n"] += 1
            return Quote(input_mint="a", output_mint="b", in_amount_raw=1,
                         out_amount_raw=1, price_impact_pct=Decimal("2"))

    import asm.services.position_service as P
    monkeypatch.setattr(P, "jupiter", lambda: FakeJup())

    svc = PositionService()
    svc.redis = redis
    pos = position(amount_raw=1_000_000)

    for _ in range(10):
        await svc._exit_liquidity(pos)

    assert calls["n"] == 1, f"made {calls['n']} quotes for 10 ticks; should cache"


async def test_no_route_out_is_not_cached(redis, monkeypatch):
    """A missing route is the worst case, so it must be re-checked rather than
    remembered - otherwise one transient failure strands a position for 30 seconds."""
    from decimal import Decimal

    from asm.services.position_service import PositionService

    calls = {"n": 0}

    class NoRoute:
        async def quote(self, **kw):
            calls["n"] += 1
            return None

    import asm.services.position_service as P
    monkeypatch.setattr(P, "jupiter", lambda: NoRoute())

    svc = PositionService()
    svc.redis = redis
    pos = position(amount_raw=1_000_000)

    for _ in range(3):
        assert await svc._exit_liquidity(pos) == Decimal(0)
    assert calls["n"] == 3, "a missing route must be re-checked every tick"
