"""Risk ledger: Redis is working memory, Postgres is truth. PRD 22-25, 52."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from redis.asyncio import Redis

from asm.config import settings
from asm.domain.enums import Reason, SystemState
from asm.domain.models import PortfolioState
from asm.logging import get_logger
from asm.state import keys, scripts
from asm.state.client import get_redis

log = get_logger(__name__)
RESERVATION_TTL = 120


@dataclass(slots=True)
class Reservation:
    granted: bool
    id: str | None = None
    usd: Decimal = Decimal(0)
    reason: Reason | None = None
    detail: str = ""


def _d(v) -> Decimal:
    return Decimal(str(v)) if v not in (None, "", False) else Decimal(0)


class RiskLedger:
    def __init__(self, redis: Redis | None = None, mode: str | None = None,
                 system_state_key: str | None = None):
        self.r = redis or get_redis()
        self.mode = mode or settings.mode.value
        # Real modes share the global state key, so one kill switch stops everything.
        # A backtest is a simulation, not trading, and gets its own isolated key -
        # replaying history must work whether or not live trading is started.
        self.system_state_key = system_state_key or keys.SYSTEM_STATE
        self._reserve = self.r.register_script(scripts.RESERVE)
        self._release = self.r.register_script(scripts.RELEASE)
        self._commit = self.r.register_script(scripts.COMMIT)
        self._release_pos = self.r.register_script(scripts.RELEASE_POSITION)
        self._mark = self.r.register_script(scripts.MARK)

    def _base_keys(self) -> list[str]:
        return [
            keys.risk_state(self.mode),
            keys.exposure_token(self.mode),
            keys.exposure_trader(self.mode),
        ]

    # ------------------------------------------------------------------ setup
    async def bootstrap(self, capital_usd: Decimal, force: bool = False) -> None:
        k = keys.risk_state(self.mode)
        if not force and await self.r.exists(k):
            return
        await cast(Awaitable[int], self.r.hset(
            k,
            mapping={
                "equity": str(capital_usd),
                "cash": str(capital_usd),
                "deployed": "0",
                "unrealized": "0",
                "realized_today": "0",
                "hwm": str(capital_usd),
                "open_positions": "0",
                "pending": "0",
                "harvested_total": "0",
                "sol_balance": "0",
            },
        ))
        log.info("risk_ledger_bootstrapped", mode=self.mode, capital=str(capital_usd))

    # ------------------------------------------------------- reserve / release
    async def reserve(self, *, mint: str, wallet: str, size_usd: Decimal) -> Reservation:
        """Atomic check-and-reserve. The only place entry risk is authorised."""
        res_id = uuid.uuid4().hex[:24]
        rc = settings.risk
        out = await self._reserve(
            keys=[*self._base_keys(), keys.reservation(res_id, self.mode),
                  self.system_state_key, keys.KILL_SWITCH],
            args=[
                mint,
                wallet,
                str(size_usd),
                str(rc.max_daily_loss_pct),
                str(rc.max_drawdown_pct),
                str(rc.max_portfolio_deployed_pct),
                str(settings.copyability.max_open_positions),
                str(rc.max_concurrent_pending),
                str(settings.copyability.max_token_exposure_pct),
                str(rc.max_trader_risk_budget_pct),
                str(rc.min_position_usd),
                str(RESERVATION_TTL),
                res_id,
            ],
        )
        ok, a, b = int(out[0]), out[1], out[2] if len(out) > 2 else ""
        if ok:
            return Reservation(granted=True, id=a, usd=_d(b))
        try:
            reason = Reason(a)
        except ValueError:
            reason = Reason.INSUFFICIENT_CAPITAL
        return Reservation(granted=False, reason=reason, detail=str(b))

    async def release(self, res_id: str) -> Decimal:
        out = await self._release(
            keys=[*self._base_keys(), keys.reservation(res_id, self.mode)]
        )
        return _d(out[1]) if int(out[0]) else Decimal(0)

    async def commit(self, res_id: str, actual_usd: Decimal) -> Decimal:
        out = await self._commit(
            keys=[*self._base_keys(), keys.reservation(res_id, self.mode)],
            args=[str(actual_usd)],
        )
        return _d(out[1]) if int(out[0]) else Decimal(0)

    async def release_position(
        self,
        *,
        mint: str,
        wallet: str,
        cost_released_usd: Decimal,
        realized_pnl_usd: Decimal,
        proceeds_usd: Decimal,
        fully_closed: bool,
    ) -> None:
        await self._release_pos(
            keys=self._base_keys(),
            args=[
                mint,
                wallet,
                str(cost_released_usd),
                str(realized_pnl_usd),
                str(proceeds_usd),
                "1" if fully_closed else "0",
            ],
        )

    async def mark(self, unrealized_usd: Decimal, sol_balance: Decimal | None = None):
        out = await self._mark(
            keys=[keys.risk_state(self.mode)],
            args=[str(unrealized_usd), str(sol_balance) if sol_balance is not None else ""],
        )
        return _d(out[0]), _d(out[1])

    # ------------------------------------------------------------------ reads
    async def snapshot(self) -> PortfolioState:
        h = await cast(Awaitable[dict], self.r.hgetall(keys.risk_state(self.mode)))
        return PortfolioState(
            active_capital_usd=_d(h.get("equity")),
            cash_usd=_d(h.get("cash")),
            deployed_usd=_d(h.get("deployed")),
            unrealized_pnl_usd=_d(h.get("unrealized")),
            realized_pnl_today_usd=_d(h.get("realized_today")),
            harvested_total_usd=_d(h.get("harvested_total")),
            high_water_mark_usd=_d(h.get("hwm")),
            open_positions=int(_d(h.get("open_positions"))),
            sol_balance=_d(h.get("sol_balance")),
        )

    async def exposures(self) -> dict[str, dict[str, Decimal]]:
        tok = await cast(Awaitable[dict], self.r.hgetall(keys.exposure_token(self.mode)))
        tr = await cast(Awaitable[dict], self.r.hgetall(keys.exposure_trader(self.mode)))
        return {
            "token": {k: _d(v) for k, v in tok.items()},
            "trader": {k: _d(v) for k, v in tr.items()},
        }

    async def reset_daily(self) -> None:
        await cast(Awaitable[int], self.r.hset(keys.risk_state(self.mode), "realized_today", "0"))

    async def add_harvest(self, amount_usd: Decimal) -> None:
        k = keys.risk_state(self.mode)
        async with self.r.pipeline(transaction=True) as p:
            p.hincrbyfloat(k, "cash", -float(amount_usd))
            p.hincrbyfloat(k, "harvested_total", float(amount_usd))
            await p.execute()


class SystemControl:
    """PRD 55 - emergency controls. Checked by the Lua script itself, so a pause
    holds even if the decision service is wedged."""

    def __init__(self, redis: Redis | None = None):
        self.r = redis or get_redis()

    async def get(self) -> SystemState:
        """Absent state means never started - fail closed, not open.

        A trading system that begins spending the moment it boots is the wrong default.
        Starting is a deliberate act; once taken it persists in Redis, so the system
        keeps running across service restarts and regardless of any open browser.
        """
        if await self.r.get(keys.KILL_SWITCH):
            return SystemState.KILLED
        v = await self.r.get(keys.SYSTEM_STATE)
        try:
            return SystemState(v) if v else SystemState.STOPPED
        except ValueError:
            return SystemState.HARD_PAUSED

    async def start(self, actor: str = "user") -> SystemState:
        """Begin trading. Nothing is copied until this is called."""
        if await self.get() is SystemState.KILLED:
            raise RuntimeError("kill switch is active; clear it deliberately first")
        await self.set(SystemState.RUNNING, actor)
        return SystemState.RUNNING

    async def stop(self, actor: str = "user") -> SystemState:
        """Stop opening new positions. Exits keep running so nothing is stranded."""
        await self.set(SystemState.STOPPED, actor)
        return SystemState.STOPPED

    async def clear_kill(self, actor: str = "system") -> None:
        async with self.r.pipeline(transaction=True) as p:
            p.set(keys.SYSTEM_STATE, SystemState.HARD_PAUSED.value)
            p.delete(keys.KILL_SWITCH)
            await p.execute()
        log.warning("kill_switch_cleared", actor=actor)

    async def set(self, state: SystemState, actor: str = "system") -> None:
        await self.r.set(keys.SYSTEM_STATE, state.value)
        log.warning("system_state_changed", state=state.value, actor=actor)

    async def kill(self, actor: str = "system") -> None:
        await self.r.set(keys.KILL_SWITCH, "1")
        await self.set(SystemState.KILLED, actor)
