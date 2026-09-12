"""Historical data provider - the backtester's view of the world (PRD 44).

Two rules govern everything here:

1. **No lookahead.** Every answer is computed from data at or before the simulated
   clock. A backtest that can see the future is a random number generator with good
   marketing.
2. **Pessimism on unknowns.** Where history is thin, the provider returns the
   conservative answer, never the flattering one. A backtest that overstates results
   is worse than no backtest, because it gets acted on.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from asm.domain.models import MarketState, Quote, TokenRisk
from asm.domain.money import ZERO, raw_to_ui


@dataclass
class PricePoint:
    at: datetime
    price_usd: Decimal
    liquidity_usd: Decimal = ZERO


@dataclass
class TokenHistory:
    """Observed price/liquidity for one token, oldest first."""

    mint: str
    points: list[PricePoint] = field(default_factory=list)
    decimals: int = 9
    created_at: datetime | None = None
    risk_score: int = 15
    symbol: str | None = None

    def __post_init__(self) -> None:
        self.points.sort(key=lambda p: p.at)
        self._times = [p.at for p in self.points]

    def at(self, when: datetime) -> PricePoint | None:
        """Last observation at or before `when`. Never interpolates forward."""
        if not self.points:
            return None
        idx = bisect.bisect_right(self._times, when) - 1
        return self.points[idx] if idx >= 0 else None


class HistoricalDataProvider:
    """Replays token history against a simulated clock."""

    def __init__(self, histories: dict[str, TokenHistory], *,
                 sol_price: Decimal = Decimal("150"),
                 max_staleness_seconds: int = 900,
                 impact_model: ImpactModel | None = None):
        self.histories = histories
        self._sol_price = sol_price
        self.max_staleness = timedelta(seconds=max_staleness_seconds)
        self.impact = impact_model or ImpactModel()
        self._now: datetime = datetime.now(UTC)
        self.misses: int = 0

    @property
    def now(self) -> datetime:
        return self._now

    def set_clock(self, when: datetime) -> None:
        self._now = when

    async def market_state(self, mint: str, *, decimals: int = 9) -> MarketState:
        h = self.histories.get(mint)
        if h is None:
            self.misses += 1
            return MarketState(mint=mint, stale=True, decimals=decimals)

        point = h.at(self._now)
        if point is None:
            self.misses += 1
            return MarketState(mint=mint, stale=True, decimals=h.decimals)

        # A quote from an hour ago is not a price. Mark it stale and let the gates reject.
        stale = (self._now - point.at) > self.max_staleness

        age = None
        if h.created_at:
            age = max(0, int((self._now - h.created_at).total_seconds()))

        return MarketState(
            mint=mint, price=point.price_usd / self._sol_price if self._sol_price else ZERO,
            price_usd=point.price_usd, liquidity_usd=point.liquidity_usd,
            market_cap_usd=ZERO, decimals=h.decimals, symbol=h.symbol,
            token_age_seconds=age, fetched_at=point.at, stale=stale,
        )

    async def token_risk(self, mint: str) -> TokenRisk:
        h = self.histories.get(mint)
        if h is None:
            return TokenRisk(mint=mint, score=100, unknown=True)
        return TokenRisk(mint=mint, score=h.risk_score, unknown=False)

    async def sol_price_usd(self) -> Decimal:
        return self._sol_price

    async def quote(self, *, input_mint: str, output_mint: str, amount_raw: int,
                    slippage_bps: int | None = None) -> Quote | None:
        """Synthesise the route Jupiter would have returned, from the impact model."""
        target = output_mint if output_mint in self.histories else input_mint
        state = await self.market_state(target)
        if state.stale or state.price_usd <= 0:
            return None

        buying = output_mint in self.histories
        in_usd = (
            raw_to_ui(amount_raw, 9) * self._sol_price
            if buying else raw_to_ui(amount_raw, state.decimals) * state.price_usd
        )
        impact_pct = self.impact.price_impact_pct(in_usd, state.liquidity_usd)

        if buying:
            effective = state.price_usd * (Decimal(1) + impact_pct / Decimal(100))
            out_ui = (in_usd / effective) if effective > 0 else ZERO
            out_raw = int(out_ui * (Decimal(10) ** state.decimals))
        else:
            effective = state.price_usd * (Decimal(1) - impact_pct / Decimal(100))
            out_usd = raw_to_ui(amount_raw, state.decimals) * effective
            out_raw = int((out_usd / self._sol_price) * (Decimal(10) ** 9)) if self._sol_price else 0

        if out_raw <= 0:
            return None

        return Quote(
            input_mint=input_mint, output_mint=output_mint,
            in_amount_raw=amount_raw, out_amount_raw=out_raw,
            price_impact_pct=impact_pct,
            slippage_bps=slippage_bps or 0, route_label="historical",
        )

    async def insider_flags(self, *, trader_wallet: str, mint: str,
                            source_value_usd: Decimal,
                            token_age_seconds: int | None) -> list[str]:
        flags: list[str] = []
        if token_age_seconds is not None and token_age_seconds < 120:
            flags.append("bought_within_2min_of_launch")
        return flags


@dataclass
class ImpactModel:
    """Price impact as a function of trade size versus available depth.

    impact% = k * (size / liquidity) ^ exponent * 100

    Exponent > 1 because depth thins as you eat through it - a trade twice as large
    costs more than twice as much. Defaults are deliberately harsh; calibrate them
    against shadow-mode fills before trusting a backtest's absolute numbers.
    """

    k: Decimal = Decimal("1.2")
    exponent: Decimal = Decimal("1.35")
    floor_pct: Decimal = Decimal("0.05")
    cap_pct: Decimal = Decimal("99")

    def price_impact_pct(self, size_usd: Decimal, liquidity_usd: Decimal) -> Decimal:
        if liquidity_usd <= 0:
            return self.cap_pct
        ratio = size_usd / liquidity_usd
        if ratio <= 0:
            return self.floor_pct
        impact = self.k * (ratio ** self.exponent) * Decimal(100)
        return max(self.floor_pct, min(impact, self.cap_pct))
