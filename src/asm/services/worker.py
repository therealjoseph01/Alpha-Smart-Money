"""ARQ worker - the cold path (PRD 72 phases A, B, F, G).

Everything here is allowed to take seconds or minutes. Nothing here sits between a
detected trade and a submitted order; that separation is the whole architecture.
"""
from __future__ import annotations

from typing import ClassVar

from arq import cron
from arq.connections import RedisSettings

from asm.config import settings
from asm.logging import get_logger, setup_logging
from asm.services.tasks.backtests import run_backtest
from asm.services.tasks.discovery import (
    discover_wallets,
    maintain_roster,
    refresh_watchlist,
    score_candidates,
)
from asm.services.tasks.emergency import emergency_exit_all
from asm.services.tasks.graph import rebuild_wallet_graph
from asm.services.tasks.intelligence import backfill_wallet, detect_decay, rescore_all
from asm.services.tasks.ops import (
    attribute_performance,
    daily_report,
    reset_daily_counters,
    seed_from_file,
    snapshot_portfolio,
    weekly_review,
)
from asm.services.tasks.research import (
    gate_effectiveness,
    latency_profile,
    research_summary,
    trader_contribution,
)
from asm.services.tasks.retention import apply_retention, retention_report

log = get_logger(__name__)


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def startup(ctx: dict) -> None:
    setup_logging()

    from asm import runtime_config

    await runtime_config.refresh(force=True)
    log.info("arq_worker_started", mode=settings.mode.value)


async def shutdown(ctx: dict) -> None:
    from asm.db.session import close_engine
    from asm.state.client import close_redis

    await close_engine()
    await close_redis()


class WorkerSettings:
    redis_settings = redis_settings()
    functions: ClassVar[list] = [
        backfill_wallet,
        rescore_all,
        detect_decay,
        snapshot_portfolio,
        attribute_performance,
        daily_report,
        reset_daily_counters,
        seed_from_file,
        emergency_exit_all,
        run_backtest,
        rebuild_wallet_graph,
        apply_retention,
        retention_report,
        research_summary,
        gate_effectiveness,
        latency_profile,
        trader_contribution,
        weekly_review,
        discover_wallets,
        refresh_watchlist,
        score_candidates,
        maintain_roster,
    ]
    cron_jobs: ClassVar[list] = [
        # equity curve
        cron(snapshot_portfolio, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        # PRD 36 - adaptive re-scoring. Once daily, and it skips wallets with no new
        # trades: each page of history is a 100-credit Enhanced API call.
        cron(rescore_all, hour=4, minute=17),
        # PRD 37 - strategy decay
        cron(detect_decay, hour={3, 15}, minute=23),
        # PRD 34 - attribution feeds the next re-score
        cron(attribute_performance, minute={7, 37}),
        # PRD 23 - daily loss breaker resets at UTC midnight
        cron(reset_daily_counters, hour=0, minute=0),
        # PRD 67
        cron(daily_report, hour=7, minute=0),
        # PRD 11 - the discovery funnel, at three different speeds.
        #
        # Discovery is FREE (GMGN has no monthly quota, only a rate limit), so it runs
        # often - every four hours catches wallets across time zones that a single
        # daily snapshot would miss entirely.
        cron(discover_wallets, hour={1, 5, 9, 13, 17, 21}, minute=5),
        # Scoring is EXPENSIVE (~300 Helius credits per wallet), so it is rationed to a
        # daily budget and drains the screened queue in order of promise.
        cron(score_candidates, hour=6, minute=20),
        # Ranking is free and decides who actually gets copied.
        cron(maintain_roster, hour={6, 18}, minute=45),
        # Retire wallets that have gone quiet; refresh GMGN metadata.
        cron(refresh_watchlist, hour=7, minute=35),
        # PRD 39 - wallet graph / cluster detection
        cron(rebuild_wallet_graph, hour=4, minute=11),
        # PRD 69 - data retention
        cron(apply_retention, hour=5, minute=30),
        # PRD 68 - weekly strategy review (Mondays)
        cron(weekly_review, weekday=0, hour=8, minute=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 600
    keep_result = 3600
