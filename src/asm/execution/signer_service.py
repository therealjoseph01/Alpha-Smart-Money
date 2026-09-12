"""The signer daemon (PRD 54).

This is the only process that ever holds the private key. It does one thing: accept a
serialised transaction over a unix socket, check it against its own rules, and return a
signature. It does not trade, does not read the market, and does not trust the caller.

Why a separate process rather than a key in the trading service:

  * the key is never in the memory, environment, or crash dumps of the code that talks
    to the internet;
  * the rules here are enforced even when the caller is buggy or compromised - it
    re-checks the kill switch, the program allowlist and the value ceiling itself;
  * it can be run as a different user, with no outbound network at all.

The socket is created with 0600 permissions, so only the user running it can connect.
There is no network listener: if you cannot already run code as that user, you cannot
ask it to sign anything.

Run it with:  python -m asm.execution.signer_service
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
from pathlib import Path

from asm.config import settings
from asm.execution.signer import ALLOWED_PROGRAMS, LocalSigner, SignerError
from asm.logging import get_logger, setup_logging
from asm.state import keys
from asm.state.client import close_redis, get_redis

log = get_logger(__name__)

MAX_REQUEST_BYTES = 64 * 1024
REQUEST_TIMEOUT = 10.0


class SignerService:
    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or settings.signer_socket
        self._signer: LocalSigner | None = None
        self._server: asyncio.AbstractServer | None = None
        self.signed = 0
        self.refused = 0

    @property
    def signer(self) -> LocalSigner:
        if self._signer is None:
            self._signer = LocalSigner()
        return self._signer

    # ------------------------------------------------------------- validation
    async def _check(self, tx_b64: str, max_lamports: int) -> None:
        """Every rule the daemon enforces on its own behalf."""
        from solders.transaction import VersionedTransaction

        state = await get_redis().get(keys.SYSTEM_STATE)
        if state in ("killed", "hard_paused"):
            raise SignerError(f"refusing to sign: system state is {state}")
        if await get_redis().get(keys.KILL_SWITCH):
            raise SignerError("refusing to sign: kill switch is latched")

        raw = base64.b64decode(tx_b64)
        if len(raw) > MAX_REQUEST_BYTES:
            raise SignerError("transaction is implausibly large")

        tx = VersionedTransaction.from_bytes(raw)
        account_keys = [str(k) for k in tx.message.account_keys]

        for ix in tx.message.instructions:
            idx = ix.program_id_index
            if idx >= len(account_keys):
                raise SignerError("malformed instruction: program index out of range")
            program = account_keys[idx]
            if program not in ALLOWED_PROGRAMS:
                raise SignerError(f"program not allowlisted: {program}")

        ceiling = settings.signer_max_lamports
        if max_lamports > ceiling:
            raise SignerError(
                f"requested ceiling {max_lamports} exceeds the daemon's own limit "
                f"of {ceiling} lamports")

    # ---------------------------------------------------------------- serving
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(
                reader.readline(), timeout=REQUEST_TIMEOUT)
            if not line:
                return
            request = json.loads(line)
            tx_b64 = request["tx"]
            max_lamports = int(request.get("max_lamports", 0))

            await self._check(tx_b64, max_lamports)
            signed = await self.signer.sign(tx_b64, max_lamports=max_lamports)
            self.signed += 1
            log.info("signed", total=self.signed, max_lamports=max_lamports)
            response = {"signed": signed}

        except SignerError as exc:
            self.refused += 1
            log.warning("refused", reason=str(exc), total=self.refused)
            response = {"error": str(exc)}
        except Exception as exc:
            self.refused += 1
            # Never echo the exception body - it can contain transaction bytes.
            log.exception("signer_error", kind=exc.__class__.__name__)
            response = {"error": f"signer error: {exc.__class__.__name__}"}

        with contextlib.suppress(Exception):
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()

    def _prepare_socket_path(self) -> Path:
        """Filesystem work happens off the loop; it runs once at startup."""
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        return path

    async def start(self) -> None:
        path = await asyncio.to_thread(self._prepare_socket_path)

        self._server = await asyncio.start_unix_server(self._handle, path=str(path))
        # Only this user may ask for a signature. There is no network listener at all.
        await asyncio.to_thread(os.chmod, path, 0o600)

        log.warning("signer_started", socket=str(path), pubkey=self.signer.pubkey,
                    mode=settings.mode.value)
        if not settings.is_live:
            log.warning("signer_not_needed",
                        note=f"mode is {settings.mode.value}; nothing will ask to sign")

    async def serve_forever(self) -> None:
        await self.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(Path(self.socket_path).unlink)
        await close_redis()
        log.warning("signer_stopped", signed=self.signed, refused=self.refused)


async def main() -> None:
    setup_logging()
    if not os.environ.get("SOLANA_KEYPAIR"):
        log.error("no_key",
                  note="SOLANA_KEYPAIR is not set. The signer has nothing to sign with.")
        raise SystemExit(1)
    await SignerService().serve_forever()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
