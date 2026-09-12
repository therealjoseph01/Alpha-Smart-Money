"""PRD 26/27 - shadow and live execution.

ShadowExecutor and LiveExecutor share `_prepare`. Shadow stops before sign/send.
That is how the shadow-vs-live gap becomes a measurement rather than a hope.
"""
from __future__ import annotations

import base64
from decimal import Decimal

from asm.adapters.helius import helius
from asm.adapters.jupiter import jupiter
from asm.config import settings
from asm.domain.enums import Action, OrderStatus
from asm.domain.models import ApprovedOrder, Execution
from asm.domain.money import ZERO, raw_to_ui
from asm.execution.base import new_execution, stamp
from asm.execution.mev import plan_protection
from asm.execution.signer import SignerError, get_signer
from asm.logging import get_logger
from asm.state.dedupe import Dedupe

log = get_logger(__name__)


class _BaseOnChain:
    mode = "shadow"

    def __init__(self, signer=None, dedupe: Dedupe | None = None):
        self._signer = signer
        self.dedupe = dedupe or Dedupe()

    @property
    def signer(self):
        if self._signer is None:
            self._signer = get_signer()
        return self._signer

    async def _prepare(self, order: ApprovedOrder, ex: Execution) -> str | None:
        """Re-quote, fee-estimate, build, simulate. Returns unsigned tx or None."""
        # PRD 52: never trade on a quote that is already stale.
        quote = await jupiter().quote(
            input_mint=order.input_mint,
            output_mint=order.output_mint,
            amount_raw=order.in_amount_raw,
            slippage_bps=order.slippage_bps,
        )
        if quote is None:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = "no_route_at_execution"
            return None

        ex.expected_slippage_pct = quote.price_impact_pct
        if quote.price_impact_pct > settings.copyability.max_expected_slippage_pct:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = f"slippage_regressed:{quote.price_impact_pct}"
            return None

        h = helius()
        accounts = [order.input_mint, order.output_mint, self.signer.pubkey]
        priority = await h.priority_fee(accounts)
        ex.priority_fee_lamports = priority

        # PRD 27 - decide whether this trade is worth protecting at all.
        plan = plan_protection(
            size_usd=order.size_usd,
            price_impact_pct=quote.price_impact_pct,
            liquidity_usd=order.candidate.market.liquidity_usd,
            urgent=order.exit_reason is not None,
        )
        ex.raw["mev"] = {
            "private_route": plan.use_private_route,
            "tip_lamports": plan.tip_lamports,
            "reason": plan.reason,
        }
        if quote.price_impact_pct > plan.max_price_impact_pct:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = f"impact_above_protection_ceiling:{quote.price_impact_pct}"
            return None
        if plan.use_private_route:
            # The swap builder does not attach a Sender tip instruction yet.
            # Never claim that an ordinary untipped transaction is protected.
            ex.status = OrderStatus.FAILED
            ex.failure_reason = "protected_route_requires_tip_instruction"
            return None

        try:
            built = await jupiter().build_swap(
                quote=quote, user_pubkey=self.signer.pubkey,
                priority_fee_lamports=priority,
            )
        except Exception as exc:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = f"build_failed:{exc!r}"[:200]
            return None

        tx_b64 = built.get("swapTransaction")
        if not tx_b64:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = "no_transaction_returned"
            return None

        stamp(ex, "transaction_built_at")
        ex.route = quote.route_label
        ex.raw["quote_at_execution"] = {
            "out_amount": quote.out_amount_raw,
            "price_impact_pct": str(quote.price_impact_pct),
            "route": quote.route_label,
        }

        sim = await h.simulate(tx_b64)
        if not isinstance(sim, dict) or not isinstance(sim.get("value"), dict) or "err" not in sim["value"]:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = "simulation_result_unavailable"
            return None
        sim_err = sim["value"]["err"]
        ex.raw["simulation"] = {"err": sim_err,
                                "units": (sim or {}).get("value", {}).get("unitsConsumed")}
        if sim_err:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = f"simulation_failed:{sim_err}"
            return None

        ex.status = OrderStatus.SIMULATED
        ex.out_amount_raw = quote.out_amount_raw
        if order.action is Action.BUY:
            ex.expected_price = (
                order.size_usd / raw_to_ui(quote.out_amount_raw, order.candidate.market.decimals)
                if quote.out_amount_raw else ZERO
            )
        else:
            from asm.engine.market import sol_price_usd

            sol_usd = await sol_price_usd()
            units = raw_to_ui(order.in_amount_raw, order.candidate.market.decimals)
            if sol_usd <= 0 or units <= 0:
                ex.status = OrderStatus.FAILED
                ex.failure_reason = "exit_valuation_unavailable"
                return None
            ex.value_usd = raw_to_ui(quote.out_amount_raw, 9) * sol_usd
            ex.expected_price = ex.value_usd / units
        return tx_b64


class ShadowExecutor(_BaseOnChain):
    """PRD 8.3 - build the exact transaction we would send, then stop."""

    mode = "shadow"

    async def execute(self, order: ApprovedOrder) -> Execution:
        ex = new_execution(order, self.mode)
        tx_b64 = await self._prepare(order, ex)
        if tx_b64 is None:
            return ex

        # Everything real except the two lines that move money.
        ex.status = OrderStatus.CONFIRMED
        ex.actual_price = ex.expected_price
        ex.actual_slippage_pct = ex.expected_slippage_pct
        if order.action is Action.BUY:
            ex.value_usd = order.size_usd
        ex.network_fee_lamports = 5_000
        ex.signature = f"shadow-{order.id.hex[:16]}"
        ex.raw["unsigned_tx_bytes"] = len(base64.b64decode(tx_b64))
        stamp(ex, "submitted_at")
        stamp(ex, "confirmed_at")
        log.info("shadow_fill", mint=order.output_mint[:8], route=ex.route)
        return ex


