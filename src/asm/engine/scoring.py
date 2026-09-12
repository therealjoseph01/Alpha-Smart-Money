"""PRD 13-15, 36 - trader scoring.

The thesis of the product lives here: source profitability and *copyable* profitability
are different numbers, and only the second one should move capital.

Score components, 0-100:
  performance      raw realized edge
  consistency      is the P&L spread across trades or one lucky moonshot
  longevity        long enough a track record to mean anything
  risk_adjusted    return per unit of drawdown
  copyability      what a follower with our latency could have captured
  live_replication what we ACTUALLY captured once we started copying (PRD 13.6)

Composite weighting deliberately dominates on the last two: once live_replication has
a sample, it overrides everything else. That is PRD 5.1 expressed as arithmetic.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from asm.domain.models import TraderScores
from asm.domain.money import ZERO, clamp, safe_div

HUNDRED = Decimal(100)


@dataclass
class TradeOutcome:
    """One round trip by the source wallet, with our copyable estimate alongside."""

    opened_at: datetime
    closed_at: datetime | None
    source_return_pct: Decimal
    copyable_return_pct: Decimal | None = None
    realized_return_pct: Decimal | None = None   # what WE actually got
    size_usd: Decimal = ZERO
    liquidity_usd: Decimal = ZERO
    detection_latency_ms: int | None = None

    @property
    def hold_seconds(self) -> int:
        if not self.closed_at:
            return 0
        return int((self.closed_at - self.opened_at).total_seconds())


@dataclass
class TraderStats:
    wallet: str
    outcomes: list[TradeOutcome] = field(default_factory=list)
    first_trade_at: datetime | None = None
    last_trade_at: datetime | None = None


def _pctl(values: list[Decimal], p: float) -> Decimal:
    if not values:
        return ZERO
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(len(s) * p)))
    return s[idx]


def performance_score(stats: TraderStats) -> Decimal:
    """13.1 - median return per trade, not mean. The mean is one moonshot away from a lie."""
    rets = [o.source_return_pct for o in stats.outcomes if o.closed_at]
    if not rets:
        return ZERO
    median = Decimal(str(statistics.median(float(r) for r in rets)))
    # 0% median -> 40, +50% median -> 90. Saturating, so absurd numbers do not dominate.
    return clamp(Decimal(40) + median * Decimal("1.0"), ZERO, HUNDRED)


def consistency_score(stats: TraderStats) -> Decimal:
    """13.2 - penalise P&L concentrated in a single trade."""
    wins = [o.source_return_pct for o in stats.outcomes
            if o.closed_at and o.source_return_pct > 0]
    rets = [o.source_return_pct for o in stats.outcomes if o.closed_at]
    if len(rets) < 5:
        return Decimal(30)

    total_win = sum(wins, ZERO)
    top_share = safe_div(max(wins, default=ZERO), total_win, ZERO)
    win_rate = Decimal(len([r for r in rets if r > 0])) / Decimal(len(rets))

    # A single trade being >50% of all winnings is the classic uncopyable profile.
    concentration_penalty = clamp(top_share * Decimal(120), ZERO, Decimal(60))
    return clamp(win_rate * HUNDRED - concentration_penalty + Decimal(20), ZERO, HUNDRED)


def longevity_score(stats: TraderStats) -> Decimal:
    """13.3 - a three-day leaderboard run is not a track record."""
    if not stats.first_trade_at:
        return ZERO
    days = (datetime.now(UTC) - stats.first_trade_at).days
    n = len(stats.outcomes)
    day_score = clamp(Decimal(days) / Decimal(90) * HUNDRED, ZERO, HUNDRED)
    count_score = clamp(Decimal(n) / Decimal(100) * HUNDRED, ZERO, HUNDRED)
    return (day_score + count_score) / Decimal(2)


def risk_adjusted_score(stats: TraderStats) -> Decimal:
    """13.4 - return per unit of downside. Sortino-flavoured, not Sharpe."""
    rets = [float(o.source_return_pct) for o in stats.outcomes if o.closed_at]
    if len(rets) < 5:
        return Decimal(30)
    mean = statistics.mean(rets)
    downside = [r for r in rets if r < 0]
    if not downside:
        return Decimal(85)
    dev = statistics.pstdev(downside) or 1.0
    ratio = Decimal(str(mean / dev))
    return clamp(Decimal(50) + ratio * Decimal(25), ZERO, HUNDRED)


def copyability_score(stats: TraderStats) -> Decimal:
    """13.5 - THE score. What fraction of the source's edge survives our latency?"""
    pairs = [
        (o.source_return_pct, o.copyable_return_pct)
        for o in stats.outcomes
        if o.closed_at and o.copyable_return_pct is not None
    ]
    if len(pairs) < 5:
        return Decimal(35)  # unproven, not zero - it still needs a chance to earn

    ratios = [
        float(safe_div(copy, src, ZERO))
        for src, copy in pairs
        if src > 0
    ]
    if not ratios:
        return Decimal(20)

    retention = Decimal(str(statistics.median(ratios)))
    profitable = Decimal(len([c for _s, c in pairs if c and c > 0])) / Decimal(len(pairs))

    # Retaining 80% of source edge is excellent; 30% is marginal; negative is a trap.
    retention_score = clamp(retention * Decimal(90), ZERO, HUNDRED)
    return clamp(retention_score * Decimal("0.7") + profitable * HUNDRED * Decimal("0.3"),
                 ZERO, HUNDRED)


