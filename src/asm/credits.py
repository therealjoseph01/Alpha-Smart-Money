"""Credit budget guard.

Watching costs money, and the cost is not proportional to anything you can see in
advance. Three separate estimates of it were wrong here, all optimistic, because
`logsSubscribe` with `mentions` fires on every transaction a wallet APPEARS in - not
just the ones it initiates. A wallet entangled in busy tokens costs far more than its
trade count suggests, and there is no way to know which wallets those are until you
watch them.

So the system measures its own burn and acts on it, rather than trusting a model:

  * projected monthly spend is computed from the live rate;
  * past a warning threshold it alerts;
  * past the hard threshold it suspends the single noisiest wallet and re-subscribes,
    then keeps going until the projection is back inside budget.

Suspending the worst offender rather than halting everything is deliberate. The
expensive wallets are almost always the hyperactive ones, and those are exactly the
wallets that cannot be copied at a follower's latency - so the cheapest wallet to drop
is also the least valuable one to keep.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from asm.config import settings
from asm.logging import get_logger
from asm.state.client import get_redis

log = get_logger(__name__)

SECONDS_PER_MONTH = 60 * 60 * 24 * 30
MIN_SAMPLE_SECONDS = 120          # below this the rate is noise


def _key(day: str | None = None) -> str:
    day = day or datetime.now(UTC).strftime("%Y%m%d")
    return f"asm:credits:{day}"


@dataclass
class Burn:
    credits: int
    seconds: float
    budget: int

    @property
    def per_minute(self) -> float:
        return self.credits / (self.seconds / 60) if self.seconds else 0.0

    @property
    def projected_month(self) -> float:
        return self.per_minute * SECONDS_PER_MONTH / 60

    @property
    def pct_of_budget(self) -> float:
        return self.projected_month / self.budget * 100 if self.budget else 0.0

    @property
    def days_until_exhausted(self) -> float:
        per_day = self.per_minute * 60 * 24
        return self.budget / per_day if per_day else float("inf")


@dataclass
class BudgetGuard:
    """Tracks burn since the process started and decides when to intervene."""

    warn_pct: float = 60.0
    hard_pct: float = 100.0
    started: float = field(default_factory=time.monotonic)
    baseline: int | None = None
    _warned: bool = False

    async def _total(self) -> int:
        v = await get_redis().hget(_key(), "_total")
        return int(v or 0)

    async def sample(self) -> Burn | None:
        """Burn since this guard started, or None while the sample is too short."""
        if self.baseline is None:
            self.baseline = await self._total()
            return None
        elapsed = time.monotonic() - self.started
        if elapsed < MIN_SAMPLE_SECONDS:
            return None
        used = await self._total() - self.baseline
        return Burn(credits=max(0, used), seconds=elapsed,
                    budget=settings.monthly_credit_budget)

    async def check(self, wallet_costs: dict[str, int]) -> str | None:
        """Returns the wallet to suspend, or None.

        `wallet_costs` is a per-wallet notification count from the caller, used only to
        pick the worst offender - the decision to act comes from the measured rate.
        """
        burn = await self.sample()
        if burn is None or burn.credits == 0:
            return None

        if burn.pct_of_budget >= self.hard_pct:
            worst = max(wallet_costs, key=wallet_costs.get, default=None)
            log.error(
                "credit_budget_exceeded",
                per_minute=round(burn.per_minute),
                projected_month=round(burn.projected_month),
                pct_of_budget=round(burn.pct_of_budget),
                days_left=round(burn.days_until_exhausted, 1),
                suspending=worst[:8] if worst else None,
            )
            return worst

        if burn.pct_of_budget >= self.warn_pct and not self._warned:
            self._warned = True
            log.warning(
                "credit_budget_warning",
                per_minute=round(burn.per_minute),
                projected_month=round(burn.projected_month),
                pct_of_budget=round(burn.pct_of_budget),
            )
        return None


async def current_burn(window_seconds: float = 300) -> Burn:
    """Burn over a recent window, for reporting."""
    r = get_redis()
    total = int(await r.hget(_key(), "_total") or 0)
    marker = "asm:credits:window"
    prev = await r.hgetall(marker)
    now = time.time()

    if prev and float(prev.get("at", 0)) > now - window_seconds * 3:
        elapsed = max(1.0, now - float(prev["at"]))
        used = max(0, total - int(prev.get("total", 0)))
    else:
        elapsed, used = 1.0, 0

    await r.hset(marker, mapping={"at": str(now), "total": str(total)})
    await r.expire(marker, 86_400)
    return Burn(credits=used, seconds=elapsed,
                budget=settings.monthly_credit_budget)