class LiveExecutor(_BaseOnChain):
    """PRD 8.4 - signs and broadcasts. Guarded by idempotency and the signer's own limits."""

    mode = "live"

    async def execute(self, order: ApprovedOrder) -> Execution:
        ex = new_execution(order, self.mode)

        # PRD 51: claim the idempotency key BEFORE signing. A timeout after broadcast
        # must never let a retry double-buy.
        claimed = await self.dedupe.claim_order(order.idempotency_key)
        if not claimed:
            existing = await self.dedupe.lookup_order(order.idempotency_key)
            ex.status = OrderStatus.FAILED
            ex.failure_reason = f"duplicate_order:{existing}"
            log.warning("duplicate_order_blocked", key=order.idempotency_key, existing=existing)
            return ex

        tx_b64 = await self._prepare(order, ex)
        if tx_b64 is None:
            await self.dedupe.abandon_order(order.idempotency_key)
            return ex

        try:
            signed = await self.signer.sign(
                tx_b64, max_lamports=order.in_amount_raw + ex.priority_fee_lamports + 10_000
            )
            stamp(ex, "signed_at")
        except SignerError as exc:
            ex.status = OrderStatus.FAILED
            ex.failure_reason = f"signer_refused:{exc}"
            await self.dedupe.abandon_order(order.idempotency_key)
            log.error("signer_refused", error=str(exc))
            return ex

        h = helius()
        try:
            # A protected trade goes through the block-engine route; an unprotected one
            # through the ordinary RPC, keeping the tip we decided not to spend.
            from solders.transaction import VersionedTransaction

            signature = str(VersionedTransaction.from_bytes(base64.b64decode(signed)).signatures[0])
            ex.signature = signature
            ex.status = OrderStatus.SUBMITTED
            await self.dedupe.finish_order(order.idempotency_key, signature)
            returned_signature = await h.send_transaction(signed)
            if returned_signature != signature:
                raise ValueError("RPC returned a different transaction signature")
            stamp(ex, "submitted_at")
            ex.signature = signature
            ex.status = OrderStatus.SUBMITTED
            await self.dedupe.finish_order(order.idempotency_key, signature)
        except Exception as exc:
            # PRD 53: a send timeout is NOT proof of failure. Leave the idempotency key
            # claimed and reconcile by signature before ever retrying.
            ex.status = OrderStatus.SUBMITTED
            ex.failure_reason = f"submit_error_unconfirmed:{exc!r}"[:200]
            log.error("submit_failed_unconfirmed", key=order.idempotency_key, error=repr(exc))
            return ex

        try:
            status = await h.confirm(signature)
        except Exception as exc:
            ex.status = OrderStatus.SUBMITTED
            ex.failure_reason = f"confirmation_unavailable:{exc!r}"[:200]
            return ex
        if status is None:
            ex.status = OrderStatus.SUBMITTED
            ex.failure_reason = "confirmation_timeout"
            return ex
        if status.get("err"):
            ex.status = OrderStatus.FAILED
            ex.failure_reason = f"onchain_error:{status['err']}"
            return ex

        try:
            await self._reconcile_fill(ex, order, signature)
        except Exception as exc:
            ex.status = OrderStatus.SUBMITTED
            ex.failure_reason = f"fill_reconciliation_required:{exc!r}"[:200]
            return ex
        stamp(ex, "confirmed_at")
        ex.status = OrderStatus.CONFIRMED
        log.info("live_fill", signature=signature[:16], mint=order.output_mint[:8],
                 size_usd=str(order.size_usd))
        return ex

    async def _reconcile_fill(self, ex: Execution, order: ApprovedOrder, signature: str) -> None:
        """PRD 26.12 - record the ACTUAL fill, not the quote we hoped for."""
        from asm.engine.market import sol_price_usd
        from asm.ingest.parser import parse_transaction

        tx = await helius().get_transaction(signature)
        sol_usd = await sol_price_usd()
        if not tx or sol_usd <= 0:
            raise ValueError("transaction or SOL price unavailable")
        parsed = parse_transaction(tx, self.signer.pubkey, signature=signature,
                                   sol_price_usd=sol_usd)
        if (parsed is None or parsed.token_amount_raw <= 0 or parsed.source_value_usd <= 0
                or parsed.token_mint != order.candidate.source_trade.token_mint):
            raise ValueError("fill cannot be matched to order")
        if (parsed.action is Action.BUY) != (order.action is Action.BUY):
            raise ValueError("fill direction does not match order")
        # meta.fee already includes the priority fee; do not count it twice.
        ex.network_fee_lamports = int((tx.get("meta") or {}).get("fee", 0))
        ex.priority_fee_lamports = 0
        ex.value_usd = parsed.source_value_usd
        units = raw_to_ui(parsed.token_amount_raw, parsed.token_decimals)
        ex.actual_price = ex.value_usd / units
        ex.out_amount_raw = (parsed.token_amount_raw if order.action is Action.BUY
                             else parsed.quote_amount_raw)
        if ex.expected_price > 0:
            direction = Decimal(1) if order.action is Action.BUY else Decimal(-1)
            ex.actual_slippage_pct = direction * (
                (ex.actual_price - ex.expected_price) / ex.expected_price * Decimal(100)
            )
