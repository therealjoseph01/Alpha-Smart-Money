"""PRD 13, 21, 30 - scoring, sizing, harvesting."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from asm.domain.enums import TraderStatus
from asm.domain.models import PortfolioState
from asm.engine.harvest import evaluate_harvest
from asm.engine.scoring import (
    TradeOutcome,
    TraderStats,
    compute_scores,
    consistency_score,
    copyability_score,
    latency_tolerance_ms,
    next_status,
)
from asm.engine.sizing import SizingMode, base_size, final_size
from tests import factories as F


# ------------------------------------------------------------------ harvest
def test_no_harvest_below_threshold():
    d = evaluate_harvest(equity=Decimal("11000"), cash=Decimal("11000"),
                         harvest_baseline=Decimal("10000"))
    assert not d.should_harvest   # +10%, threshold is +25%


def test_harvest_takes_configured_share_of_profit():
    """PRD 30.2 - +$2500 on a $10k base, take 40% => $1000 out, $1500 stays working."""
    d = evaluate_harvest(equity=Decimal("12500"), cash=Decimal("12500"),
                         harvest_baseline=Decimal("10000"))
    assert d.should_harvest
    assert d.amount_usd == Decimal("1000.00")
    assert d.retained_usd == Decimal("1500.00")
    assert d.new_hwm == Decimal("11500")


def test_harvest_limited_by_free_cash():
    """Deployed capital is in positions; only free cash can actually leave."""
    d = evaluate_harvest(equity=Decimal("12500"), cash=Decimal("300"),
                         harvest_baseline=Decimal("10000"))
    assert d.should_harvest
    assert d.amount_usd == Decimal("300.00")


def test_harvest_respects_minimum_active_capital():
    """PRD 30.5 - never strip the account below its working floor."""
    d = evaluate_harvest(equity=Decimal("700"), cash=Decimal("700"),
                         harvest_baseline=Decimal("500"),
                         min_active_usd=Decimal("600"))
    assert d.amount_usd <= Decimal("100")


def test_no_harvest_without_free_cash():
    d = evaluate_harvest(equity=Decimal("12500"), cash=Decimal("0"),
                         harvest_baseline=Decimal("10000"))
    assert not d.should_harvest
    assert d.reason == "insufficient_free_cash"


# ------------------------------------------------------------------- sizing
def portfolio(equity=Decimal("10000"), cash=None) -> PortfolioState:
    return PortfolioState(cash_usd=cash if cash is not None else equity,
                          high_water_mark_usd=equity)


def test_confidence_weighted_size_scales_with_scores():
    strong = F.candidate(trader=F.trader(scores_kw={
        "composite": Decimal("95"), "confidence": Decimal("95"), "copyability": Decimal("95")}))
    weak = F.candidate(trader=F.trader(scores_kw={
        "composite": Decimal("60"), "confidence": Decimal("55"), "copyability": Decimal("55")}))
    s_strong = base_size(strong, portfolio(), SizingMode.CONFIDENCE_WEIGHTED)
    s_weak = base_size(weak, portfolio(), SizingMode.CONFIDENCE_WEIGHTED)
    assert s_strong > s_weak * Decimal("1.5")


def test_hard_position_cap_is_never_exceeded():
    """PRD 4 - no autonomous change to hard limits, whatever the multipliers say."""
    c = F.candidate(confirmations=["a", "b"])
    size, pct = final_size(c, portfolio(), Decimal("10"), SizingMode.PORTFOLIO_PCT)
    assert pct <= Decimal("2.0")
    assert size <= Decimal("200")


def test_size_cannot_exceed_available_cash():
    c = F.candidate()
    size, _ = final_size(c, portfolio(equity=Decimal("10000"), cash=Decimal("25")),
                         Decimal(1), SizingMode.PORTFOLIO_PCT)
    assert size <= Decimal("25")


def test_dust_size_returns_zero():
    c = F.candidate()
    size, pct = final_size(c, portfolio(equity=Decimal("100")), Decimal("0.01"))
    assert size == Decimal(0)
    assert pct == Decimal(0)


def test_kelly_is_fractional_not_full():
    """Full Kelly on memecoin distributions is ruin-seeking."""
    c = F.candidate(trader=F.trader(
        win_rate=Decimal("70"), trade_count=200,
        notes={"avg_win_pct": 100, "avg_loss_pct": 30},
    ))
    size = base_size(c, portfolio(), SizingMode.KELLY)
    assert size <= Decimal("10000") * Decimal("0.02")


def test_source_proportional_tracks_conviction():
    big = F.candidate(source_trade=F.source_trade(source_value_usd=Decimal("2000")),
                      trader=F.trader(avg_trade_size_usd=Decimal("500")))
    small = F.candidate(source_trade=F.source_trade(source_value_usd=Decimal("100")),
                        trader=F.trader(avg_trade_size_usd=Decimal("500")))
    assert (base_size(big, portfolio(), SizingMode.SOURCE_PROPORTIONAL)
            > base_size(small, portfolio(), SizingMode.SOURCE_PROPORTIONAL))


# ------------------------------------------------------------------ scoring
def outcome(src, copy=None, real=None, days_ago=1, latency_ms=3000) -> TradeOutcome:
    opened = datetime.now(UTC) - timedelta(days=days_ago)
    return TradeOutcome(
        opened_at=opened, closed_at=opened + timedelta(hours=2),
        source_return_pct=Decimal(str(src)),
        copyable_return_pct=Decimal(str(copy)) if copy is not None else None,
        realized_return_pct=Decimal(str(real)) if real is not None else None,
        detection_latency_ms=latency_ms,
    )


def test_one_moonshot_is_punished_by_consistency():
    """PRD 2 - a wallet whose P&L is one trade is not copyable."""
    lucky = TraderStats("w", [outcome(5000)] + [outcome(-8) for _ in range(19)])
    steady = TraderStats("w", [outcome(12) for _ in range(12)] + [outcome(-6) for _ in range(8)])
    assert consistency_score(steady) > consistency_score(lucky)


def test_copyability_reflects_edge_retention():
    """Same source returns; the trader whose edge survives latency scores higher."""
    survives = TraderStats("w", [outcome(40, 34) for _ in range(20)])
    evaporates = TraderStats("w", [outcome(40, 2) for _ in range(20)])
    assert copyability_score(survives) > copyability_score(evaporates) + Decimal(20)


def test_uncopyable_trader_scores_low_despite_great_source_pnl():
    """The central thesis: profitable != profitable to copy."""
    stats = TraderStats(
        "w", [outcome(120, -3) for _ in range(30)],
        first_trade_at=datetime.now(UTC) - timedelta(days=120),
        last_trade_at=datetime.now(UTC),
    )
    scores = compute_scores(stats)
    assert scores.performance > Decimal("70")
    assert scores.copyability < Decimal("30")
    assert scores.composite < Decimal("60"), "composite must follow copyability, not source P&L"


def test_live_replication_dominates_once_it_has_a_sample():
    theory_only = TraderStats(
        "w", [outcome(50, 45) for _ in range(30)],
        first_trade_at=datetime.now(UTC) - timedelta(days=90), last_trade_at=datetime.now(UTC))
    with_bad_live = TraderStats(
        "w", [outcome(50, 45, -12) for _ in range(30)],
        first_trade_at=datetime.now(UTC) - timedelta(days=90), last_trade_at=datetime.now(UTC))
    assert compute_scores(with_bad_live).composite < compute_scores(theory_only).composite


def test_low_sample_drags_composite_down():
    """PRD 5.3 - capital is earned. Three good trades is not evidence."""
    tiny = TraderStats("w", [outcome(60, 50) for _ in range(3)],
                       first_trade_at=datetime.now(UTC) - timedelta(days=3),
                       last_trade_at=datetime.now(UTC))
    big = TraderStats("w", [outcome(60, 50) for _ in range(80)],
                      first_trade_at=datetime.now(UTC) - timedelta(days=120),
                      last_trade_at=datetime.now(UTC))
    assert compute_scores(tiny).composite < compute_scores(big).composite
    assert compute_scores(tiny).confidence < Decimal("50")


def test_latency_tolerance_found_where_edge_dies():
    fast = [outcome(30, 25, latency_ms=500) for _ in range(15)]
    slow = [outcome(30, -5, latency_ms=9000) for _ in range(15)]
    tol = latency_tolerance_ms(TraderStats("w", fast + slow))
    assert tol is not None
    assert tol >= 500


def test_promotion_requires_score_and_confidence():
    good = compute_scores(TraderStats(
        "w", [outcome(45, 38) for _ in range(80)],
        first_trade_at=datetime.now(UTC) - timedelta(days=120),
        last_trade_at=datetime.now(UTC)))
    assert next_status(TraderStatus.DISCOVERED, good) is TraderStatus.PROBATION
    assert next_status(TraderStatus.PROBATION, good, live_trades=25) is TraderStatus.ACTIVE


def test_bad_scores_degrade_an_active_trader():
    bad = compute_scores(TraderStats(
        "w", [outcome(-20, -30) for _ in range(40)],
        first_trade_at=datetime.now(UTC) - timedelta(days=60),
        last_trade_at=datetime.now(UTC)))
    assert next_status(TraderStatus.ACTIVE, bad) in (
        TraderStatus.DEGRADED, TraderStatus.SUSPENDED)


def test_blacklist_is_never_auto_reversed():
    good = compute_scores(TraderStats(
        "w", [outcome(50, 45) for _ in range(80)],
        first_trade_at=datetime.now(UTC) - timedelta(days=120),
        last_trade_at=datetime.now(UTC)))
    assert next_status(TraderStatus.BLACKLISTED, good) is TraderStatus.BLACKLISTED
