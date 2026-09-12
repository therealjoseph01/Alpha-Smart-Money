"""Verify that an address is actually a WALLET before watching it.

This exists because of a real incident. Two addresses that looked like wallets were
actually pump.fun token mints. `logsSubscribe` with `mentions` on a mint notifies you
about every transaction involving that token *by anyone on Solana* - so a watchlist of
five addresses produced ~49,000 notifications and ~11,500 getTransaction calls in
under an hour.

Base58 validity is not enough. On Solana the account's OWNER says what it is:

    System Program  -> a user wallet          (what we want)
    SPL Token       -> a mint or token account (a firehose)
    executable      -> a program               (a bigger firehose)

One getAccountInfo call, 1 credit, once per address at import. It pays for itself
the first time it catches one.
"""
from __future__ import annotations

from dataclasses import dataclass

from asm.adapters.helius import helius
from asm.logging import get_logger

log = get_logger(__name__)

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}


@dataclass(frozen=True)
class Verdict:
    wallet: str
    ok: bool
    kind: str          # wallet | token | program | unknown | unchecked
    reason: str

    @property
    def label(self) -> str:
        return f"{self.wallet[:8]}… {self.kind}: {self.reason}"


async def verify(address: str) -> Verdict:
    """Classify an address. Fails OPEN on an RPC error - we do not block an import
    because Helius happened to be unreachable, we just cannot confirm."""
    try:
        result = await helius().rpc(
            "getAccountInfo", [address, {"encoding": "jsonParsed"}])
    except Exception as exc:
        log.warning("address_verify_failed", address=address[:8], error=repr(exc))
        return Verdict(address, True, "unchecked", "could not reach the RPC")

    value = (result or {}).get("value")
    if value is None:
        # Never funded. Not a mint or program, so harmless to watch - but it has no
        # history either, so it can never earn a score.
        return Verdict(address, True, "wallet", "account not found on chain (unused)")

    owner = value.get("owner", "")
    if value.get("executable"):
        return Verdict(address, False, "program",
                       "this is a program - watching it would stream every "
                       "transaction that uses it")
    if owner in TOKEN_PROGRAMS:
        parsed = (value.get("data") or {}).get("parsed") or {}
        kind = parsed.get("type", "token")
        return Verdict(address, False, "token",
                       f"this is a {kind}, not a wallet - watching it would stream "
                       "every trade of that token by anyone")
    if owner != SYSTEM_PROGRAM:
        return Verdict(address, False, "program",
                       f"owned by {owner[:8]}…, not the System Program - not a wallet")

    return Verdict(address, True, "wallet", "ok")


async def verify_many(addresses: list[str]) -> dict[str, Verdict]:
    """Sequential on purpose - the adapter paces itself, and this runs at import."""
    return {a: await verify(a) for a in addresses}
