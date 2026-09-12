"""PRD 39 - wallet relationship graph and cluster detection.

Why this matters commercially (PRD 74.4): a "diversified" portfolio built from five
wallets that are actually one person running five keys is not diversified at all. It is
one position with five times the size and a single point of failure.

Three signals, cheap to compute from data we already store:

  co_trading    the same tokens within a short window, repeatedly
  funding       direct SOL transfers between wallets
  timing        suspiciously similar entry times on the same token

None is conclusive alone. Together, and above a threshold, they justify treating a set
of wallets as a single risk unit.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import combinations

from asm.domain.money import ZERO
from asm.logging import get_logger

log = get_logger(__name__)

CO_TRADE_WINDOW = timedelta(minutes=30)
TIGHT_WINDOW = timedelta(seconds=60)
MIN_SHARED_TOKENS = 3


@dataclass
class TradeEvent:
    wallet: str
    mint: str
    at: datetime


@dataclass
class Relationship:
    wallet_a: str
    wallet_b: str
    relation: str
    strength: Decimal
    evidence: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return tuple(sorted((self.wallet_a, self.wallet_b)))  # type: ignore[return-value]


def detect_co_trading(events: list[TradeEvent]) -> list[Relationship]:
    """Wallets that repeatedly buy the same tokens at nearly the same time."""
    by_mint: dict[str, list[TradeEvent]] = defaultdict(list)
    for e in events:
        by_mint[e.mint].append(e)

    shared: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for mint, group in by_mint.items():
        group.sort(key=lambda e: e.at)
        for a, b in combinations(group, 2):
            if a.wallet == b.wallet:
                continue
            gap = abs((b.at - a.at).total_seconds())
            if timedelta(seconds=gap) <= CO_TRADE_WINDOW:
                pair = (min(a.wallet, b.wallet), max(a.wallet, b.wallet))
                shared[pair].append({"mint": mint, "gap_seconds": int(gap)})

    out: list[Relationship] = []
    for (wa, wb), hits in shared.items():
        mints = {h["mint"] for h in hits}
        if len(mints) < MIN_SHARED_TOKENS:
            continue
        tight = [h for h in hits if h["gap_seconds"] <= TIGHT_WINDOW.total_seconds()]
        # Strength rises with breadth (how many tokens) and tightness (how synchronised).
        breadth = min(Decimal(len(mints)) / Decimal(10), Decimal(1))
        sync = min(Decimal(len(tight)) / Decimal(max(len(hits), 1)), Decimal(1))
        strength = (breadth * Decimal("0.6") + sync * Decimal("0.4")) * Decimal(100)
        out.append(Relationship(
            wallet_a=wa, wallet_b=wb, relation="co_trading",
            strength=strength.quantize(Decimal("0.01")),
            evidence={"shared_tokens": len(mints), "events": len(hits),
                      "tight_events": len(tight),
                      "sample_mints": sorted(mints)[:5]},
        ))
    return out


def detect_funding_links(transfers: list[tuple[str, str, Decimal]]) -> list[Relationship]:
    """Direct value transfers between watched wallets: (from, to, amount_usd)."""
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for src, dst, amount in transfers:
        if src == dst:
            continue
        pair = (min(src, dst), max(src, dst))
        totals[pair] += amount
        counts[pair] += 1

    out: list[Relationship] = []
    for pair, total in totals.items():
        # A funding link is strong evidence; a single transfer already counts for a lot.
        strength = min(Decimal(50) + Decimal(counts[pair]) * Decimal(10), Decimal(100))
        out.append(Relationship(
            wallet_a=pair[0], wallet_b=pair[1], relation="funding",
            strength=strength,
            evidence={"transfers": counts[pair], "total_usd": str(total)},
        ))
    return out


def merge(relationships: list[Relationship]) -> list[Relationship]:
    """Collapse multiple signals for a pair, keeping the strongest and noting the rest."""
    best: dict[tuple[str, str], Relationship] = {}
    for rel in relationships:
        current = best.get(rel.key)
        if current is None or rel.strength > current.strength:
            if current is not None:
                rel.evidence = {**rel.evidence, f"also_{current.relation}": str(current.strength)}
            best[rel.key] = rel
        else:
            current.evidence[f"also_{rel.relation}"] = str(rel.strength)
    return list(best.values())


def find_clusters(relationships: list[Relationship],
                  threshold: Decimal = Decimal("60")) -> list[set[str]]:
    """Union-find over edges above the threshold. Each cluster is ONE risk unit."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for rel in relationships:
        if rel.strength >= threshold:
            union(rel.wallet_a, rel.wallet_b)

    clusters: dict[str, set[str]] = defaultdict(set)
    for wallet in parent:
        clusters[find(wallet)].add(wallet)
    return [c for c in clusters.values() if len(c) > 1]


def cluster_risk_multiplier(cluster_size: int) -> Decimal:
    """PRD 38 - shrink per-wallet allocation when wallets share a cluster.

    Five linked wallets are one bet. Each gets 1/5 of the budget so the cluster in
    aggregate consumes one wallet's worth of risk.
    """
    if cluster_size <= 1:
        return Decimal(1)
    return (Decimal(1) / Decimal(cluster_size)).quantize(Decimal("0.0001"))
