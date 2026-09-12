"""PRD 8/26 - one Executor interface, four modes.

Shadow is Live minus sign() and send(). That shared path is the entire point: it means
a shadow run exercises the real route, the real priority fee and the real simulation,
so the shadow-vs-live gap is measurable instead of assumed.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from asm.domain.enums import OrderStatus
from asm.domain.models import ApprovedOrder, Execution
from asm.domain.money import pct_change


class Executor(Protocol):
    mode: str

    async def execute(self, order: ApprovedOrder) -> Execution: ...


def execution_quality(ex: Execution) -> dict[str, str]:
    """PRD 26 - what we actually got versus what we expected and what the source got."""
    return {
        "vs_expected_pct": str(pct_change(ex.expected_price, ex.actual_price)),
        "vs_source_pct": str(pct_change(ex.source_price, ex.actual_price)),
        "slippage_error_pct": str(ex.actual_slippage_pct - ex.expected_slippage_pct),
        "total_fee_lamports": str(ex.total_fee_lamports),
    }


def new_execution(order: ApprovedOrder, mode: str) -> Execution:
    ex = Execution(
        order_id=order.id,
        mode=mode,
        status=OrderStatus.CREATED,
        source_price=order.candidate.source_trade.source_price,
        expected_price=order.candidate.market.price,
        in_amount_raw=order.in_amount_raw,
        latency=order.latency.model_copy(deep=True),
    )
    if order.candidate.quote:
        ex.expected_slippage_pct = order.candidate.quote.price_impact_pct
    return ex


def stamp(ex: Execution, field: str) -> None:
    setattr(ex.latency, field, datetime.now(UTC))
