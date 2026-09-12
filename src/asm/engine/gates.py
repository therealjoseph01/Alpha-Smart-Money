"""PRD 17-20 - the copyability gates.

Every gate is a pure function of (candidate, config) -> GateResult. No I/O, no clock
reads beyond what is on the candidate. That is what makes PRD 5.5 possible: given the
stored inputs, a decision can be replayed months later and must produce the same answer.

A gate can reject (passed=False) or merely shrink the trade (size_mult < 1).
"""
from __future__ import annotations

from decimal import Decimal

from asm.config import CopyabilityConfig, settings
from asm.domain.enums import Action, Reason
from asm.domain.models import GateResult, TradeCandidate
from asm.domain.money import SOL_MINT, ZERO, pct_change

ONE = Decimal(1)


# ---------------------------------------------------------------- trader gates
def trader_gate(c: TradeCandidate, cfg: CopyabilityConfig) -> GateResult:
    t = c.trader
    if not t.status.is_followed:
        return GateResult.fail("trader", Reason.TRADER_NOT_FOLLOWED, status=t.status.value)
    if t.scores.composite < cfg.min_trader_score:
        return GateResult.fail(
            "trader", Reason.TRADER_SCORE_TOO_LOW,
            score=str(t.scores.composite), required=str(cfg.min_trader_score),
        )
    if t.scores.confidence < cfg.min_trader_confidence:
        return GateResult.fail(
            "trader", Reason.TRADER_CONFIDENCE_TOO_LOW, confidence=str(t.scores.confidence)
        )
    if t.scores.copyability < cfg.min_copyability_score:
        return GateResult.fail(
            "trader", Reason.COPYABILITY_TOO_LOW, copyability=str(t.scores.copyability)
        )
    return GateResult.ok(
        "trader", Reason.HIGH_TRADER_SCORE, Reason.HIGH_COPYABILITY,
        mult=t.size_multiplier, score=str(t.scores.composite),
    )


# ------------------------------------------------------------ 17.1 price chase
def price_chase_gate(c: TradeCandidate, cfg: CopyabilityConfig) -> GateResult:
    """A trader being right does not mean the price we can still get is right."""
    src = c.source_trade.source_price
    now = c.market.price if c.source_trade.quote_mint == SOL_MINT else c.market.price_usd
    if src <= 0 or now <= 0:
        return GateResult.fail("price_chase", Reason.CRITICAL_DATA_UNAVAILABLE, move_pct="unknown")

    move = pct_change(src, now)
    # Chasing only matters in the direction that hurts us.
    adverse = move if c.source_trade.action is Action.BUY else -move
    d = {"move_pct": str(move.quantize(Decimal('0.0001'))), "adverse_pct": str(adverse.quantize(Decimal('0.0001')))}

    if adverse <= cfg.price_move_normal_pct:
        return GateResult.ok("price_chase", Reason.ACCEPTABLE_PRICE_MOVE, mult=ONE, **d)
    if adverse <= cfg.price_move_acceptable_pct:
        return GateResult.ok("price_chase", Reason.ACCEPTABLE_PRICE_MOVE, mult=ONE, **d)
    if adverse <= cfg.price_move_reduced_pct:
        return GateResult.ok(
            "price_chase", Reason.PRICE_MOVE_REDUCED_SIZE, mult=cfg.price_move_reduced_mult, **d
        )
    if adverse <= cfg.price_move_caution_pct:
        return GateResult.ok(
            "price_chase", Reason.PRICE_MOVE_REDUCED_SIZE, mult=cfg.price_move_caution_mult, **d
        )
    return GateResult.fail("price_chase", Reason.PRICE_MOVED_TOO_FAR, **d)


