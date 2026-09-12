"""PRD 72 - stage progression. backtest -> paper -> shadow -> live.

The guarantee under test: nothing automatic ever reaches live, and no stage change
can strand open positions.
"""
from __future__ import annotations

import pytest

from asm import promotion
from asm.config import Mode, settings


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setattr(settings, "mode", Mode.PAPER)


async def _stub(monkeypatch, *, trades=0, pnl=0, days=0, verdict=None, held=0):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    first = datetime.now(UTC) - timedelta(days=days) if trades else None

    async def closed(mode):
        return trades, Decimal(str(pnl)), first

    async def backtest():
        return {"verdict": verdict} if verdict else None

    async def open_pos(mode):
        return held

    monkeypatch.setattr(promotion, "_closed_trades", closed)
    monkeypatch.setattr(promotion, "_latest_backtest", backtest)
    monkeypatch.setattr(promotion, "open_positions", open_pos)


async def test_paper_will_not_advance_without_evidence(redis, monkeypatch):
    await _stub(monkeypatch)
    state = await promotion.readiness()
    assert state.target is Mode.SHADOW
    assert not state.ready
    assert len(state.blockers) == 4


async def test_paper_advances_when_everything_passes(redis, monkeypatch):
    await _stub(monkeypatch, trades=50, pnl=120, days=7, verdict="VIABLE: ...")
    state = await promotion.readiness()
    assert state.ready
    assert state.automatic

    result = await promotion.advance(actor="test")
    assert result["changed"]
    assert result["current"] == "shadow"


async def test_a_losing_paper_run_blocks_advancement(redis, monkeypatch):
    await _stub(monkeypatch, trades=50, pnl=-40, days=7, verdict="VIABLE: ...")
    state = await promotion.readiness()
    assert not state.ready
    assert any("P&L" in b for b in state.blockers)


async def test_a_negative_backtest_blocks_advancement(redis, monkeypatch):
    await _stub(monkeypatch, trades=50, pnl=500, days=7,
                verdict="NEGATIVE: lost money after costs")
    state = await promotion.readiness()
    assert not state.ready
    assert any("NEGATIVE" in b for b in state.blockers)


async def test_shadow_never_advances_automatically(redis, monkeypatch):
    """The property this whole module exists for.

    Every gate can pass and the automatic path must still refuse - risking money
    should be a decision, not a schedule.
    """
    monkeypatch.setattr(settings, "mode", Mode.SHADOW)
    monkeypatch.setattr(settings, "trading_wallet_pubkey", "T" * 43)
    monkeypatch.setattr(settings, "treasury_wallet_pubkey", "R" * 43)
    await _stub(monkeypatch, trades=100, pnl=900, days=30)

    state = await promotion.readiness()
    assert state.target is Mode.LIVE
    assert state.ready, "gates should be satisfied"
    assert not state.automatic, "but it must not be automatic"

    result = await promotion.advance(actor="auto")
    assert not result["changed"]
    assert "never automatic" in result["reason"]


async def test_live_requires_an_explicit_target(redis, monkeypatch):
    monkeypatch.setattr(settings, "mode", Mode.SHADOW)
    monkeypatch.setattr(settings, "trading_wallet_pubkey", "T" * 43)
    monkeypatch.setattr(settings, "treasury_wallet_pubkey", "R" * 43)
    await _stub(monkeypatch, trades=100, pnl=900, days=30)

    result = await promotion.advance(force_target=Mode.LIVE, actor="operator")
    assert result["changed"]
    assert result["current"] == "live"


async def test_live_refused_without_wallets(redis, monkeypatch):
    monkeypatch.setattr(settings, "mode", Mode.SHADOW)
    monkeypatch.setattr(settings, "trading_wallet_pubkey", "")
    monkeypatch.setattr(settings, "treasury_wallet_pubkey", "")
    await _stub(monkeypatch, trades=100, pnl=900, days=30)

    result = await promotion.advance(force_target=Mode.LIVE, actor="operator")
    assert not result["changed"]
    assert "WALLET_PUBKEY" in result["reason"]


async def test_open_positions_block_any_mode_change(redis, monkeypatch):
    """The position service reads only its current mode, so switching with positions
    open would leave them unmarked and unexited forever."""
    await _stub(monkeypatch, trades=50, pnl=120, days=7,
                verdict="VIABLE: ...", held=2)
    result = await promotion.advance(actor="test")
    assert not result["changed"]
    assert "still open" in result["reason"]


async def test_the_auto_job_never_reaches_live(redis, monkeypatch):
    from asm.services.tasks.promote import auto_advance

    monkeypatch.setattr(settings, "mode", Mode.SHADOW)
    monkeypatch.setattr(settings, "trading_wallet_pubkey", "T" * 43)
    monkeypatch.setattr(settings, "treasury_wallet_pubkey", "R" * 43)
    await _stub(monkeypatch, trades=100, pnl=900, days=30)

    result = await auto_advance({})
    assert not result["changed"]
    assert settings.mode is Mode.SHADOW
