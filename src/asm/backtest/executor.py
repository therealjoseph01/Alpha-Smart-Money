"""Backtest fills (PRD 44/45).

Uses the historical provider's quote - so the fill reflects the same impact model the
gates were shown - then adds the costs a live fill pays and a paper fill often forgets:
latency drift, priority fees, and a failure rate that rises with congestion.
"""
from __future__ import annotations

import random
from decimal import Decimal

from asm.domain.enums import OrderStatus
from asm.domain.models import ApprovedOrder, Execution
from asm.domain.money import ZERO, raw_to_ui
from asm.execution.base import new_execution

NETWORK_FEE_LAMPORTS = 5_000


class BacktestExecutor:
    """Deterministic given a seed, so a backtest is reproducible."""

    mode = "backtest"

    def __init__(self, *, seed: int = 1337, fail_rate: Decimal = Decimal("0.04"),
                 priority_fee_lamports: int = 100_000,
                 drift_pct_per_second: Decimal = Decimal("0.35"),
                 sol_price: Decimal = Decimal("150")):
        self.rng = random.Random(seed)
        self.fail_rate = fail_rate
        self.priority_fee_lamports = priority_fee_lamports
        self.drift_pct_per_second = drift_pct_per_second
        self.sol_price = sol_price
        self.fills = 0
        self.failures = 0

    async def execute(self, order: ApprovedOrder) -> Execution:
        ex = new_execution(order, self.mode)
        ex.latency = order.latency.model_copy(deep=True)
        quote = order.candidate.quote

        if quote is None or not quote.out_amount_raw:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = "no_route"
            self.failures += 1
            return ex

        impact_pct = quote.price_impact_pct
        if self._failed(impact_pct):
            ex.status = OrderStatus.FAILED
            ex.failure_reason = "simulated_tx_failure"
            ex.network_fee_lamports = NETWORK_FEE_LAMPORTS
            ex.priority_fee_lamports = self.priority_fee_lamports
            self.failures += 1
            return ex

        latency_s = Decimal(order.latency.analysis_ms or 0) / Decimal(1000)
        drift_pct = self.drift_pct_per_second * latency_s
        is_buy = order.output_mint == order.candidate.source_trade.token_mint

        mark = order.candidate.market.price_usd
        # Drift always works against us: worse entries on buys, worse exits on sells.
        actual = (mark * (Decimal(1) + (impact_pct + drift_pct) / Decimal(100)) if is_buy
                  else mark * (Decimal(1) - (impact_pct + drift_pct) / Decimal(100)))
        actual = max(actual, ZERO)

        if is_buy:
            filled_ui = (order.size_usd / actual) if actual > 0 else ZERO
            ex.out_amount_raw = int(filled_ui * (Decimal(10) ** order.candidate.market.decimals))
            ex.value_usd = order.size_usd
        else:
            sold_ui = raw_to_ui(order.in_amount_raw, order.candidate.market.decimals)
            proceeds = sold_ui * actual
            ex.out_amount_raw = int((proceeds / self.sol_price) * (Decimal(10) ** 9)) if self.sol_price else 0
            ex.value_usd = proceeds

        ex.status = OrderStatus.CONFIRMED
        ex.expected_price = mark
        ex.actual_price = actual
        ex.actual_slippage_pct = impact_pct + drift_pct
        ex.expected_slippage_pct = impact_pct
        ex.network_fee_lamports = NETWORK_FEE_LAMPORTS
        ex.priority_fee_lamports = self.priority_fee_lamports
        ex.route = quote.route_label or "historical"
        ex.signature = f"bt-{order.id.hex[:16]}"
        self.fills += 1
        return ex

    def fee_usd(self) -> Decimal:
        lamports = NETWORK_FEE_LAMPORTS + self.priority_fee_lamports
        return Decimal(lamports) / Decimal(1_000_000_000) * self.sol_price

    def _failed(self, impact_pct: Decimal) -> bool:
        rate = self.fail_rate + (impact_pct / Decimal(100)) * Decimal("0.5")
        return Decimal(str(self.rng.random())) < min(rate, Decimal("0.5"))