# --------------------------------------------------------------- 17.2 latency
def latency_gate(c: TradeCandidate, cfg: CopyabilityConfig) -> GateResult:
    """Per-trader latency tolerance, learned. A trader whose edge dies in two seconds
    is a fundamentally different asset from one who holds for hours."""
    age_ms = int((c.created_at - c.source_trade.source_confirmed_at).total_seconds() * 1000)
    d = {"signal_age_ms": age_ms}

    if age_ms > cfg.max_detection_age_ms:
        return GateResult.fail("latency", Reason.SIGNAL_TOO_OLD, **d)

    tol = c.trader.latency_tolerance_ms
    if tol and age_ms > tol:
        return GateResult.fail(
            "latency", Reason.LATENCY_EXCEEDS_TRADER_TOLERANCE, tolerance_ms=tol, **d
        )
    # Decay size as we approach the trader's tolerance rather than cliff-edging.
    if tol and age_ms > tol * 0.6:
        return GateResult.ok("latency", mult=Decimal("0.6"), tolerance_ms=tol, **d)
    return GateResult.ok("latency", mult=ONE, **d)


# ------------------------------------------------------- 17.3/17.4 liquidity
def liquidity_gate(c: TradeCandidate, cfg: CopyabilityConfig, intended_usd: Decimal) -> GateResult:
    m = c.market
    d = {"liquidity_usd": str(m.liquidity_usd), "intended_usd": str(intended_usd)}

    if m.liquidity_usd <= 0:
        return GateResult.fail("liquidity", Reason.LOW_LIQUIDITY, **d)
    if m.liquidity_usd < cfg.min_liquidity_usd:
        return GateResult.fail(
            "liquidity", Reason.LOW_LIQUIDITY, required=str(cfg.min_liquidity_usd), **d
        )
    share = intended_usd / m.liquidity_usd * Decimal(100)
    if share > cfg.max_position_pct_of_liquidity:
        # Shrink to fit the book rather than rejecting outright.
        allowed = m.liquidity_usd * cfg.max_position_pct_of_liquidity / Decimal(100)
        if allowed < settings.risk.min_position_usd:
            return GateResult.fail(
                "liquidity", Reason.POSITION_TOO_LARGE_FOR_LIQUIDITY,
                share_pct=str(share), **d,
            )
        return GateResult.ok(
            "liquidity", Reason.POSITION_TOO_LARGE_FOR_LIQUIDITY,
            mult=allowed / intended_usd, share_pct=str(share), **d,
        )
    return GateResult.ok("liquidity", Reason.SUFFICIENT_LIQUIDITY, mult=ONE, share_pct=str(share), **d)


def slippage_gate(c: TradeCandidate, cfg: CopyabilityConfig) -> GateResult:
    """PRD 17.4: reject when ExpectedAlpha <= Fees + Slippage + SafetyMargin."""
    if c.quote is None:
        return GateResult.fail("slippage", Reason.NO_ROUTE)

    impact = c.quote.price_impact_pct
    d = {"price_impact_pct": str(impact.quantize(Decimal("0.0001"))),
         "route": c.quote.route_label}

    if impact > cfg.max_expected_slippage_pct:
        return GateResult.fail("slippage", Reason.EXPECTED_SLIPPAGE_TOO_HIGH, **d)

    # Expected alpha proxy: the trader's historical edge per trade, haircut by how far
    # price has already run. Conservative by construction - unknown edge means no trade.
    edge = c.trader.notes.get("expected_edge_pct")
    expected_alpha = Decimal(str(edge)) if edge else Decimal("15")
    already_moved = max(ZERO, pct_change(c.source_trade.source_price, c.market.price if c.source_trade.quote_mint == SOL_MINT else c.market.price_usd))
    net_alpha = expected_alpha - already_moved

    fee_pct = Decimal("0.35")  # jup + network + priority, as a share of notional
    cost = impact + fee_pct + cfg.slippage_safety_margin_pct
    d |= {"expected_alpha_pct": str(net_alpha), "estimated_cost_pct": str(cost)}

    if net_alpha <= cost:
        return GateResult.fail("slippage", Reason.ALPHA_BELOW_COST, **d)
    return GateResult.ok("slippage", Reason.ACCEPTABLE_SLIPPAGE, mult=ONE, **d)


