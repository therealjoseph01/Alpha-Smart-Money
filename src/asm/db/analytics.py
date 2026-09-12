"""Aggregations that SQL dialects disagree about.

PostgreSQL has `unnest()` and `percentile_cont()`; SQLite has neither. Rather than
maintain two query paths, these compute in Python.

That is a deliberate trade. At the row counts involved - a few thousand decisions a day -
pulling the column and aggregating in the process costs single-digit milliseconds, and
one implementation that behaves identically everywhere is worth more than the speed. If
the dataset ever outgrows it, the fix is a materialized daily rollup, not dialect
branching.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from asm.db import models as M


async def rejection_counts(session: AsyncSession, *, since: datetime, mode: str,
                           limit: int | None = None) -> list[dict[str, Any]]:
    """Reason-code frequency across rejected decisions."""
    rows = (await session.execute(
        select(M.TradeDecision.reason_codes).where(
            M.TradeDecision.decided_at >= since,
            M.TradeDecision.decision == "reject",
            M.TradeDecision.mode == mode,
        )
    )).scalars().all()

    counter: Counter[str] = Counter()
    for codes in rows:
        counter.update(codes or [])
    items = [{"code": c, "count": n} for c, n in counter.most_common()]
    return items[:limit] if limit else items


def _pct(values: list[float], q: float) -> int | None:
    """Linear-interpolated percentile, matching percentile_cont."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return round(s[0])
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return round(s[lo] + (s[hi] - s[lo]) * frac)


async def latency_percentiles(session: AsyncSession, *, since: datetime,
                              mode: str) -> dict[str, Any]:
    """PRD 17.2 - where the copy latency actually goes, stage by stage."""
    rows = (await session.execute(
        select(M.TradeDecision.observation_latency_ms,
               M.TradeDecision.analysis_latency_ms,
               M.TradeDecision.total_copy_latency_ms)
        .where(M.TradeDecision.decided_at >= since, M.TradeDecision.mode == mode)
    )).all()

    obs = [float(r[0]) for r in rows if r[0] is not None]
    ana = [float(r[1]) for r in rows if r[1] is not None]
    tot = [float(r[2]) for r in rows if r[2] is not None]

    return {
        "samples": len(rows),
        "observation_ms": {"p50": _pct(obs, 0.5), "p95": _pct(obs, 0.95)},
        "analysis_ms": {"p50": _pct(ana, 0.5), "p95": _pct(ana, 0.95)},
        "total_copy_ms": {"p50": _pct(tot, 0.5), "p95": _pct(tot, 0.95)},
    }


async def upsert(session: AsyncSession, model, *, match: dict[str, Any],
                 values: dict[str, Any]):
    """Dialect-agnostic upsert.

    PostgreSQL's ON CONFLICT and SQLite's differ enough that a shared helper is clearer
    than two code paths. At these write rates a select-then-write is fine; the atomic
    guarantees that matter live in the Redis Lua script, not here.
    """
    conditions = [getattr(model, k) == v for k, v in match.items()]
    row = (await session.execute(select(model).where(*conditions))).scalar_one_or_none()
    if row is None:
        row = model(**match, **values)
        session.add(row)
        await session.flush()
        return row, True
    for k, v in values.items():
        setattr(row, k, v)
    return row, False
