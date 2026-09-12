"""PRD 54 - key isolation.

The signing key never enters the API process, the decision process, or any prompt.
`LocalSigner` is for development. In production run `asm-signer` as its own process
(own container, no inbound network, key from KMS) and use `RemoteSigner`, which talks
to it over a unix socket.

The signer enforces its own limits. That matters: if the decision service is compromised
or simply buggy, the signer is still a hard ceiling on damage.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Protocol

from asm.config import settings
from asm.logging import get_logger
from asm.state import keys
from asm.state.client import get_redis

log = get_logger(__name__)

# Programs the signer will co-sign for. Anything else is refused.
ALLOWED_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter v6
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token
    "11111111111111111111111111111111",              # System
    "ComputeBudget111111111111111111111111111111",
}


class SignerError(Exception):
    pass


class Signer(Protocol):
    async def sign(self, tx_b64: str, *, max_lamports: int) -> str: ...
    @property
    def pubkey(self) -> str: ...


class LocalSigner:
    """Development only. Requires SOLANA_KEYPAIR (json array or base58)."""

    def __init__(self, keypair_env: str = "SOLANA_KEYPAIR"):
        from solders.keypair import Keypair

        raw = os.environ.get(keypair_env, "")
        if not raw:
            raise SignerError(f"{keypair_env} not set")
        if raw.strip().startswith("["):
            self._kp = Keypair.from_bytes(bytes(json.loads(raw)))
        else:
            import base58

            self._kp = Keypair.from_bytes(base58.b58decode(raw))
        log.warning("local_signer_active", pubkey=str(self._kp.pubkey()))

    @property
    def pubkey(self) -> str:
        return str(self._kp.pubkey())

    async def sign(self, tx_b64: str, *, max_lamports: int) -> str:
        from solders.transaction import VersionedTransaction

        raw = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        await self._enforce(tx, max_lamports)
        signed = VersionedTransaction(tx.message, [self._kp])
        return base64.b64encode(bytes(signed)).decode()

    async def _enforce(self, tx, max_lamports: int) -> None:
        # PRD 55: the kill switch is checked here too, so a pause holds even if
        # everything upstream is wedged.
        redis = get_redis()
        state = await redis.get(keys.SYSTEM_STATE)
        if await redis.get(keys.KILL_SWITCH) or state not in (None, "running", "soft_paused"):
            raise SignerError(f"refusing to sign: system state={state}")

        msg = tx.message
        account_keys = [str(k) for k in msg.account_keys]
        for idx in {ix.program_id_index for ix in msg.instructions}:
            if idx >= len(account_keys):
                raise SignerError("unresolved program address; lookup table validation required")
            if account_keys[idx] not in ALLOWED_PROGRAMS:
                raise SignerError(f"program not allowlisted: {account_keys[idx]}")


class RemoteSigner:
    """Talks to the isolated signer process over a unix socket."""

    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or settings.signer_socket
        self._pubkey = settings.trading_wallet_pubkey

    @property
    def pubkey(self) -> str:
        return self._pubkey

    async def sign(self, tx_b64: str, *, max_lamports: int) -> str:
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        try:
            writer.write(
                (json.dumps({"tx": tx_b64, "max_lamports": max_lamports}) + "\n").encode()
            )
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        finally:
            writer.close()
            await writer.wait_closed()
        resp = json.loads(line)
        if resp.get("error"):
            raise SignerError(resp["error"])
        return resp["signed"]


def get_signer() -> Signer:
    if os.environ.get("SOLANA_KEYPAIR"):
        return LocalSigner()
    return RemoteSigner()
