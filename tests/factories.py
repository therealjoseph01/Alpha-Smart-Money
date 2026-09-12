"""Builders for domain objects so tests read as scenarios, not as constructor soup."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from asm.domain.enums import Action, TraderStatus
from asm.domain.models import (
    MarketState,
    Quote,
    SourceTrade,
    TokenRisk,
    TradeCandidate,
    TraderProfile,
    TraderScores,
)
from asm.domain.money import SOL_MINT

MINT = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WALLET = "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW"


def trader(**kw) -> TraderProfile:
    defaults = dict(
        wallet=WALLET,
        status=TraderStatus.ACTIVE,
        scores=TraderScores(
            composite=Decimal("85"), confidence=Decimal("80"),
            copyability=Decimal("75"), sample_size=50,
        ),
        size_multiplier=Decimal(1),
        avg_trade_size_usd=Decimal("500"),
        win_rate=Decimal("55"),
        trade_count=120,
    )
    scores_override = kw.pop("scores_kw", None)
    if scores_override:
        defaults["scores"] = TraderScores(**{
            "composite": Decimal("85"), "confidence": Decimal("80"),
            "copyability": Decimal("75"), "sample_size": 50, **scores_override,
        })
    return TraderProfile(**(defaults | kw))


def source_trade(**kw) -> SourceTrade:
    now = datetime.now(UTC)
    defaults = dict(
        wallet=WALLET, signature="sig123", action=Action.BUY, token_mint=MINT,
        quote_mint=SOL_MINT, token_amount_raw=1_000_000_000,
        quote_amount_raw=500_000_000, token_decimals=9, quote_decimals=9,
        source_price=Decimal("1.00"), source_value_usd=Decimal("500"),
        source_confirmed_at=now - timedelta(seconds=2), detected_at=now,
    )
    return SourceTrade(**(defaults | kw))


def market(**kw) -> MarketState:
    defaults = dict(
        mint=MINT, price=Decimal("1.00"), price_usd=Decimal("1.00"),
        liquidity_usd=Decimal("250000"), market_cap_usd=Decimal("2000000"),
        decimals=9, token_age_seconds=86_400,
    )
    return MarketState(**(defaults | kw))


def quote(**kw) -> Quote:
    defaults = dict(
        input_mint=SOL_MINT, output_mint=MINT,
        in_amount_raw=500_000_000, out_amount_raw=1_000_000_000,
        price_impact_pct=Decimal("0.4"), route_label="Raydium",
    )
    return Quote(**(defaults | kw))


def token_risk(**kw) -> TokenRisk:
    defaults = dict(mint=MINT, score=10, unknown=False)
    return TokenRisk(**(defaults | kw))


def candidate(**kw) -> TradeCandidate:
    defaults = dict(
        source_trade=source_trade(), trader=trader(), market=market(),
        token_risk=token_risk(), quote=quote(),
    )
    return TradeCandidate(**(defaults | kw))
