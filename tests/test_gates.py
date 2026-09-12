"""PRD 17-20. Gates are pure, so these are the cheapest high-value tests in the repo."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from asm.config import settings
from asm.domain.enums import Reason, TraderStatus
from asm.engine import gates as G
from tests import factories as F

CFG = settings.copyability


# ------------------------------------------------------------------ 17.1
@pytest.mark.parametrize(
    ("current_price", "expect_pass", "expect_mult"),
    [
        (Decimal("1.005"), True, Decimal(1)),      # +0.5% - normal
        (Decimal("1.02"), True, Decimal(1)),       # +2%   - acceptable
        (Decimal("1.04"), True, Decimal("0.5")),   # +4%   - reduced size
        (Decimal("1.07"), True, Decimal("0.25")),  # +7%   - high caution
        (Decimal("1.15"), False, Decimal(0)),      # +15%  - reject
    ],
)
def test_price_chase_ladder(current_price, expect_pass, expect_mult):
    c = F.candidate(market=F.market(price=current_price, price_usd=current_price))
    r = G.price_chase_gate(c, CFG)
    assert r.passed is expect_pass
    assert r.size_mult == expect_mult


def test_price_chase_ignores_favourable_move():
    """Price moving DOWN after a source buy is a better entry, not a chase."""
    c = F.candidate(market=F.market(price=Decimal("0.90"), price_usd=Decimal("0.90")))
    r = G.price_chase_gate(c, CFG)
    assert r.passed
    assert r.size_mult == Decimal(1)


# ------------------------------------------------------------------ 17.2
def test_stale_signal_rejected():
    old = datetime.now(UTC) - timedelta(seconds=60)
    c = F.candidate(source_trade=F.source_trade(source_confirmed_at=old))
    r = G.latency_gate(c, CFG)
    assert not r.passed
    assert Reason.SIGNAL_TOO_OLD in r.reasons


def test_trader_latency_tolerance_respected():
    """A scalper whose edge dies in 1s must not be copied at 2s."""
    c = F.candidate(trader=F.trader(latency_tolerance_ms=1000))
    r = G.latency_gate(c, CFG)
    assert not r.passed
    assert Reason.LATENCY_EXCEEDS_TRADER_TOLERANCE in r.reasons


def test_latency_decays_size_near_tolerance():
    c = F.candidate(trader=F.trader(latency_tolerance_ms=3000))
    r = G.latency_gate(c, CFG)
    assert r.passed
    assert r.size_mult == Decimal("0.6")


# ------------------------------------------------------------------ 17.3
def test_thin_liquidity_rejected():
    c = F.candidate(market=F.market(liquidity_usd=Decimal("5000")))
    r = G.liquidity_gate(c, CFG, Decimal("100"))
    assert not r.passed
    assert Reason.LOW_LIQUIDITY in r.reasons


def test_oversized_position_shrinks_to_fit_book():
    """We do not reject a good trade for being too big - we make it smaller."""
    c = F.candidate(market=F.market(liquidity_usd=Decimal("50000")))
    r = G.liquidity_gate(c, CFG, Decimal("2000"))  # 4% of book, cap is 1%
    assert r.passed
    assert r.size_mult == Decimal("500") / Decimal("2000")


# ------------------------------------------------------------------ 17.4
def test_high_price_impact_rejected():
    c = F.candidate(quote=F.quote(price_impact_pct=Decimal("8")))
    r = G.slippage_gate(c, CFG)
    assert not r.passed
    assert Reason.EXPECTED_SLIPPAGE_TOO_HIGH in r.reasons


def test_alpha_below_cost_rejected():
    """PRD 17.4 - the trade is only worth taking if the edge exceeds the friction."""
    c = F.candidate(
        trader=F.trader(notes={"expected_edge_pct": "2"}),
        quote=F.quote(price_impact_pct=Decimal("1.0")),
    )
    r = G.slippage_gate(c, CFG)
    assert not r.passed
    assert Reason.ALPHA_BELOW_COST in r.reasons


def test_missing_route_rejected():
    c = F.candidate(quote=None)
    r = G.slippage_gate(c, CFG)
    assert not r.passed
    assert Reason.NO_ROUTE in r.reasons


# ------------------------------------------------------------------ 17.6/18
def test_brand_new_token_rejected():
    c = F.candidate(market=F.market(token_age_seconds=30))
    r = G.token_age_gate(c, CFG)
    assert not r.passed
    assert Reason.TOKEN_TOO_NEW in r.reasons


def test_young_token_halves_size():
    c = F.candidate(market=F.market(token_age_seconds=900))
    r = G.token_age_gate(c, CFG)
    assert r.passed
    assert r.size_mult == CFG.new_token_size_mult


def test_unknown_token_risk_fails_closed():
    """PRD 53 - missing validation data must never read as 'safe'."""
    c = F.candidate(token_risk=F.token_risk(unknown=True))
    r = G.token_safety_gate(c, CFG)
    assert not r.passed
    assert Reason.CRITICAL_DATA_UNAVAILABLE in r.reasons


def test_honeypot_rejected():
    c = F.candidate(token_risk=F.token_risk(score=5, is_honeypot=True))
    r = G.token_safety_gate(c, CFG)
    assert not r.passed


def test_insider_flags_reject():
    c = F.candidate(token_risk=F.token_risk(insider_flags=["bought_within_2min_of_launch"]))
    r = G.token_safety_gate(c, CFG)
    assert not r.passed
    assert Reason.INSIDER_PATTERN in r.reasons


# ------------------------------------------------------------------ trader
def test_unfollowed_trader_rejected():
    c = F.candidate(trader=F.trader(status=TraderStatus.CANDIDATE))
    r = G.trader_gate(c, CFG)
    assert not r.passed
    assert Reason.TRADER_NOT_FOLLOWED in r.reasons


def test_low_score_trader_rejected():
    c = F.candidate(trader=F.trader(scores_kw={"composite": Decimal("40")}))
    r = G.trader_gate(c, CFG)
    assert not r.passed
    assert Reason.TRADER_SCORE_TOO_LOW in r.reasons


# ------------------------------------------------------------------ 20
def test_multi_trader_confirmation_boosts_size():
    c = F.candidate(confirmations=["w1", "w2"])
    r = G.confirmation_gate(c, CFG)
    assert r.passed
    assert r.size_mult == Decimal("1.25")


# ------------------------------------------------------------------ pipeline
def test_clean_candidate_passes_all_gates():
    c = F.candidate()
    results, passed, mult = G.run_entry_gates(c, Decimal("100"))
    assert passed, [r.name for r in results if not r.passed]
    assert mult == Decimal(1)


def test_all_gates_run_even_after_first_failure():
    """PRD 41 shows several reason codes on a rejection - we need the full picture."""
    c = F.candidate(
        market=F.market(price=Decimal("1.5"), price_usd=Decimal("1.5"),
                        liquidity_usd=Decimal("1000"), token_age_seconds=10),
    )
    results, passed, _ = G.run_entry_gates(c, Decimal("100"))
    assert not passed
    failed = {r.name for r in results if not r.passed}
    assert {"price_chase", "token_age", "liquidity"} <= failed
    assert len(results) == len(G.ENTRY_GATES) + 1


def test_multipliers_compound():
    c = F.candidate(
        market=F.market(price=Decimal("1.04"), price_usd=Decimal("1.04"),
                        token_age_seconds=900),
    )
    _results, passed, mult = G.run_entry_gates(c, Decimal("100"))
    assert passed
    assert mult == Decimal("0.5") * Decimal("0.5")
