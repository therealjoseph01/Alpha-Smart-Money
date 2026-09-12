"""Mode progression (PRD 72, phases C -> D -> E).

backtest -> paper -> shadow -> live, each stage answering a question the previous one
could not:

    paper   does the strategy make money at all, on assumed fills?
    shadow  were those assumed fills honest? (real route, real fee, real simulation)
    live    real money

The first advance is automatic because it is free: nothing in shadow mode can spend, so
there is no reason to make someone sit and press a button once the evidence is in.

**Live is never automatic.** Not because the checks would be weaker, but because
something should have to decide to risk money, and that something should be a person.
The system's job is to refuse when the evidence is not there, not to choose for you.

Every gate below is a reason a promotion was refused, in plain language, so "why is it
still on paper" is always answerable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from asm.config import Mode, settings
from asm.db import models as M
from asm.db.session import session_scope
from asm.logging import get_logger

log = get_logger(__name__)

# Paper must produce enough closed trades to mean something before shadow is worth
# running. Below this the sample is noise and advancing only spends credits.
MIN_PAPER_TRADES = 30
MIN_PAPER_DAYS = 3
MIN_SHADOW_TRADES = 20
MIN_SHADOW_DAYS = 5


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass
class Readiness:
    current: Mode
    target: Mode | None
    gates: list[Gate] = field(default_factory=list)
    automatic: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.gates) and all(g.passed for g in self.gates)

    @property
    def blockers(self) -> list[str]:
        return [g.detail for g in self.gates if not g.passed]

    def as_dict(self) -> dict:
        return {
            "current": self.current.value,
            "target": self.target.value if self.target else None,
            "ready": self.ready,
            "automatic": self.automatic,
            "gates": [{"name": g.name, "passed": g.passed, "detail": g.detail}
                      for g in self.gates],
            "blockers": self.blockers,
        }


async def _closed_trades(mode: str) -> tuple[int, Decimal, datetime | None]:
    async with session_scope() as s:
        row = (await s.execute(
            select(func.count(M.PositionRow.id),
                   func.coalesce(func.sum(M.PositionRow.realized_pnl_usd), 0),
                   func.min(M.PositionRow.opened_at))
            .where(M.PositionRow.mode == mode,
                   M.PositionRow.status == "closed")
        )).one()
    total = row[1]
    return int(row[0]), Decimal(str(total or 0)), row[2]


async def _latest_backtest() -> dict | None:
    async with session_scope() as s:
        row = (await s.execute(
            select(M.Backtest)
            .where(M.Backtest.status == "complete")
            .order_by(M.Backtest.finished_at.desc()).limit(1)
        )).scalar_one_or_none()
    return dict(row.results or {}) if row else None


async def open_positions(mode: str) -> int:
    async with session_scope() as s:
        return int((await s.execute(
            select(func.count()).select_from(M.PositionRow)
            .where(M.PositionRow.mode == mode,
                   M.PositionRow.status.in_(["open", "reducing", "pending"]))
        )).scalar_one())


async def _stage_gates(mode: str, min_trades: int, min_days: int) -> list[Gate]:
    count, pnl, first = await _closed_trades(mode)
    days = (datetime.now(UTC) - first).days if first else 0
    return [
        Gate("sample", count >= min_trades,
             f"{count} closed trades in {mode} (need {min_trades})"),
        Gate("duration", days >= min_days,
             f"{days} days of {mode} (need {min_days})"),
        Gate("profitable", pnl > 0,
             f"{mode} P&L is ${pnl:,.2f}" + ("" if pnl > 0 else " - not positive")),
    ]


async def readiness() -> Readiness:
    """What, if anything, this system is currently entitled to advance to."""
    mode = settings.mode

    if mode is Mode.BACKTEST:
        return Readiness(mode, Mode.PAPER, automatic=True, gates=[
            Gate("ready", True, "backtest mode advances to paper freely"),
        ])

    if mode is Mode.PAPER:
        gates = await _stage_gates("paper", MIN_PAPER_TRADES, MIN_PAPER_DAYS)
        bt = await _latest_backtest()
        verdict = (bt or {}).get("verdict", "")
        gates.append(Gate(
            "backtest", verdict.startswith("VIABLE"),
            f"backtest says {verdict.split(':')[0]}" if verdict
            else "no completed backtest - run one before advancing",
        ))
        return Readiness(mode, Mode.SHADOW, automatic=True, gates=gates)

    if mode is Mode.SHADOW:
        gates = await _stage_gates("shadow", MIN_SHADOW_TRADES, MIN_SHADOW_DAYS)
        ok, detail = _wallets_verdict()
        gates.append(Gate("wallets", ok, detail))
        # Deliberately automatic=False. Everything above can be satisfied and this
        # still will not advance on its own.
        return Readiness(mode, Mode.LIVE, automatic=False, gates=gates)

    return Readiness(mode, None, gates=[Gate("live", True, "already live")])


# base58 - Bitcoin's alphabet, which Solana uses. 0, O, I and l are absent from it
# precisely because they are the characters people transcribe wrongly.
_B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _bad_address(value: str) -> str | None:
    """Why this is not a Solana address, or None if it looks like one."""
    stray = sorted(set(value) - _B58)
    if stray:
        return f"contains {', '.join(repr(c) for c in stray[:3])}"
    if not 32 <= len(value) <= 44:
        return f"is {len(value)} characters, not 32-44"
    return None


def _wallets_verdict() -> tuple[bool, str]:
    """The last gate before real money, so it checks shape as well as presence.

    A mistyped address that only fails at submission fails with the trade already
    decided; a treasury equal to the trading wallet never fails at all - it just
    reports harvested profit as banked while leaving every cent of it at risk.
    """
    trading = settings.trading_wallet_pubkey.strip()
    treasury = settings.treasury_wallet_pubkey.strip()

    missing = [name for name, value in
               (("TRADING_WALLET_PUBKEY", trading),
                ("TREASURY_WALLET_PUBKEY", treasury)) if not value]
    if missing:
        return False, f"{' and '.join(missing)} not set in the environment"

    for name, value in (("TRADING_WALLET_PUBKEY", trading),
                        ("TREASURY_WALLET_PUBKEY", treasury)):
        why = _bad_address(value)
        if why:
            return False, f"{name} {why} - not a Solana address"

    if trading == treasury:
        return False, ("treasury and trading wallet are the same address - "
                       "harvesting would not take profit off the table")

    return True, "trading and treasury wallets configured"


async def advance(force_target: Mode | None = None, actor: str = "auto") -> dict:
    """Advance a stage if entitled to. Live always requires an explicit target."""
    from asm.db import repo

    state = await readiness()
    target = force_target or state.target

    if target is None:
        return {"changed": False, "reason": "no further stage", **state.as_dict()}

    if target is Mode.LIVE and force_target is None:
        return {"changed": False,
                "reason": "live is never automatic - it must be chosen",
                **state.as_dict()}

    if not state.ready:
        return {"changed": False, "reason": "; ".join(state.blockers),
                **state.as_dict()}

    # Changing mode with positions open would strand them: the position service reads
    # only its current mode, so the old ones would never be marked or exited again.
    held = await open_positions(settings.mode.value)
    if held:
        return {"changed": False,
                "reason": f"{held} position(s) still open in {settings.mode.value} - "
                          "close them before changing mode",
                **state.as_dict()}

    previous = settings.mode
    settings.mode = target
    await _persist_mode(target)

    async with session_scope() as s:
        await repo.log_audit(s, actor=actor, action="mode_changed",
                             before={"mode": previous.value},
                             after={"mode": target.value})
        await repo.log_risk_event(
            s, "MODE_CHANGED", f"{previous.value} -> {target.value}",
            severity="critical" if target is Mode.LIVE else "warning",
            detail={"actor": actor, "gates": state.as_dict()["gates"]})

    log.warning("mode_changed", previous=previous.value, new=target.value, actor=actor)
    return {"changed": True, "previous": previous.value, "current": target.value,
            **(await readiness()).as_dict()}


async def _persist_mode(mode: Mode) -> None:
    """Store the mode so every service picks it up, and it survives a restart."""
    from asm.state.client import get_redis

    await get_redis().set("asm:mode", mode.value)
    async with session_scope() as s:
        row = (await s.execute(
            select(M.StrategyConfig).where(M.StrategyConfig.name == "mode")
        )).scalar_one_or_none()
        if row is None:
            s.add(M.StrategyConfig(name="mode", version="current", active=True,
                                   config={"mode": mode.value}))
        else:
            row.config = {"mode": mode.value}


async def load_mode() -> Mode:
    """Read the stored mode at startup. The env var is only the initial default."""
    from asm.state.client import get_redis

    try:
        stored = await get_redis().get("asm:mode")
        if not stored:
            async with session_scope() as s:
                row = (await s.execute(
                    select(M.StrategyConfig).where(M.StrategyConfig.name == "mode")
                )).scalar_one_or_none()
                stored = (row.config or {}).get("mode") if row else None
        if stored:
            settings.mode = Mode(stored)
    except Exception as exc:
        log.warning("mode_load_failed", error=repr(exc))
    return settings.mode
