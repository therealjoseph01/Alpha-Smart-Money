"""PRD 30.4, 31, 32, 33 - capital management.

Principle 5.4: realized gains sitting in the trading wallet are still at risk. So this
module exists to move money OUT of harm's way and to decide how much of what remains
is allowed to compound.

Four related ideas:

  profit lock (32)      a floor under equity that the risk engine will not trade below
  capital buckets (33)  the active balance split by purpose, so one bad week cannot
                        consume the reserve
  compounding (31)      how aggressively realized profit is returned to the active book
  treasury (30.4)       the actual on-chain transfer out of the trading wallet

The transfer is the only part that touches a key, and it goes through the same isolated
signer as a trade. Harvesting that only updates a number is bookkeeping, not protection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from asm.config import settings
from asm.db import models as M
from asm.db.session import session_scope
from asm.domain.money import SOL_MINT, ZERO, lamports_to_sol, sol_to_lamports
from asm.logging import get_logger
from asm.state.ledger import RiskLedger

log = get_logger(__name__)


class CompoundingMode(StrEnum):
    """PRD 31."""

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    GROWTH = "growth"

    @property
    def reinvest_fraction(self) -> Decimal:
        """Share of realized profit that rejoins the active trading balance."""
        return {
            CompoundingMode.CONSERVATIVE: Decimal("0.25"),
            CompoundingMode.BALANCED: Decimal("0.50"),
            CompoundingMode.GROWTH: Decimal("0.85"),
        }[self]


class Bucket(StrEnum):
    """PRD 33 - capital buckets."""

    ACTIVE = "active"        # deployable on trades right now
    RESERVE = "reserve"      # dry powder, released as evidence accumulates
    LOCKED = "locked"        # profit lock; never tradeable without an explicit unlock
    GAS = "gas"              # SOL kept back for fees


@dataclass
class BucketAllocation:
    active_usd: Decimal = ZERO
    reserve_usd: Decimal = ZERO
    locked_usd: Decimal = ZERO
    gas_usd: Decimal = ZERO

    @property
    def total_usd(self) -> Decimal:
        return self.active_usd + self.reserve_usd + self.locked_usd + self.gas_usd

    def as_dict(self) -> dict[str, str]:
        return {
            "active_usd": str(self.active_usd.quantize(Decimal("0.01"))),
            "reserve_usd": str(self.reserve_usd.quantize(Decimal("0.01"))),
            "locked_usd": str(self.locked_usd.quantize(Decimal("0.01"))),
            "gas_usd": str(self.gas_usd.quantize(Decimal("0.01"))),
            "total_usd": str(self.total_usd.quantize(Decimal("0.01"))),
        }


def allocate_buckets(
    equity_usd: Decimal,
    *,
    locked_usd: Decimal = ZERO,
    gas_usd: Decimal = ZERO,
    reserve_pct: Decimal = Decimal("20"),
) -> BucketAllocation:
    """Split equity by purpose. Locked and gas come off the top - they are not
    negotiable - and the remainder splits between active and reserve."""
    available = max(ZERO, equity_usd - locked_usd - gas_usd)
    reserve = available * reserve_pct / Decimal(100)
    return BucketAllocation(
        active_usd=available - reserve, reserve_usd=reserve,
        locked_usd=locked_usd, gas_usd=gas_usd,
    )


def compound_split(realized_profit_usd: Decimal,
                   mode: CompoundingMode) -> tuple[Decimal, Decimal]:
    """PRD 31 - returns (reinvest_usd, set_aside_usd)."""
    if realized_profit_usd <= 0:
        return ZERO, ZERO
    reinvest = realized_profit_usd * mode.reinvest_fraction
    return reinvest, realized_profit_usd - reinvest


class ProfitLock:
    """PRD 32 - a ratcheting floor under equity.

    Once equity has been above a level and the lock has ratcheted, that capital is no
    longer available to lose. The lock only ever moves up; that is the entire point.
    """

    def __init__(self, ledger: RiskLedger | None = None):
        self.ledger = ledger or RiskLedger()
        self.r = self.ledger.r

    @property
    def _key(self) -> str:
        return f"asm:{self.ledger.mode}:profit_lock"

    async def get(self) -> Decimal:
        v = await self.r.get(self._key)
        return Decimal(v) if v else ZERO

    async def set(self, value: Decimal, actor: str = "system") -> Decimal:
        """Ratchets upward only. A request to lower it is ignored, not honoured."""
        current = await self.get()
        if value <= current:
            return current
        await self.r.set(self._key, str(value))
        log.info("profit_lock_raised", old=str(current), new=str(value), actor=actor)
        return value

    async def unlock(self, value: Decimal, actor: str) -> Decimal:
        """Deliberate downward move. Audited, never automatic (PRD 4)."""
        await self.r.set(self._key, str(max(ZERO, value)))
        log.warning("profit_lock_lowered", new=str(value), actor=actor)
        async with session_scope() as s:
            s.add(M.AuditLog(actor=actor, action="profit_lock_lowered",
                             after={"value": str(value)}, occurred_at=datetime.now(UTC)))
        return value

    async def ratchet(self, *, lock_pct: Decimal = Decimal("50")) -> Decimal:
        """Lock a share of gains above the current lock. Called after a harvest."""
        snap = await self.ledger.snapshot()
        current = await self.get()
        gain_above_lock = snap.total_equity_usd - current
        if gain_above_lock <= 0:
            return current
        return await self.set(current + gain_above_lock * lock_pct / Decimal(100))

    async def tradeable_equity(self) -> Decimal:
        """What the risk engine is allowed to consider as risk capital."""
        snap = await self.ledger.snapshot()
        return max(ZERO, snap.total_equity_usd - await self.get())


class TreasuryTransferError(Exception):
    pass


class Treasury:
    """PRD 30.4 - moves harvested profit out of the trading wallet, for real."""

    def __init__(self, ledger: RiskLedger | None = None, signer=None):
        self.ledger = ledger or RiskLedger()
        self._signer = signer

    @property
    def signer(self):
        if self._signer is None:
            from asm.execution.signer import get_signer

            self._signer = get_signer()
        return self._signer

    async def transfer(self, amount_usd: Decimal, *,
                       harvest_event_id=None,
                       destination: str | None = None) -> M.TreasuryTransfer:
        """Send `amount_usd` worth of SOL to the treasury wallet.

        In paper/shadow this records the transfer without touching the chain. In live it
        builds, signs and submits a real SystemProgram transfer.
        """
        if not amount_usd.is_finite() or amount_usd <= 0:
            raise TreasuryTransferError("transfer amount must be finite and positive")
        if destination and destination != settings.treasury_wallet_pubkey:
            raise TreasuryTransferError("destination is not the configured treasury allowlist entry")
        destination = destination or settings.treasury_wallet_pubkey
        if not destination:
            raise TreasuryTransferError(
                "TREASURY_WALLET_PUBKEY is not configured; refusing to harvest into nowhere")

        from asm.engine.market import sol_price_usd

        sol_usd = await sol_price_usd()
        if sol_usd <= 0:
            raise TreasuryTransferError("no SOL price available; cannot size the transfer")

        amount_sol = amount_usd / sol_usd
        lamports = sol_to_lamports(amount_sol)

        row = M.TreasuryTransfer(
            harvest_event_id=harvest_event_id, direction="out",
            amount_usd=amount_usd, amount_raw=Decimal(lamports), mint=SOL_MINT,
            destination=destination, status="pending", occurred_at=datetime.now(UTC),
        )

        if not settings.is_live:
            row.status = "simulated"
            row.signature = f"paper-treasury-{lamports}"
            async with session_scope() as s:
                s.add(row)
            log.info("treasury_transfer_simulated", amount_usd=str(amount_usd),
                     sol=str(amount_sol.quantize(Decimal("0.000001"))))
            return row

        try:
            signature = await self._send_sol(destination, lamports)
            row.status = "confirmed"
            row.signature = signature
            log.info("treasury_transfer_confirmed", signature=signature[:16],
                     amount_usd=str(amount_usd))
        except Exception as exc:
            row.status = "failed"
            log.exception("treasury_transfer_failed", error=repr(exc))
            async with session_scope() as s:
                s.add(row)
                s.add(M.Alert(
                    level="critical", category="treasury",
                    title="Treasury transfer failed",
                    body=f"${amount_usd} could not be moved out of the trading wallet.",
                    detail={"error": repr(exc)[:500], "destination": destination},
                    created_at=datetime.now(UTC)))
            raise TreasuryTransferError(repr(exc)) from exc

        async with session_scope() as s:
            s.add(row)
        return row

    async def _send_sol(self, destination: str, lamports: int) -> str:
        """Build, sign and submit a native SOL transfer."""
        import base64

        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.transaction import VersionedTransaction

        from asm.adapters.helius import helius

        h = helius()
        await self._assert_gas_reserve(lamports)

        blockhash, _ = await h.get_latest_blockhash()
        payer = Pubkey.from_string(self.signer.pubkey)
        dest = Pubkey.from_string(destination)

        # SystemProgram::Transfer - instruction index 2, then u64 lamports LE.
        data = (2).to_bytes(4, "little") + lamports.to_bytes(8, "little")
        ix = Instruction(
            program_id=Pubkey.from_string("11111111111111111111111111111111"),
            accounts=[AccountMeta(payer, True, True), AccountMeta(dest, False, True)],
            data=data,
        )
        msg = MessageV0.try_compile(payer, [ix], [], Hash.from_string(blockhash))
        tx = VersionedTransaction.populate(msg, [])
        unsigned = base64.b64encode(bytes(tx)).decode()

        signed = await self.signer.sign(unsigned, max_lamports=lamports + 10_000)
        signature = await h.send_transaction(signed)
        status = await h.confirm(signature)
        if status is None:
            raise TreasuryTransferError(f"confirmation timeout for {signature}")
        if status.get("err"):
            raise TreasuryTransferError(f"on-chain error: {status['err']}")
        return signature

    async def _assert_gas_reserve(self, lamports: int) -> None:
        """PRD 30.6 - never transfer away the SOL needed to keep trading."""
        from asm.adapters.helius import helius

        balance = await helius().get_balance_sol(self.signer.pubkey)
        remaining = balance - lamports_to_sol(lamports)
        if remaining < settings.risk.sol_gas_reserve:
            raise TreasuryTransferError(
                f"transfer would leave {remaining} SOL, below the "
                f"{settings.risk.sol_gas_reserve} gas reserve"
            )
