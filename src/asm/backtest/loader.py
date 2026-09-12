"""Turn on-chain history into backtest inputs (PRD 44).

Price history is reconstructed from the trades themselves: every swap by any watched
wallet is a real, executed print at a real price. That is a sparse and biased sample -
it only has prices at moments someone traded - which is exactly why the provider marks
old points stale and the gates reject stale data rather than interpolating.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from asm.adapters.helius import helius
from asm.backtest.provider import PricePoint, TokenHistory
from asm.db import repo
from asm.db.session import session_scope
from asm.domain.models import SourceTrade, TraderProfile
from asm.domain.money import ZERO
from asm.ingest.parser import parse_helius_enhanced
from asm.logging import get_logger

log = get_logger(__name__)

# Depth is not in the trade record, so it is inferred from observed trade sizes.
# Deliberately conservative: assume the book is only a few multiples of typical flow.
LIQUIDITY_MULTIPLE = Decimal("40")
MIN_ASSUMED_LIQUIDITY = Decimal("5000")


async def load_from_chain(wallets: list[str], *, pages: int = 10,
                          sol_price: Decimal = Decimal("150")) -> tuple[list[SourceTrade], dict[str, TokenHistory]]:
    h = helius()
    all_trades: list[SourceTrade] = []

    for wallet in wallets:
        rows: list[dict] = []
        before: str | None = None
        for _ in range(pages):
            page = await h.wallet_history(wallet, limit=100, before=before)
            if not page:
                break
            rows.extend(page)
            before = page[-1].get("signature")
        parsed = [t for t in (parse_helius_enhanced(r, wallet) for r in rows) if t]
        all_trades.extend(parsed)
        log.info("backtest_loaded_wallet", wallet=wallet[:8], trades=len(parsed))

    return all_trades, build_histories(all_trades, sol_price=sol_price)


def build_histories(trades: list[SourceTrade], *,
                    sol_price: Decimal = Decimal("150")) -> dict[str, TokenHistory]:
    """Every observed swap becomes a price point for that token."""
    by_mint: dict[str, list[SourceTrade]] = defaultdict(list)
    for t in trades:
        if t.source_price > 0:
            by_mint[t.token_mint].append(t)

    histories: dict[str, TokenHistory] = {}
    for mint, group in by_mint.items():
        group.sort(key=lambda t: t.source_confirmed_at)
        sizes = [t.source_value_usd for t in group if t.source_value_usd > 0]
        typical = (sum(sizes, ZERO) / Decimal(len(sizes))) if sizes else ZERO
        liquidity = max(typical * LIQUIDITY_MULTIPLE, MIN_ASSUMED_LIQUIDITY)

        points = [
            PricePoint(
                at=t.source_confirmed_at,
                price_usd=t.source_price * sol_price,
                liquidity_usd=liquidity,
            )
            for t in group
        ]
        histories[mint] = TokenHistory(
            mint=mint, points=points,
            decimals=group[0].token_decimals,
            created_at=group[0].source_confirmed_at,
        )
    log.info("backtest_histories_built", tokens=len(histories))
    return histories


async def load_traders(wallets: list[str]) -> dict[str, TraderProfile]:
    """Use each wallet's CURRENT scored profile.

    Note the bias this introduces: scores were computed with knowledge of the whole
    history, so a backtest using them is mildly optimistic about trader selection. It
    tests the execution and risk path honestly; treat the trader-picking half as
    in-sample until walk-forward scoring exists.
    """
    out: dict[str, TraderProfile] = {}
    async with session_scope() as s:
        for wallet in wallets:
            row = await repo.get_trader(s, wallet)
            if row is not None:
                out[wallet] = repo.trader_to_domain(row)
    return out
