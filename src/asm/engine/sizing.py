"""PRD 21 - position sizing.

Sizing produces an *intent*. The risk ledger is what authorises it (PRD 52), so a
size returned here is never final until reserve() grants it.
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from asm.config import RiskConfig, settings
from asm.domain.models import PortfolioState, TradeCandidate
from asm.domain.money import ZERO, clamp

ONE = Decimal(1)


class SizingMode(StrEnum):
    FIXED = "fixed"
    PORTFOLIO_PCT = "portfolio_pct"
    SOURCE_PROPORTIONAL = "source_proportional"
    CONFIDENCE_WEIGHTED = "confidence_weighted"
    KELLY = "kelly"


def base_size(
    candidate: TradeCandidate,
    portfolio: PortfolioState,
    mode: SizingMode = SizingMode.CONFIDENCE_WEIGHTED,
    cfg: RiskConfig | None = None,
) -> Decimal:
    cfg = cfg or settings.risk
    equity = portfolio.total_equity_usd
    if equity <= 0:
        return ZERO

    match mode:
        case SizingMode.FIXED:
            size = cfg.min_position_usd * Decimal(5)

        case SizingMode.PORTFOLIO_PCT:
            size = equity * cfg.base_position_pct / Decimal(100)

        case SizingMode.SOURCE_PROPORTIONAL:
            # PRD 21.3 - mirror the source's conviction, not their absolute size.
            src_usd = candidate.source_trade.source_value_usd
            avg = candidate.trader.avg_trade_size_usd or src_usd
            conviction = clamp(
                (src_usd / avg) if avg > 0 else ONE, Decimal("0.25"), Decimal(3)
            )
            size = equity * cfg.base_position_pct / Decimal(100) * conviction

        case SizingMode.CONFIDENCE_WEIGHTED:
            # PRD 5.3 - capital is earned. Score and confidence both gate size.
            s = candidate.trader.scores
            score_w = clamp(s.composite / Decimal(100), Decimal("0.2"), ONE)
            conf_w = clamp(s.confidence / Decimal(100), Decimal("0.2"), ONE)
            copy_w = clamp(s.copyability / Decimal(100), Decimal("0.2"), ONE)
            size = equity * cfg.base_position_pct / Decimal(100) * score_w * conf_w * copy_w

        case SizingMode.KELLY:
            size = _kelly(candidate, equity, cfg)

        case _:
            size = equity * cfg.base_position_pct / Decimal(100)

    return size


def _kelly(candidate: TradeCandidate, equity: Decimal, cfg: RiskConfig) -> Decimal:
    """PRD 21.5 - fractional Kelly, heavily capped.

    Full Kelly on memecoin win rates is ruin-seeking: the payoff distribution has fat
    left tails that the formula assumes away. Quarter-Kelly, then hard-capped.
    """
    t = candidate.trader
    win_rate = (t.win_rate or ZERO) / Decimal(100)
    if win_rate <= 0 or t.trade_count < 30:
        return equity * cfg.base_position_pct / Decimal(100) * Decimal("0.5")

    avg_win = Decimal(str(t.notes.get("avg_win_pct", 60))) / Decimal(100)
    avg_loss = Decimal(str(t.notes.get("avg_loss_pct", 35))) / Decimal(100)
    if avg_loss <= 0:
        return equity * cfg.base_position_pct / Decimal(100)

    b = avg_win / avg_loss
    edge = (b * win_rate - (ONE - win_rate)) / b
    fraction = clamp(edge * Decimal("0.25"), ZERO, cfg.max_position_pct / Decimal(100))
    return equity * fraction


def final_size(
    candidate: TradeCandidate,
    portfolio: PortfolioState,
    gate_multiplier: Decimal,
    mode: SizingMode = SizingMode.CONFIDENCE_WEIGHTED,
    cfg: RiskConfig | None = None,
) -> tuple[Decimal, Decimal]:
    """Returns (size_usd, size_pct_of_equity) after gate multipliers and hard caps."""
    cfg = cfg or settings.risk
    equity = portfolio.total_equity_usd
    size = base_size(candidate, portfolio, mode, cfg) * gate_multiplier

    # Hard ceiling - PRD 4: no autonomous change to hard limits.
    cap = equity * cfg.max_position_pct / Decimal(100)
    size = min(size, cap, portfolio.cash_usd)

    if size < cfg.min_position_usd:
        return ZERO, ZERO
    pct = (size / equity * Decimal(100)) if equity > 0 else ZERO
    return size.quantize(Decimal("0.01")), pct.quantize(Decimal("0.0001"))
