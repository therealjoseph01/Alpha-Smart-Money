"""Data providers: the seam that lets backtest, paper and live share one decision engine.

PRD 44 is only meaningful if a replayed decision runs the SAME gate code as a live one.
If the backtester has its own copy of the rules, it validates the copy, not the system.
So the engine depends on this protocol, and only the provider changes between modes.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from asm.domain.models import MarketState, Quote, TokenRisk


class DataProvider(Protocol):
    """Everything the decision engine needs to know about the outside world."""

    async def market_state(self, mint: str, *, decimals: int = 9) -> MarketState: ...
    async def token_risk(self, mint: str) -> TokenRisk: ...
    async def sol_price_usd(self) -> Decimal: ...
    async def quote(self, *, input_mint: str, output_mint: str,
                    amount_raw: int, slippage_bps: int | None = None) -> Quote | None: ...
    async def insider_flags(self, *, trader_wallet: str, mint: str,
                            source_value_usd: Decimal,
                            token_age_seconds: int | None) -> list[str]: ...
    @property
    def now(self) -> datetime | None:
        """Simulated clock for replay; None means use the wall clock."""


class LiveDataProvider:
    """Production: Helius + Jupiter, through the caches and circuit breakers."""

    @property
    def now(self) -> datetime | None:
        return None

    async def market_state(self, mint: str, *, decimals: int = 9) -> MarketState:
        from asm.engine.market import get_market_state

        return await get_market_state(mint, decimals=decimals)

    async def token_risk(self, mint: str) -> TokenRisk:
        from asm.engine.token_safety import evaluate_token

        return await evaluate_token(mint)

    async def sol_price_usd(self) -> Decimal:
        from asm.engine.market import sol_price_usd

        return await sol_price_usd()

    async def quote(self, *, input_mint: str, output_mint: str,
                    amount_raw: int, slippage_bps: int | None = None) -> Quote | None:
        from asm.adapters.jupiter import jupiter

        return await jupiter().quote(
            input_mint=input_mint, output_mint=output_mint,
            amount_raw=amount_raw, slippage_bps=slippage_bps,
        )

    async def insider_flags(self, *, trader_wallet: str, mint: str,
                            source_value_usd: Decimal,
                            token_age_seconds: int | None) -> list[str]:
        from asm.engine.token_safety import detect_insider_pattern

        return await detect_insider_pattern(
            trader_wallet=trader_wallet, mint=mint,
            source_value_usd=source_value_usd, token_age_seconds=token_age_seconds,
        )
