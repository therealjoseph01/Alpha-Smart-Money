"""PRD 45 - paper trading must simulate the things that destroy real edge.

A paper engine that fills at the quoted mid is a lie generator. This one applies:
  * the router's own price impact at our real size
  * an extra adverse drift proportional to how long we took to decide
  * network + priority fees
  * a probabilistic failure rate that rises with congestion proxies
"""
from __future__ import annotations

import random
from decimal import Decimal

from asm.config import settings
from asm.domain.enums import Action, OrderStatus
from asm.domain.models import ApprovedOrder, Execution
from asm.domain.money import ZERO, raw_to_ui
from asm.execution.base import new_execution, stamp
from asm.logging import get_logger

log = get_logger(__name__)

BASE_FAIL_RATE = Decimal("0.04")
NETWORK_FEE_LAMPORTS = 5_000
# Adverse drift per second of latency, as a fraction of price. Calibrate this from
# shadow-mode data; the default is intentionally pessimistic.
DRIFT_PCT_PER_SECOND = Decimal("0.35")


class PaperExecutor:
    mode = "paper"

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()

    async def execute(self, order: ApprovedOrder) -> Execution:
        ex = new_execution(order, self.mode)
        stamp(ex, "transaction_built_at")
        stamp(ex, "submitted_at")

        quote = order.candidate.quote
        if quote is None and order.action in (Action.SELL, Action.PARTIAL_SELL):
            from asm.adapters.jupiter import jupiter

            quote = await jupiter().quote(
                input_mint=order.input_mint, output_mint=order.output_mint,
                amount_raw=order.in_amount_raw, slippage_bps=order.slippage_bps,
            )
        if quote is None or not quote.out_amount_raw:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = "no_route"
            return ex

        latency_s = Decimal(order.latency.analysis_ms or 0) / Decimal(1000)
        impact_pct = quote.price_impact_pct
        drift_pct = DRIFT_PCT_PER_SECOND * latency_s

        if self._failed(impact_pct):
            ex.status = OrderStatus.FAILED
            ex.failure_reason = "simulated_tx_failure"
            ex.priority_fee_lamports = settings.priority_fee_lamports
            ex.network_fee_lamports = NETWORK_FEE_LAMPORTS
            stamp(ex, "confirmed_at")
            return ex

        expected = order.candidate.market.price_usd
        total_adverse = (impact_pct + drift_pct) / Decimal(100)
        is_buy = order.action is Action.BUY
        actual = max(ZERO, expected * (Decimal(1) + total_adverse if is_buy
                                      else Decimal(1) - total_adverse))

        decimals = order.candidate.market.decimals
        filled_ui = (order.size_usd / actual) if actual > 0 else ZERO

        ex.status = OrderStatus.CONFIRMED
        ex.actual_price = actual
        ex.expected_price = expected
        ex.actual_slippage_pct = impact_pct + drift_pct
        if is_buy:
            ex.out_amount_raw = int(filled_ui * (Decimal(10) ** decimals))
            ex.value_usd = order.size_usd
        else:
            ex.value_usd = raw_to_ui(order.in_amount_raw, decimals) * actual
            # Router output is already denominated in the output token's raw units.
            ex.out_amount_raw = int(Decimal(quote.out_amount_raw) *
                                    max(ZERO, Decimal(1) - drift_pct / Decimal(100)))
        ex.network_fee_lamports = NETWORK_FEE_LAMPORTS
        ex.priority_fee_lamports = settings.priority_fee_lamports
        ex.route = quote.route_label or "simulated"
        ex.signature = f"paper-{order.id.hex[:16]}"
        stamp(ex, "confirmed_at")

        log.info(
            "paper_fill", mint=order.output_mint[:8], size_usd=str(order.size_usd),
            slippage_pct=str(ex.actual_slippage_pct.quantize(Decimal("0.01"))),
            drift_pct=str(drift_pct.quantize(Decimal("0.01"))),
        )
        return ex

    def _failed(self, impact_pct: Decimal) -> bool:
        """Thin books fail more: the route goes stale between quote and land."""
        rate = BASE_FAIL_RATE + (impact_pct / Decimal(100)) * Decimal("0.5")
        return Decimal(str(self.rng.random())) < min(rate, Decimal("0.5"))
