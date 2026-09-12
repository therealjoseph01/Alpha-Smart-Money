"""Regression for a real incident.

Five addresses seeded as "wallets" were pump.fun token mints. `logsSubscribe` with
`mentions` on a mint notifies you about every transaction touching that token by
anyone on Solana - roughly 49,000 notifications and 11,500 getTransaction calls in
under an hour, from a five-address watchlist.

Base58 validity was the only check. It is not enough.
"""
from __future__ import annotations

import pytest

from asm.ingest.verify import SYSTEM_PROGRAM, TOKEN_PROGRAMS, Verdict, verify


def _account(monkeypatch, value):
    from asm.adapters import helius as H

    class FakeRpc:
        async def rpc(self, method, params=None, **kw):
            return {"value": value}

    monkeypatch.setattr(H, "helius", lambda: FakeRpc())
    import asm.ingest.verify as V
    monkeypatch.setattr(V, "helius", lambda: FakeRpc())


async def test_a_real_wallet_is_accepted(monkeypatch):
    _account(monkeypatch, {"owner": SYSTEM_PROGRAM, "executable": False})
    v = await verify("W" * 43)
    assert v.ok and v.kind == "wallet"


async def test_a_token_mint_is_rejected(monkeypatch):
    """The exact case that caused the incident."""
    _account(monkeypatch, {
        "owner": next(iter(TOKEN_PROGRAMS)), "executable": False,
        "data": {"parsed": {"type": "mint"}},
    })
    v = await verify("Bj1CbypNWSvA3VAX15m5y92RL3ujftj747yNjwqpump")
    assert not v.ok
    assert v.kind == "token"
    assert "every trade of that token" in v.reason


async def test_a_program_is_rejected(monkeypatch):
    _account(monkeypatch, {"owner": "BPFLoader", "executable": True})
    v = await verify("P" * 43)
    assert not v.ok and v.kind == "program"


async def test_an_unfunded_address_is_allowed(monkeypatch):
    """Never used on chain: harmless to watch, though it can never earn a score."""
    _account(monkeypatch, None)
    v = await verify("N" * 43)
    assert v.ok and v.kind == "wallet"


async def test_rpc_failure_fails_open(monkeypatch):
    """An unreachable RPC must not block an import - we just cannot confirm."""
    import asm.ingest.verify as V

    class Broken:
        async def rpc(self, *a, **kw):
            raise RuntimeError("rpc down")

    monkeypatch.setattr(V, "helius", lambda: Broken())
    v = await verify("W" * 43)
    assert v.ok and v.kind == "unchecked"


async def test_import_rejects_tokens_and_reports_why(monkeypatch, redis):
    from asm import seeding

    async def fake_verify_many(addresses):
        return {a: Verdict(a, False, "token", "this is a mint, not a wallet")
                for a in addresses}

    monkeypatch.setattr("asm.ingest.verify.verify_many", fake_verify_many)
    result = await seeding.import_wallets(
        [("B" * 43, None, None)], enqueue_backfill=False)

    assert result["imported"] == 0
    assert result["rejected"][0]["kind"] == "token"


def test_firehose_threshold_is_below_any_human_rate():
    """No trader makes 120 transactions a minute; a token's global flow easily does."""
    from asm.services.decision_service import FIREHOSE_PER_MINUTE

    assert 30 <= FIREHOSE_PER_MINUTE <= 600
