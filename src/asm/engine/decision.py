"""PRD 41 - the decision engine.

Order of operations matters and is deliberate:

  1. cheap deterministic rejects first (action, dedupe, system state)
  2. gates that need no network second
  3. network enrichment (market, risk, quote) only for candidates still alive
  4. sizing
  5. atomic risk reservation LAST - it mutates state, so nothing after it may reject
     without releasing

Every rejection is recorded with its full input snapshot. Rejections are the more
valuable half of the dataset: they are the counterfactual the copyability model learns
from (PRD 74.1).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from asm.config import settings
from asm.domain.enums import Action, Decision, Reason
from asm.domain.models import (
    ApprovedOrder,
    DecisionResult,
    GateResult,
    LatencyBreakdown,
    MarketState,
    SourceTrade,
    TradeCandidate,
    TraderProfile,
)
from asm.domain.money import SOL_MINT, ui_to_raw
from asm.engine import gates as G
from asm.engine.providers import DataProvider, LiveDataProvider
from asm.engine.sizing import SizingMode, final_size
from asm.logging import get_logger
from asm.state.ledger import RiskLedger

log = get_logger(__name__)
STRATEGY_VERSION = "v1"


def _reject(
    candidate_id, *reasons: Reason, gates: list[GateResult] | None = None,
    latency: LatencyBreakdown | None = None, **inputs
) -> DecisionResult:
    return DecisionResult(
        candidate_id=candidate_id,
        decision=Decision.REJECT,
        reasons=list(reasons),
        gates=gates or [],
        latency=latency or LatencyBreakdown(),
        inputs=inputs,
        strategy_version=STRATEGY_VERSION,
    )


class DecisionEngine:
    def __init__(self, ledger: RiskLedger | None = None,
                 sizing_mode: SizingMode = SizingMode.CONFIDENCE_WEIGHTED,
                 data: DataProvider | None = None):
        self.ledger = ledger or RiskLedger()
        self.sizing_mode = sizing_mode
        # Swapping this for a HistoricalDataProvider is the whole backtester (PRD 44).
        self.data = data or LiveDataProvider()

    def _now(self) -> datetime:
        return self.data.now or datetime.now(UTC)

    async def evaluate_entry(
        self, trade: SourceTrade, trader: TraderProfile,
        confirmations: list[str] | None = None,
    ) -> tuple[DecisionResult, ApprovedOrder | None]:
        latency = LatencyBreakdown(
            source_confirmed_at=trade.source_confirmed_at, detected_at=trade.detected_at
        )
        candidate = TradeCandidate(
            source_trade=trade, trader=trader,
            market=MarketState(mint=trade.token_mint),
            confirmations=confirmations or [],
            created_at=self._now(),
        )

        # ---- 1. free rejects -------------------------------------------------
        if trade.action is not Action.BUY:
            return self._done(_reject(candidate.id, Reason.NON_COPYABLE_ACTION,
                                      latency=latency, action=trade.action.value)), None

        pre_gate = G.trader_gate(candidate, settings.copyability)
        if not pre_gate.passed:
            return self._done(_reject(candidate.id, *pre_gate.reasons, gates=[pre_gate],
                                      latency=latency)), None

        # ---- 2. enrichment ---------------------------------------------------
        market, token_risk = await asyncio.gather(
            self.data.market_state(trade.token_mint, decimals=trade.token_decimals),
            self.data.token_risk(trade.token_mint),
        )
        candidate.market = market
        candidate.token_risk = token_risk

        if market.stale or market.price_usd <= 0:
            return self._done(_reject(candidate.id, Reason.STALE_MARKET_DATA,
                                      latency=latency, mint=trade.token_mint)), None

        flags = await self.data.insider_flags(
            trader_wallet=trade.wallet, mint=trade.token_mint,
            source_value_usd=trade.source_value_usd,
            token_age_seconds=market.token_age_seconds,
        )
        if flags:
            token_risk.insider_flags = flags

        # ---- 3. provisional size, then a real quote at that size -------------
        portfolio = await self.ledger.snapshot()
        provisional, _ = final_size(candidate, portfolio, Decimal(1), self.sizing_mode)
        if provisional <= 0:
            return self._done(_reject(candidate.id, Reason.POSITION_BELOW_MINIMUM,
                                      latency=latency, equity=str(portfolio.total_equity_usd))), None

        sol_usd = await self.data.sol_price_usd()
        if sol_usd <= 0:
            return self._done(_reject(candidate.id, Reason.CRITICAL_DATA_UNAVAILABLE,
                                      latency=latency, missing="sol_price")), None

        in_lamports = ui_to_raw(provisional / sol_usd, 9)
        candidate.quote = await self.data.quote(
            input_mint=SOL_MINT, output_mint=trade.token_mint, amount_raw=in_lamports
        )
        if candidate.quote is None:
            return self._done(_reject(candidate.id, Reason.NO_ROUTE,
                                      latency=latency, mint=trade.token_mint)), None

        # ---- 4. full gate sweep ---------------------------------------------
        candidate.created_at = self._now()
        results, passed, mult = G.run_entry_gates(candidate, provisional)
        latency.decision_completed_at = self._now()

        if not passed:
            failed = [r for r in results if not r.passed]
            reasons = G.collect_reasons(failed)
            return self._done(DecisionResult(
                candidate_id=candidate.id, decision=Decision.REJECT,
                reasons=reasons, gates=results, latency=latency,
                inputs=self._snapshot(candidate, provisional, portfolio),
                strategy_version=STRATEGY_VERSION,
            )), None

        size_usd, size_pct = final_size(candidate, portfolio, mult, self.sizing_mode)
        if size_usd <= 0:
            return self._done(DecisionResult(
                candidate_id=candidate.id, decision=Decision.REJECT,
                reasons=[Reason.POSITION_BELOW_MINIMUM], gates=results, latency=latency,
                inputs=self._snapshot(candidate, provisional, portfolio),
                strategy_version=STRATEGY_VERSION,
            )), None

        # ---- 5. atomic reservation (mutates state - must be last) ------------
        res = await self.ledger.reserve(
            mint=trade.token_mint, wallet=trade.wallet, size_usd=size_usd
        )
        if not res.granted:
            return self._done(DecisionResult(
                candidate_id=candidate.id, decision=Decision.REJECT,
                reasons=[res.reason] if res.reason else [Reason.INSUFFICIENT_CAPITAL],
                gates=results, latency=latency,
                inputs=self._snapshot(candidate, size_usd, portfolio) | {"ledger_detail": res.detail},
                strategy_version=STRATEGY_VERSION,
            )), None

        decision = DecisionResult(
            candidate_id=candidate.id, decision=Decision.COPY,
            reasons=G.collect_reasons(results), gates=results,
            size_usd=res.usd, size_pct=size_pct, size_multiplier=mult,
            reservation_id=res.id, latency=latency,
            inputs=self._snapshot(candidate, res.usd, portfolio),
            strategy_version=STRATEGY_VERSION,
        )

        order = ApprovedOrder(
            idempotency_key=f"entry:{trade.dedupe_key}",
            decision_id=decision.id, candidate=candidate, action=Action.BUY,
            input_mint=SOL_MINT, output_mint=trade.token_mint,
            in_amount_raw=ui_to_raw(res.usd / sol_usd, 9),
            size_usd=res.usd,
            slippage_bps=settings.copyability.execution_slippage_bps,
            reservation_id=res.id, latency=latency,
        )
        log.info(
            "trade_approved", trader=trade.wallet[:8], mint=trade.token_mint[:8],
            size_usd=str(res.usd), mult=str(mult),
            reasons=[r.value for r in decision.reasons],
        )
        return decision, order

    # ------------------------------------------------------------------ helpers
    def _done(self, d: DecisionResult) -> DecisionResult:
        if d.latency.decision_completed_at is None:
            d.latency.decision_completed_at = self._now()
        log.info(
            "trade_rejected", candidate=str(d.candidate_id)[:8],
            reasons=[r.value for r in d.reasons],
        )
        return d

    def _snapshot(self, c: TradeCandidate, size: Decimal, portfolio) -> dict:
        """PRD 5.5 - everything needed to re-run this decision offline."""
        return {
            "trader": {
                "wallet": c.trader.wallet,
                "composite": str(c.trader.scores.composite),
                "confidence": str(c.trader.scores.confidence),
                "copyability": str(c.trader.scores.copyability),
                "status": c.trader.status.value,
                "latency_tolerance_ms": c.trader.latency_tolerance_ms,
            },
            "source_trade": {
                "signature": c.source_trade.signature,
                "price": str(c.source_trade.source_price),
                "value_usd": str(c.source_trade.source_value_usd),
                "confirmed_at": c.source_trade.source_confirmed_at.isoformat(),
                "observation_latency_ms": c.source_trade.observation_latency_ms,
            },
            "market": {
                "price_usd": str(c.market.price_usd),
                "liquidity_usd": str(c.market.liquidity_usd),
                "market_cap_usd": str(c.market.market_cap_usd),
                "token_age_seconds": c.market.token_age_seconds,
            },
            "token_risk": {
                "score": c.token_risk.score if c.token_risk else None,
                "flags": c.token_risk.insider_flags if c.token_risk else [],
            },
            "quote": {
                "price_impact_pct": str(c.quote.price_impact_pct) if c.quote else None,
                "route": c.quote.route_label if c.quote else None,
                "out_amount_raw": c.quote.out_amount_raw if c.quote else None,
            },
            "portfolio": {
                "equity": str(portfolio.total_equity_usd),
                "cash": str(portfolio.cash_usd),
                "deployed": str(portfolio.deployed_usd),
                "open_positions": portfolio.open_positions,
                "drawdown_pct": str(portfolio.drawdown_pct),
            },
            "intended_size_usd": str(size),
            "config_version": STRATEGY_VERSION,
        }


def render_decision(d: DecisionResult) -> str:
    """PRD 41 - the human-readable block. Used in logs, alerts and the daily report."""
    i = d.inputs
    t, m, q = i.get("trader", {}), i.get("market", {}), i.get("quote", {})
    lines = [
        "TRADE SIGNAL",
        f"Trader:              {t.get('wallet', '?')[:12]}",
        f"Token:               {i.get('source_trade', {}).get('signature', '?')[:12]}",
        "",
        f"Trader Score:        {t.get('composite', '-')}",
        f"Confidence:          {t.get('confidence', '-')}",
        f"Copyability:         {t.get('copyability', '-')}",
        f"Token Risk:          {i.get('token_risk', {}).get('score', '-')}/100",
        f"Liquidity:           ${m.get('liquidity_usd', '-')}",
        f"Expected Slippage:   {q.get('price_impact_pct', '-')}%",
        "",
        f"Decision: {d.decision.value.upper()}",
    ]
    if d.approved:
        lines.append(f"Position Size: ${d.size_usd} ({d.size_pct}% portfolio)")
    lines.append("Reason Codes:")
    lines += [f"- {r.value}" for r in d.reasons]
    return "\n".join(lines)