def live_replication_score(stats: TraderStats) -> Decimal | None:
    """13.6 - what we actually captured. Overrides theory once it has a sample."""
    realized = [o.realized_return_pct for o in stats.outcomes if o.realized_return_pct is not None]
    if len(realized) < 5:
        return None
    median = Decimal(str(statistics.median(float(r) for r in realized)))
    win_rate = Decimal(len([r for r in realized if r > 0])) / Decimal(len(realized))
    return clamp(Decimal(45) + median + win_rate * Decimal(20), ZERO, HUNDRED)


def confidence_score(stats: TraderStats) -> Decimal:
    """PRD 14 - how much we trust the scores above.

    Sample size is a MULTIPLIER, not a weighted term. Recency and completeness can only
    ever discount confidence, never manufacture it: a wallet with three recent trades
    has three trades of evidence, however fresh they are. Getting this wrong would let
    a three-trade wallet clear the promotion floor on recency alone.
    """
    n = len(stats.outcomes)
    if n == 0:
        return ZERO

    evidence = clamp(Decimal(n) / Decimal(50), ZERO, Decimal(1))

    recency = HUNDRED
    if stats.last_trade_at:
        idle_days = (datetime.now(UTC) - stats.last_trade_at).days
        recency = clamp(HUNDRED - Decimal(idle_days) * Decimal(7), ZERO, HUNDRED)

    closed = len([o for o in stats.outcomes if o.closed_at])
    completeness = clamp(Decimal(closed) / Decimal(max(n, 1)) * HUNDRED, ZERO, HUNDRED)

    quality = recency * Decimal("0.6") + completeness * Decimal("0.4")
    return (evidence * quality).quantize(Decimal("0.01"))


def compute_scores(stats: TraderStats) -> TraderScores:
    perf = performance_score(stats)
    cons = consistency_score(stats)
    longev = longevity_score(stats)
    risk = risk_adjusted_score(stats)
    copy = copyability_score(stats)
    live = live_replication_score(stats)
    conf = confidence_score(stats)

    if live is not None:
        # PRD 5.1: observed replication dominates everything theoretical.
        composite = (live * Decimal("0.45") + copy * Decimal("0.25")
                     + risk * Decimal("0.12") + cons * Decimal("0.10")
                     + perf * Decimal("0.05") + longev * Decimal("0.03"))
    else:
        composite = (copy * Decimal("0.35") + risk * Decimal("0.20")
                     + cons * Decimal("0.20") + perf * Decimal("0.15")
                     + longev * Decimal("0.10"))

    # Low confidence drags the composite down rather than being reported separately -
    # PRD 5.3, capital is earned before it is scaled.
    composite = composite * (Decimal("0.5") + conf / HUNDRED * Decimal("0.5"))

    return TraderScores(
        performance=perf.quantize(Decimal("0.01")),
        consistency=cons.quantize(Decimal("0.01")),
        longevity=longev.quantize(Decimal("0.01")),
        risk_adjusted=risk.quantize(Decimal("0.01")),
        copyability=copy.quantize(Decimal("0.01")),
        live_replication=live.quantize(Decimal("0.01")) if live is not None else None,
        composite=composite.quantize(Decimal("0.01")),
        confidence=conf,
        sample_size=len(stats.outcomes),
        computed_at=datetime.now(UTC),
    )


def latency_tolerance_ms(stats: TraderStats) -> int | None:
    """PRD 17.2 - learn how long this trader's edge survives.

    Bucket outcomes by detection latency and find where copyable return goes negative.
    A scalper and a swing trader need different gates; this derives which is which.
    """
    samples = [
        (o.detection_latency_ms, o.copyable_return_pct)
        for o in stats.outcomes
        if o.detection_latency_ms is not None and o.copyable_return_pct is not None
    ]
    if len(samples) < 20:
        return None
    samples.sort(key=lambda x: x[0])
    window = max(5, len(samples) // 5)
    for i in range(0, len(samples) - window, max(1, window // 2)):
        chunk = samples[i:i + window]
        median = statistics.median(float(c[1]) for c in chunk)
        if median <= 0:
            return chunk[0][0]
    return samples[-1][0]


def median_hold_seconds(stats: TraderStats) -> int | None:
    holds = [o.hold_seconds for o in stats.outcomes if o.closed_at and o.hold_seconds > 0]
    if not holds:
        return None
    return int(statistics.median(holds))


# ---------------------------------------------------------------- lifecycle
def next_status(current, scores: TraderScores, live_trades: int = 0):
    """PRD 15/36 - promotion and degradation are automatic and evidence-driven."""
    from asm.domain.enums import TraderStatus as S

    if current in (S.BLACKLISTED, S.SUSPENDED):
        return current

    if scores.composite >= 70 and scores.confidence >= 60:
        if current in (S.DISCOVERED, S.ANALYZING, S.CANDIDATE):
            return S.PROBATION
        if current is S.PROBATION and live_trades >= 20:
            return S.ACTIVE
        if current is S.DEGRADED and scores.composite >= 75:
            return S.PROBATION
        return current if current is S.ACTIVE else S.PROBATION

    if scores.composite < 45 and current.is_followed:
        return S.DEGRADED
    if scores.composite < 30:
        return S.SUSPENDED
    if current is S.DISCOVERED and scores.sample_size > 0:
        return S.CANDIDATE
    return current
