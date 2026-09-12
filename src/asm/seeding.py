"""Manual wallet seeding - the bootstrap path (PRD 11).

Rather than depending on a scraped leaderboard API, you curate 50-100 wallets by hand
once. From then on the system is self-sufficient: it streams those wallets on-chain,
reconstructs their history, scores them on copyable alpha, and promotes or degrades
them on its own evidence.

A seeded wallet enters as DISCOVERED, not ACTIVE. It has to earn its way to being
copied through backfill scoring (PRD 5.3) - seeding grants attention, never capital.
"""
from __future__ import annotations

import re
from pathlib import Path

from asm.db import repo
from asm.db.session import session_scope
from asm.domain.enums import TraderStatus
from asm.logging import get_logger

log = get_logger(__name__)

# Base58, 32-44 chars. Excludes 0, O, I, l by construction.
SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class SeedError(Exception):
    pass


def parse_seed_file(path: str | Path) -> list[tuple[str, str | None, str | None]]:
    """Returns [(wallet, label, note)]. Raises on malformed addresses so a typo
    never silently becomes a wallet we never watch."""
    p = Path(path)
    if not p.exists():
        raise SeedError(f"seed file not found: {p}")

    out: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(p.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        wallet = parts[0]
        if not SOLANA_ADDRESS.match(wallet):
            raise SeedError(f"{p}:{lineno}: not a valid Solana address: {wallet!r}")
        if wallet in seen:
            log.warning("duplicate_seed_skipped", wallet=wallet[:8], line=lineno)
            continue
        seen.add(wallet)
        out.append((
            wallet,
            parts[1] if len(parts) > 1 and parts[1] else None,
            parts[2] if len(parts) > 2 and parts[2] else None,
        ))
    return out


def parse_seed_text(text: str) -> list[tuple[str, str | None, str | None]]:
    """Parse pasted wallet text - the UI path. Same rules as the file.

    Accepts one address per line, optionally `address,label,note`. Commas, spaces or
    newlines all work as separators for bare addresses, because people paste from
    everywhere. Invalid addresses are reported with their line number rather than
    silently dropped.
    """
    out: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    errors: list[str] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 1:
            # A whitespace-separated paste of several bare addresses.
            tokens = line.split()
            if len(tokens) > 1:
                for token in tokens:
                    if SOLANA_ADDRESS.match(token):
                        if token not in seen:
                            seen.add(token)
                            out.append((token, None, None))
                    else:
                        errors.append(f"line {lineno}: invalid address {token!r}")
                continue

        wallet = parts[0]
        if not SOLANA_ADDRESS.match(wallet):
            errors.append(f"line {lineno}: invalid address {wallet!r}")
            continue
        if wallet in seen:
            continue
        seen.add(wallet)
        out.append((
            wallet,
            parts[1] if len(parts) > 1 and parts[1] else None,
            parts[2] if len(parts) > 2 and parts[2] else None,
        ))

    if errors and not out:
        raise SeedError("; ".join(errors[:5]))
    if errors:
        log.warning("seed_text_partial", errors=errors[:5], accepted=len(out))
    return out


async def import_wallets(
    entries: list[tuple[str, str | None, str | None]],
    *, source: str = "ui", enqueue_backfill: bool = True,
) -> dict:
    """Store wallets in the database. Shared by the file and UI paths."""
    if not entries:
        return {"imported": 0, "total": 0, "wallets": []}

    imported: list[str] = []
    existing_count = 0
    async with session_scope() as s:
        for wallet, label, note in entries:
            existing = await repo.get_trader(s, wallet)
            await repo.upsert_trader(
                s, wallet, source=label or source, label=label,
                status=TraderStatus.DISCOVERED if existing is None else None,
                meta={"note": note} if note else {},
            )
            if existing is None:
                imported.append(wallet)
            else:
                existing_count += 1
        await repo.log_audit(
            s, actor=source, action="wallets_imported",
            after={"total": len(entries), "new": len(imported)},
        )

    if enqueue_backfill and imported:
        await _enqueue_backfill(imported)

    log.info("wallets_imported", source=source, total=len(entries), new=len(imported))
    return {"imported": len(imported), "already_present": existing_count,
            "total": len(entries), "wallets": imported}


async def import_seed_file(path: str | Path, *, enqueue_backfill: bool = True) -> dict:
    entries = parse_seed_file(path)
    if not entries:
        log.warning("seed_file_empty", path=str(path))
        return {"imported": 0, "wallets": []}

    imported: list[str] = []
    async with session_scope() as s:
        for wallet, label, note in entries:
            existing = await repo.get_trader(s, wallet)
            await repo.upsert_trader(
                s, wallet,
                source=label or "manual-seed",
                label=label,
                status=TraderStatus.DISCOVERED if existing is None else None,
                meta={"note": note} if note else {},
            )
            if existing is None:
                imported.append(wallet)
        await repo.log_audit(
            s, actor="cli", action="seed_import", target=str(path),
            after={"total": len(entries), "new": len(imported)},
        )

    if enqueue_backfill and imported:
        await _enqueue_backfill(imported)

    log.info("seed_imported", total=len(entries), new=len(imported))
    return {"imported": len(imported), "total": len(entries), "wallets": imported}


async def _enqueue_backfill(wallets: list[str]) -> None:
    """Queue scoring work on ARQ. Cold path by definition - this can take minutes."""
    from arq import create_pool

    from asm.services.worker import redis_settings

    pool = await create_pool(redis_settings())
    try:
        for wallet in wallets:
            await pool.enqueue_job("backfill_wallet", wallet)
    finally:
        await pool.aclose()
