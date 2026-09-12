"""PRD 27 - MEV and execution protection.

The PRD's framing is the important part: optimise for *net realized return*, not for
fastest submission. A tip that reliably costs 0.4% to avoid a 0.2% sandwich is a loss
dressed up as protection.

So tipping is sized against the trade, capped, and only applied where it plausibly pays:

  sandwich risk rises with price impact and with notional. A $30 buy into a deep pool is
  not worth sandwiching; a $3,000 buy into a thin one is. Below the threshold we use
  ordinary submission and keep the tip.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from asm.config import settings
from asm.domain.money import ZERO, lamports_to_sol
from asm.logging import get_logger

log = get_logger(__name__)

# Jito tip accounts. A tip must go to one of these to be picked up by the block engine.
JITO_TIP_ACCOUNTS = (
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
)

MIN_TIP_LAMPORTS = 1_000
SANDWICH_RISK_THRESHOLD = Decimal("25")


@dataclass
class ProtectionPlan:
    use_private_route: bool
    tip_lamports: int
    tip_account: str | None
    max_price_impact_pct: Decimal
    reason: str

    @property
    def tip_sol(self) -> Decimal:
        return lamports_to_sol(self.tip_lamports)


def sandwich_risk_score(*, size_usd: Decimal, price_impact_pct: Decimal,
                        liquidity_usd: Decimal) -> Decimal:
    """0-100. How attractive this trade is to sandwich.

    A sandwicher profits from the price move OUR trade causes, so their upside scales
    with our impact and our notional. Their cost is roughly fixed. Both terms must be
    meaningful before tipping is worth it.
    """
    if size_usd <= 0:
        return ZERO

    # Impact is the attacker's profit margin per dollar.
    impact_term = min(price_impact_pct * Decimal(20), Decimal(60))
    # Notional is how many dollars that margin applies to.
    size_term = min(size_usd / Decimal(50), Decimal(40))
    # Deep pools make the attack less reliable to land.
    depth_discount = Decimal(1)
    if liquidity_usd > 0:
        depth_discount = min(Decimal(1), Decimal(500_000) / liquidity_usd + Decimal("0.3"))

    return min((impact_term + size_term) * depth_discount, Decimal(100))


def plan_protection(*, size_usd: Decimal, price_impact_pct: Decimal,
                    liquidity_usd: Decimal, urgent: bool = False,
                    rng: random.Random | None = None) -> ProtectionPlan:
    """Decide whether this trade is worth protecting, and how much to spend doing it."""
    rng = rng or random.Random()
    risk = sandwich_risk_score(size_usd=size_usd, price_impact_pct=price_impact_pct,
                               liquidity_usd=liquidity_usd)

    if not settings.mev_protection_enabled or risk < SANDWICH_RISK_THRESHOLD:
        return ProtectionPlan(
            use_private_route=False, tip_lamports=0, tip_account=None,
            max_price_impact_pct=settings.copyability.max_expected_slippage_pct,
            reason=(f"sandwich_risk_{risk.quantize(Decimal('0.1'))}_below_threshold"
                    if settings.mev_protection_enabled else "mev_protection_disabled"),
        )

    # Tip as a share of notional, scaled by risk, then hard-capped both ways.
    tip_fraction = settings.mev_tip_bps * (risk / Decimal(100)) / Decimal(10_000)
    tip_usd = size_usd * tip_fraction
    sol_price = settings.assumed_sol_price_usd
    tip_lamports = int(tip_usd / sol_price * Decimal(1_000_000_000)) if sol_price > 0 else 0
    tip_lamports = max(MIN_TIP_LAMPORTS,
                       min(tip_lamports, settings.max_tip_lamports))

    # An urgent exit accepts worse pricing, but never an unbounded one.
    max_impact = settings.copyability.max_expected_slippage_pct * (
        Decimal(2) if urgent else Decimal(1))

    return ProtectionPlan(
        use_private_route=True,
        tip_lamports=tip_lamports,
        tip_account=rng.choice(JITO_TIP_ACCOUNTS),
        max_price_impact_pct=max_impact,
        reason=f"sandwich_risk_{risk.quantize(Decimal('0.1'))}",
    )


def tip_is_worth_it(tip_lamports: int, size_usd: Decimal,
                    sol_price_usd: Decimal, expected_saving_pct: Decimal) -> bool:
    """Sanity check: never pay more for protection than the harm it prevents."""
    if size_usd <= 0 or sol_price_usd <= 0:
        return False
    tip_usd = lamports_to_sol(tip_lamports) * sol_price_usd
    return tip_usd < size_usd * expected_saving_pct / Decimal(100)
