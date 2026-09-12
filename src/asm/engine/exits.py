"""PRD 28/29 - exit strategies.

Blindly mirroring a source exit is not optimal: our fill is later, our size is
different, and the trader may be exiting for reasons that do not apply to us. So exits
are independent rules, and the source's sell is one input among several.

`evaluate_exits` is pure: (position, market, trader, config, now) -> ExitIntent | None.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from asm.config import RiskConfig, settings
from asm.domain.enums import ExitReason, TraderStatus
from asm.domain.models import MarketState, Position, TraderProfile
from asm.domain.money import ZERO

ONE = Decimal(1)
HUNDRED = Decimal(100)


@dataclass(slots=True)
class ExitIntent:
    reason: ExitReason
    fraction: Decimal          # 0 < fraction <= 1 of the remaining position
    detail: dict
    urgent: bool = False

    @property
    def is_full(self) -> bool:
        return self.fraction >= Decimal("0.999")


def evaluate_exits(
    position: Position,
    market: MarketState,
    trader: TraderProfile | None = None,
    *,
    source_sold: bool = False,
    source_sold_fraction: Decimal = ONE,
    exit_liquidity_usd: Decimal | None = None,
    cfg: RiskConfig | None = None,
    now: datetime | None = None,
) -> ExitIntent | None:
    """First matching rule wins. Order encodes priority: capital preservation first."""
    cfg = cfg or settings.risk
    now = now or datetime.now(UTC)

    if position.amount_raw <= 0 or market.price_usd <= 0:
        return None

    pnl_pct = (
        (market.price_usd - position.entry_price) / position.entry_price * HUNDRED
        if position.entry_price > 0 else ZERO
    )
    d = {"pnl_pct": str(pnl_pct.quantize(Decimal("0.01"))),
         "price": str(market.price_usd), "entry": str(position.entry_price)}

    # 28.2 - independent stop loss. Always first: it is the loss cap.
    if pnl_pct <= cfg.stop_loss_pct:
        return ExitIntent(ExitReason.STOP_LOSS, ONE, d | {"threshold": str(cfg.stop_loss_pct)},
                          urgent=True)

    # 28.7 - liquidity deterioration. Getting out late is how a win becomes a loss.
    if exit_liquidity_usd is not None:
        value = position.market_value_usd
        if exit_liquidity_usd < value * Decimal("2"):
            return ExitIntent(
                ExitReason.LIQUIDITY_DETERIORATION, ONE,
                d | {"exit_liquidity_usd": str(exit_liquidity_usd),
                     "position_value_usd": str(value)},
                urgent=True,
            )

    # 28.6 - the trader we copied is no longer one we would copy.
    if trader and trader.status in (TraderStatus.DEGRADED, TraderStatus.SUSPENDED,
                                    TraderStatus.BLACKLISTED):
        return ExitIntent(ExitReason.TRADER_DEGRADED, ONE,
                          d | {"trader_status": trader.status.value})

    # 28.4 - trailing stop, armed only after the position has earned it.
    if position.peak_price > 0 and position.entry_price > 0:
        peak_gain = (position.peak_price - position.entry_price) / position.entry_price * HUNDRED
        armed = position.trailing_armed or peak_gain >= cfg.trailing_arm_pct
        if armed:
            drop = (position.peak_price - market.price_usd) / position.peak_price * HUNDRED
            if drop >= cfg.trailing_stop_pct:
                return ExitIntent(
                    ExitReason.TRAILING_STOP, ONE,
                    d | {"peak": str(position.peak_price),
                         "drop_from_peak_pct": str(drop.quantize(Decimal("0.01")))},
                )

    # 29 - partial profit taking ladder. Take money off the table progressively.
    for idx, (threshold, take_pct) in enumerate(cfg.take_profit_ladder):
        if idx in position.tp_levels_hit:
            continue
        if pnl_pct >= threshold:
            return ExitIntent(
                ExitReason.TAKE_PROFIT, take_pct / HUNDRED,
                d | {"ladder_index": idx, "threshold_pct": str(threshold)},
            )

    # 28.1 - mirror the source exit, scaled to how much they sold.
    if source_sold:
        return ExitIntent(
            ExitReason.MIRROR, min(source_sold_fraction, ONE),
            d | {"source_sold_fraction": str(source_sold_fraction)},
        )

    # 28.5 - time-based. A thesis that has not played out in a day probably will not.
    # Age is measured against the SUPPLIED clock. Position.age_seconds reads the wall
    # clock, which would make every replayed position instantly time out.
    age_seconds = int((now - position.opened_at).total_seconds())
    if age_seconds > cfg.max_hold_seconds:
        return ExitIntent(ExitReason.TIME_EXIT, ONE, d | {"age_seconds": age_seconds})

    return None


def update_marks(position: Position, market: MarketState, cfg: RiskConfig | None = None) -> bool:
    """Track peak and arm the trailing stop. Returns True when something changed."""
    cfg = cfg or settings.risk
    changed = position.current_price != market.price_usd
    if market.price_usd > position.peak_price:
        position.peak_price = market.price_usd
        changed = True
    position.current_price = market.price_usd
    if not position.trailing_armed and position.entry_price > 0:
        gain = (position.peak_price - position.entry_price) / position.entry_price * HUNDRED
        if gain >= cfg.trailing_arm_pct:
            position.trailing_armed = True
            changed = True
    return changed


def realized_pnl(position: Position, amount_raw: int, exit_price: Decimal) -> tuple[Decimal, Decimal]:
    """Returns (proceeds_usd, realized_pnl_usd) for selling `amount_raw` units."""
    if position.amount_raw <= 0:
        return ZERO, ZERO
    fraction = Decimal(amount_raw) / Decimal(position.amount_raw)
    units = Decimal(amount_raw) / (Decimal(10) ** position.decimals)
    proceeds = units * exit_price
    cost = position.cost_basis_usd * fraction
    return proceeds, proceeds - cost
