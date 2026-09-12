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


class TestWalletGate:
    """The last gate before real money. It checks the shape of the two addresses, not
    just that someone filled the variables in."""

    GOOD = "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o"
    OTHER = "2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f"

    def _set(self, trading, treasury):
        settings.trading_wallet_pubkey = trading
        settings.treasury_wallet_pubkey = treasury

    @pytest.fixture(autouse=True)
    def _restore(self):
        before = (settings.trading_wallet_pubkey, settings.treasury_wallet_pubkey)
        yield
        self._set(*before)

    def test_passes_with_two_valid_distinct_wallets(self):
        self._set(self.GOOD, self.OTHER)
        ok, _ = promotion._wallets_verdict()
        assert ok

    def test_names_the_variable_that_is_missing(self):
        self._set(self.GOOD, "")
        ok, detail = promotion._wallets_verdict()
        assert not ok
        assert "TREASURY_WALLET_PUBKEY" in detail
        assert "TRADING_WALLET_PUBKEY" not in detail

    def test_rejects_the_same_wallet_twice(self):
        """Harvesting into the trading wallet reports profit as banked while leaving
        it entirely at risk - and nothing downstream would ever complain."""
        self._set(self.GOOD, self.GOOD)
        ok, detail = promotion._wallets_verdict()
        assert not ok
        assert "same address" in detail

    @pytest.mark.parametrize("bad", [
        "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0",   # an ethereum address
        "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54O",  # capital O for lowercase o
        "tooshort",
    ])
    def test_rejects_a_malformed_address(self, bad):
        self._set(bad, self.OTHER)
        ok, detail = promotion._wallets_verdict()
        assert not ok
        assert "not a Solana address" in detail
