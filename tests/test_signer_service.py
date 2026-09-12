"""PRD 54 - the signer daemon.

The only process that holds the key. It re-checks everything itself, because the point
of isolating it is that it does not trust the caller: if the trading service is buggy
or compromised, these rules are still the ceiling on damage.
"""
from __future__ import annotations

import base64
import json
import os

import pytest

from asm.execution.signer import ALLOWED_PROGRAMS, RemoteSigner, SignerError
from asm.execution.signer_service import SignerService
from asm.state import keys

SOCK = "/tmp/asm-signer-pytest.sock"


def _tx(program: str, payer: str | None = None) -> str:
    """Build an unsigned transaction. `payer` must be the signer's own pubkey for it
    to be signable - solders rejects a keypair/pubkey mismatch, which is itself a
    useful guarantee (see test_refuses_a_transaction_for_a_different_wallet)."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction

    owner = Pubkey.from_string(payer) if payer else Keypair().pubkey()
    ix = Instruction(program_id=Pubkey.from_string(program),
                     accounts=[AccountMeta(owner, True, True)], data=b"\x00")
    msg = MessageV0.try_compile(owner, [ix], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, []))).decode()


@pytest.fixture
async def daemon(redis, monkeypatch):
    from solders.keypair import Keypair

    monkeypatch.setenv("SOLANA_KEYPAIR", json.dumps(list(bytes(Keypair()))))
    await redis.set(keys.SYSTEM_STATE, "running")
    await redis.delete(keys.KILL_SWITCH)

    svc = SignerService(SOCK)
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


async def test_signs_an_allowlisted_transaction(daemon):
    tx = _tx("11111111111111111111111111111111", payer=daemon.signer.pubkey)
    signed = await RemoteSigner(SOCK).sign(tx, max_lamports=1_000)
    assert signed and signed != tx


async def test_refuses_a_transaction_for_a_different_wallet(daemon):
    """A transaction whose fee payer is not this signer cannot be signed by it.

    Worth an explicit test: it means a caller cannot get the daemon to co-sign
    something built for somebody else's account.
    """
    tx = _tx("11111111111111111111111111111111")      # random payer
    with pytest.raises(SignerError):
        await RemoteSigner(SOCK).sign(tx, max_lamports=1_000)


async def test_refuses_a_program_not_on_the_allowlist(daemon):
    """A compromised caller could hand it anything. The allowlist is the answer."""
    tx = _tx("Vote111111111111111111111111111111111111111")
    with pytest.raises(SignerError, match="not allowlisted"):
        await RemoteSigner(SOCK).sign(tx, max_lamports=1_000)


async def test_kill_switch_stops_signing(daemon, redis):
    """The daemon checks this itself, so a pause holds even if everything upstream
    is wedged or lying."""
    await redis.set(keys.KILL_SWITCH, "1")
    tx = _tx("11111111111111111111111111111111")
    with pytest.raises(SignerError, match="kill switch"):
        await RemoteSigner(SOCK).sign(tx, max_lamports=1_000)


async def test_hard_pause_stops_signing(daemon, redis):
    await redis.set(keys.SYSTEM_STATE, "hard_paused")
    tx = _tx("11111111111111111111111111111111")
    with pytest.raises(SignerError, match="hard_paused"):
        await RemoteSigner(SOCK).sign(tx, max_lamports=1_000)


async def test_enforces_its_own_value_ceiling(daemon):
    """A bug in the trading code must not be able to raise this."""
    from asm.config import settings

    tx = _tx("11111111111111111111111111111111")
    too_much = settings.signer_max_lamports + 1
    with pytest.raises(SignerError, match="exceeds the daemon"):
        await RemoteSigner(SOCK).sign(tx, max_lamports=too_much)


async def test_socket_is_private_to_this_user(daemon):
    """No network listener, and 0600 - if you cannot run code as this user you
    cannot ask it to sign anything."""
    mode = os.stat(SOCK).st_mode & 0o777
    assert mode == 0o600


async def test_socket_is_removed_on_shutdown(redis, monkeypatch):
    from solders.keypair import Keypair

    monkeypatch.setenv("SOLANA_KEYPAIR", json.dumps(list(bytes(Keypair()))))
    svc = SignerService("/tmp/asm-signer-shutdown.sock")
    await svc.start()
    assert os.path.exists("/tmp/asm-signer-shutdown.sock")
    await svc.stop()
    assert not os.path.exists("/tmp/asm-signer-shutdown.sock")


async def test_errors_never_echo_transaction_bytes(daemon):
    """An error body must not become a side channel for what was being signed."""
    import asyncio

    reader, writer = await asyncio.open_unix_connection(SOCK)
    writer.write(b'{"tx": "not-valid-base64!!", "max_lamports": 1}\n')
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()

    body = json.loads(line)
    assert "error" in body
    assert "not-valid-base64" not in body["error"]


def test_allowlist_covers_only_what_trading_needs():
    """Every entry should be justifiable; an over-broad allowlist is not one."""
    assert "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4" in ALLOWED_PROGRAMS
    assert "11111111111111111111111111111111" in ALLOWED_PROGRAMS
    assert len(ALLOWED_PROGRAMS) <= 8
