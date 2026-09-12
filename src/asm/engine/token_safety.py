"""PRD 18/19 - token safety and manipulation detection.

Scored 0 (clean) to 100 (do not touch). An *unknown* is never treated as clean:
`unknown=True` makes the gate fail closed (PRD 53).
"""
from __future__ import annotations

from decimal import Decimal

from asm.adapters.helius import helius
from asm.domain.enums import Reason
from asm.domain.models import TokenRisk
from asm.logging import get_logger
from asm.state import keys
from asm.state.client import get_redis

log = get_logger(__name__)
# Mint and freeze authority revocation is one-way on Solana - a revoked authority
# cannot come back - so a longer cache cannot make a dangerous token look safe. Holder
# concentration does drift, which is why this is 15 minutes rather than hours.
CACHE_TTL = 900
BURN_ADDRESSES = {"11111111111111111111111111111111", "1nc1nerator11111111111111111111111111111111"}


async def evaluate_token(mint: str, *, deployer: str | None = None,
                         use_cache: bool = True) -> TokenRisk:
    r = get_redis()
    if use_cache:
        cached = await r.get(keys.token_risk(mint))
        if cached:
            return TokenRisk.model_validate_json(cached)

    risk = await _evaluate(mint, deployer)
    if not risk.unknown:
        await r.set(keys.token_risk(mint), risk.model_dump_json(), ex=CACHE_TTL)
    return risk


async def _evaluate(mint: str, deployer: str | None) -> TokenRisk:
    h = helius()
    score = 0
    reasons: list[Reason] = []
    flags: list[str] = []

    try:
        info = await h.get_mint_account(mint)
    except Exception as exc:
        log.warning("token_safety_rpc_failed", mint=mint, error=repr(exc))
        return TokenRisk(mint=mint, score=100, unknown=True)

    if not info or not {"mintAuthority", "freezeAuthority", "supply"} <= info.keys():
        return TokenRisk(mint=mint, score=100, unknown=True)

    mint_auth = info.get("mintAuthority")
    freeze_auth = info.get("freezeAuthority")
    supply = int(info.get("supply", 0) or 0)

    # An active mint authority means the supply can be inflated under us.
    if mint_auth:
        score += 35
        reasons.append(Reason.MINT_AUTHORITY_ACTIVE)
    if freeze_auth:
        score += 30
        reasons.append(Reason.FREEZE_AUTHORITY_ACTIVE)

    top10_pct: Decimal | None = None
    try:
        largest = await h.get_largest_token_accounts(mint)
        if largest and supply:
            top10 = sum(int(a.get("amount", 0) or 0) for a in largest[:10])
            top10_pct = Decimal(top10) / Decimal(supply) * Decimal(100)
            if top10_pct > 70:
                score += 30
                reasons.append(Reason.HOLDER_CONCENTRATION)
            elif top10_pct > 50:
                score += 15
                reasons.append(Reason.HOLDER_CONCENTRATION)
            elif top10_pct > 35:
                score += 5
    except Exception as exc:
        log.debug("holder_check_failed", mint=mint, error=repr(exc))
        score += 10
        flags.append("holders_unknown")

    if deployer:
        flags.append(f"deployer:{deployer}")

    return TokenRisk(
        mint=mint,
        score=min(score, 100),
        mint_authority_active=bool(mint_auth),
        freeze_authority_active=bool(freeze_auth),
        top10_holder_pct=top10_pct,
        reasons=reasons,
        insider_flags=flags,
        unknown=top10_pct is None,
    )


async def detect_insider_pattern(
    *, trader_wallet: str, mint: str, source_value_usd: Decimal, token_age_seconds: int | None
) -> list[str]:
    """PRD 19 - cheap heuristics that run inline. Deep graph work is a cold-path job."""
    flags: list[str] = []
    if token_age_seconds is not None and token_age_seconds < 120:
        flags.append("bought_within_2min_of_launch")
    if source_value_usd > 0 and source_value_usd < Decimal("5"):
        flags.append("dust_trade_possible_bait")
    return flags
