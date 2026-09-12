"""PRD 27 - protection must be worth more than it costs."""
from __future__ import annotations

import random
from decimal import Decimal

from asm.execution.mev import (
    JITO_TIP_ACCOUNTS,
    SANDWICH_RISK_THRESHOLD,
    plan_protection,
    sandwich_risk_score,
    tip_is_worth_it,
)

RNG = random.Random(1)


def test_small_trade_in_deep_pool_is_not_worth_protecting():
    """Paying a tip to protect a $30 buy is how you lose money slowly."""
    plan = plan_protection(size_usd=Decimal("30"), price_impact_pct=Decimal("0.1"),
                           liquidity_usd=Decimal("500000"), rng=RNG)
    assert not plan.use_private_route
    assert plan.tip_lamports == 0
    assert "below_threshold" in plan.reason


def test_large_trade_in_thin_pool_is_protected():
    plan = plan_protection(size_usd=Decimal("3000"), price_impact_pct=Decimal("3"),
                           liquidity_usd=Decimal("50000"), rng=RNG)
    assert plan.use_private_route
    assert plan.tip_lamports > 0
    assert plan.tip_account in JITO_TIP_ACCOUNTS


def test_risk_rises_with_impact_and_size():
    base = sandwich_risk_score(size_usd=Decimal("500"), price_impact_pct=Decimal("1"),
                               liquidity_usd=Decimal("100000"))
    bigger = sandwich_risk_score(size_usd=Decimal("5000"), price_impact_pct=Decimal("1"),
                                 liquidity_usd=Decimal("100000"))
    sharper = sandwich_risk_score(size_usd=Decimal("500"), price_impact_pct=Decimal("4"),
                                  liquidity_usd=Decimal("100000"))
    assert bigger > base
    assert sharper > base


def test_deep_liquidity_lowers_risk():
    thin = sandwich_risk_score(size_usd=Decimal("1000"), price_impact_pct=Decimal("2"),
                               liquidity_usd=Decimal("30000"))
    deep = sandwich_risk_score(size_usd=Decimal("1000"), price_impact_pct=Decimal("2"),
                               liquidity_usd=Decimal("5000000"))
    assert thin > deep


def test_tip_is_capped():
    """A runaway tip must never eat the trade."""
    from asm.config import settings

    plan = plan_protection(size_usd=Decimal("100000"), price_impact_pct=Decimal("5"),
                           liquidity_usd=Decimal("20000"), rng=RNG)
    assert plan.tip_lamports <= settings.max_tip_lamports


def test_tip_stays_a_small_share_of_notional():
    from asm.config import settings

    for size in (Decimal("200"), Decimal("1000"), Decimal("5000")):
        plan = plan_protection(size_usd=size, price_impact_pct=Decimal("2.5"),
                               liquidity_usd=Decimal("60000"), rng=RNG)
        tip_usd = (Decimal(plan.tip_lamports) / Decimal(1_000_000_000)
                   * settings.assumed_sol_price_usd)
        assert tip_usd < size * Decimal("0.01"), f"tip {tip_usd} too large for {size}"


def test_protection_can_be_disabled(monkeypatch):
    from asm.config import settings

    monkeypatch.setattr(settings, "mev_protection_enabled", False)
    plan = plan_protection(size_usd=Decimal("5000"), price_impact_pct=Decimal("4"),
                           liquidity_usd=Decimal("30000"), rng=RNG)
    assert not plan.use_private_route
    assert plan.reason == "mev_protection_disabled"


def test_urgent_exit_accepts_worse_pricing_but_still_bounded():
    from asm.config import settings

    normal = plan_protection(size_usd=Decimal("3000"), price_impact_pct=Decimal("3"),
                             liquidity_usd=Decimal("50000"), rng=RNG)
    urgent = plan_protection(size_usd=Decimal("3000"), price_impact_pct=Decimal("3"),
                             liquidity_usd=Decimal("50000"), urgent=True, rng=RNG)
    assert urgent.max_price_impact_pct > normal.max_price_impact_pct
    assert urgent.max_price_impact_pct <= settings.copyability.max_expected_slippage_pct * 2


def test_tip_worth_it_rejects_overpaying():
    """0.01 SOL ($1.50) to protect $100 from a 0.2% move is a bad trade."""
    assert not tip_is_worth_it(10_000_000, Decimal("100"), Decimal("150"), Decimal("0.2"))
    assert tip_is_worth_it(10_000_000, Decimal("10000"), Decimal("150"), Decimal("0.5"))


def test_threshold_is_meaningful():
    assert Decimal(0) < SANDWICH_RISK_THRESHOLD < Decimal(100)
