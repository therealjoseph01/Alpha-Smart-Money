"""Full pipeline in paper mode: detection -> gates -> reservation -> fill -> position.

Network is stubbed; everything else is the real code path, including the Lua
reservation and the real gate sweep.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from asm.domain.enums import Action, Decision, OrderStatus, Reason, TraderStatus
from asm.engine.decision import DecisionEngine, render_decision
from asm.engine.sizing import SizingMode
from asm.execution.paper import PaperExecutor
from tests import factories as F


class StubProvider:
    """Deterministic stand-in for Helius + Jupiter.

    Injected through the same DataProvider seam the backtester uses, so these tests
    exercise the real gate code with pinned inputs.
    """

    def __init__(self, market=None, risk=None, quote=None, flags=None):
        self._market = market or F.market()
        self._risk = risk or F.token_risk()
        self._quote = quote if quote is not None else F.quote()
        self._flags = flags or []

    @property
    def now(self):
        return None

    async def market_state(self, mint, *, decimals=9):
        return self._market

    async def token_risk(self, mint):
        return self._risk

    async def sol_price_usd(self):
        return Decimal("200")

    async def quote(self, **kw):
        return self._quote

    async def insider_flags(self, **kw):
        return self._flags


@pytest.fixture
def stub_network():
    return StubProvider()


async def test_good_signal_becomes_a_filled_position(ledger, stub_network):
    engine = DecisionEngine(ledger, SizingMode.CONFIDENCE_WEIGHTED, data=stub_network)
    trade = F.source_trade()
    trader = F.trader()

    decision, order = await engine.evaluate_entry(trade, trader)

    assert decision.decision is Decision.COPY, [r.value for r in decision.reasons]
    assert order is not None
    assert order.size_usd > 0
    assert order.reservation_id
    assert Reason.HIGH_TRADER_SCORE in decision.reasons
    assert Reason.SUFFICIENT_LIQUIDITY in decision.reasons

    # The reservation must already be reflected in the ledger.
    snap = await ledger.snapshot()
    assert snap.deployed_usd == order.size_usd
    assert snap.cash_usd == Decimal("10000") - order.size_usd

    # Deterministic fill (rng seeded past the failure branch).
    import random
    ex = await PaperExecutor(random.Random(1)).execute(order)
    assert ex.status is OrderStatus.CONFIRMED
    assert ex.out_amount_raw > 0
    assert ex.actual_slippage_pct > 0, "paper fills must never be free"
    assert ex.actual_price > ex.expected_price, "buys fill worse than quoted"

    await ledger.commit(order.reservation_id, ex.value_usd)
    snap = await ledger.snapshot()
    assert snap.open_positions == 1


async def test_decision_snapshot_is_replayable(ledger, stub_network):
    """PRD 5.5 - the stored inputs must be enough to re-run the decision offline."""
    engine = DecisionEngine(ledger, data=stub_network)
    decision, _ = await engine.evaluate_entry(F.source_trade(), F.trader())

    i = decision.inputs
    for section in ("trader", "source_trade", "market", "token_risk", "quote", "portfolio"):
        assert section in i, f"missing {section} from the decision snapshot"
    assert i["market"]["liquidity_usd"]
    assert i["quote"]["price_impact_pct"]
    assert decision.strategy_version
    assert decision.latency.analysis_ms is not None


async def test_price_chase_rejection_is_explained(ledger):
    moved = StubProvider(market=F.market(price=Decimal("1.4"), price_usd=Decimal("1.4")))
    decision, order = await DecisionEngine(ledger, data=moved).evaluate_entry(
        F.source_trade(), F.trader()
    )
    assert decision.decision is Decision.REJECT
    assert order is None
    assert Reason.PRICE_MOVED_TOO_FAR in decision.reasons

    # A rejection must not consume capital.
    snap = await ledger.snapshot()
    assert snap.cash_usd == Decimal("10000")
    assert snap.deployed_usd == Decimal("0")


async def test_sell_signal_is_not_an_entry(ledger, stub_network):
    decision, order = await DecisionEngine(ledger, data=stub_network).evaluate_entry(
        F.source_trade(action=Action.SELL), F.trader()
    )
    assert decision.decision is Decision.REJECT
    assert Reason.NON_COPYABLE_ACTION in decision.reasons
    assert order is None


async def test_unfollowed_trader_short_circuits_before_network(ledger, stub_network):
    decision, _order = await DecisionEngine(ledger, data=stub_network).evaluate_entry(
        F.source_trade(), F.trader(status=TraderStatus.CANDIDATE)
    )
    assert decision.decision is Decision.REJECT
    assert Reason.TRADER_NOT_FOLLOWED in decision.reasons
    assert decision.inputs == {} or "market" not in decision.inputs


async def test_repeated_signals_cannot_exceed_token_cap(ledger, stub_network,
                                                        relaxed_pending):
    """Same trader, same token, over and over. The ledger must hold the line."""
    engine = DecisionEngine(ledger, data=stub_network)
    approved = 0
    for i in range(30):
        decision, order = await engine.evaluate_entry(
            F.source_trade(signature=f"sig{i}"), F.trader()
        )
        if decision.approved:
            approved += 1
            await ledger.commit(order.reservation_id, order.size_usd)

    snap = await ledger.snapshot()
    exposures = await ledger.exposures()
    # 5% token cap on 10k equity
    assert exposures["token"][F.MINT] <= Decimal("500")
    assert snap.deployed_usd <= Decimal("500")
    assert approved >= 1


async def test_failed_fill_releases_the_reservation(ledger, stub_network):
    import random

    engine = DecisionEngine(ledger, data=stub_network)
    _decision, order = await engine.evaluate_entry(F.source_trade(), F.trader())
    assert order is not None

    class AlwaysFails(PaperExecutor):
        def _failed(self, impact_pct):
            return True

    ex = await AlwaysFails(random.Random(0)).execute(order)
    assert ex.status is OrderStatus.FAILED

    await ledger.release(order.reservation_id)
    snap = await ledger.snapshot()
    assert snap.cash_usd == Decimal("10000"), "failed fills must not strand capital"
    assert snap.deployed_usd == Decimal("0")
    assert snap.open_positions == 0


async def test_render_decision_is_human_readable(ledger, stub_network):
    decision, _order = await DecisionEngine(ledger, data=stub_network).evaluate_entry(
        F.source_trade(), F.trader()
    )
    text = render_decision(decision)
    assert "TRADE SIGNAL" in text
    assert "Decision:" in text
    assert "Reason Codes:" in text