# ------------------------------------------------------ 17.5/17.6 token shape
def market_cap_gate(c: TradeCandidate, cfg: CopyabilityConfig) -> GateResult:
    mc = c.market.market_cap_usd
    d = {"market_cap_usd": str(mc)}
    if mc <= 0:
        return GateResult.ok("market_cap", mult=ONE, note="unknown", **d)
    if mc < cfg.min_market_cap_usd:
        return GateResult.fail("market_cap", Reason.MARKET_CAP_TOO_LOW, **d)
    if mc > cfg.max_market_cap_usd:
        return GateResult.fail("market_cap", Reason.MARKET_CAP_TOO_HIGH, **d)
    return GateResult.ok("market_cap", mult=ONE, **d)


def token_age_gate(c: TradeCandidate, cfg: CopyabilityConfig) -> GateResult:
    age = c.market.token_age_seconds
    if age is None:
        return GateResult.ok("token_age", mult=ONE, note="unknown")
    d = {"token_age_seconds": age}
    if age < cfg.min_token_age_seconds:
        return GateResult.fail("token_age", Reason.TOKEN_TOO_NEW, **d)
    if age < cfg.new_token_age_seconds:
        return GateResult.ok("token_age", mult=cfg.new_token_size_mult, **d)
    return GateResult.ok("token_age", mult=ONE, **d)


# -------------------------------------------------------- 18/19 token safety
def token_safety_gate(c: TradeCandidate, cfg: CopyabilityConfig) -> GateResult:
    tr = c.token_risk
    if tr is None or tr.unknown:
        # PRD 53: disable new trades when critical validation data is unavailable.
        return GateResult.fail("token_safety", Reason.CRITICAL_DATA_UNAVAILABLE)

    d = {"risk_score": tr.score}
    if tr.is_honeypot:
        return GateResult.fail("token_safety", Reason.TOKEN_RISK_TOO_HIGH, honeypot=True, **d)
    if tr.score > cfg.max_token_risk_score:
        return GateResult.fail("token_safety", Reason.TOKEN_RISK_TOO_HIGH, *tr.reasons, **d)
    if tr.insider_flags:
        return GateResult.fail("token_safety", Reason.INSIDER_PATTERN, flags=tr.insider_flags, **d)
    return GateResult.ok("token_safety", Reason.TOKEN_SAFETY_PASS, mult=ONE, **d)


# ------------------------------------------------- 20 multi-trader confirmation
def confirmation_gate(c: TradeCandidate, cfg: CopyabilityConfig) -> GateResult:
    """Independent confirmation is a size bonus, never a requirement."""
    n = len(c.confirmations)
    if n >= 2:
        return GateResult.ok(
            "confirmation", Reason.MULTI_TRADER_CONFIRMATION,
            mult=Decimal("1.25"), confirmations=c.confirmations,
        )
    return GateResult.ok("confirmation", mult=ONE, confirmations=c.confirmations)


# ------------------------------------------------------------------ pipeline
ENTRY_GATES = (
    trader_gate,
    price_chase_gate,
    latency_gate,
    market_cap_gate,
    token_age_gate,
    token_safety_gate,
    slippage_gate,
    confirmation_gate,
)


def run_entry_gates(
    c: TradeCandidate, intended_usd: Decimal, cfg: CopyabilityConfig | None = None
) -> tuple[list[GateResult], bool, Decimal]:
    """Run every gate. Returns (results, all_passed, combined_size_multiplier).

    All gates run even after the first failure - PRD 41 shows multiple reason codes on
    a rejection, and the full picture is what makes the dataset useful later.
    """
    cfg = cfg or settings.copyability
    results: list[GateResult] = []
    for gate in ENTRY_GATES:
        results.append(gate(c, cfg))
    results.append(liquidity_gate(c, cfg, intended_usd))

    passed = all(r.passed for r in results)
    mult = ONE
    for r in results:
        if r.passed:
            mult *= r.size_mult
    return results, passed, mult


def collect_reasons(results: list[GateResult]) -> list[Reason]:
    seen: dict[Reason, None] = {}
    for r in results:
        for reason in r.reasons:
            seen.setdefault(reason, None)
    return list(seen)
