"""Market state assembly for the hot path (PRD 16.8).

Everything here is either cached in Redis or fetched from Jupiter/Helius concurrently.
A market snapshot older than STALE_AFTER is flagged, and a flagged snapshot fails the
gates rather than being silently trusted.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from asm.adapters.helius import helius
from asm.adapters.jupiter import jupiter
from asm.domain.models import MarketState
from asm.domain.money import SOL_MINT, ZERO, raw_to_ui, ui_to_raw
from asm.logging import get_logger
from asm.state import keys
from asm.state.client import get_redis

log = get_logger(__name__)
MARKET_TTL = 5          # seconds - prices move fast; this only absorbs bursts
META_TTL = 3600
STALE_AFTER = 10        # seconds
SOL_PRICE_TTL = 30
_PROBE_USD = Decimal("100")

# Token supply is the only Helius call on the marking path, and the position service
# marks every open position continuously. At a 5s market TTL that is ~17k credits per
# day PER POSITION - which alone exhausts a 1M/month plan with six positions open.
#
# Supply is effectively static once the mint authority is revoked (it cannot be
# re-enabled), so it is cached for a day. While mint authority is still active the
# supply genuinely can change - but such a token fails the safety gate anyway, so it is
# cached briefly and never traded.
SUPPLY_TTL_FIXED = 86_400
SUPPLY_TTL_MUTABLE = 300


async def sol_price_usd() -> Decimal:
    r = get_redis()
    cached = await r.get("asm:cache:solprice")
    if cached:
        return Decimal(cached)
    price = await jupiter().sol_price_usd()
    if price > 0:
        await r.set("asm:cache:solprice", str(price), ex=SOL_PRICE_TTL)
    return price


async def get_market_state(mint: str, *, decimals: int = 9) -> MarketState:
    r = get_redis()
    cached = await r.get(keys.market(mint))
    if cached:
        state = MarketState.model_validate_json(cached)
        age = (datetime.now(UTC) - state.fetched_at).total_seconds()
        if age < MARKET_TTL:
            return state

    state = await _build(mint, decimals)
    if state.price > 0:
        await r.set(keys.market(mint), state.model_dump_json(), ex=MARKET_TTL * 3)
    return state


async def _build(mint: str, decimals: int) -> MarketState:
    jup = jupiter()
    sol_usd = await sol_price_usd()

    probe_lamports = ui_to_raw(_PROBE_USD / sol_usd, 9) if sol_usd > 0 else 0

    quote_task = jup.quote(
        input_mint=SOL_MINT, output_mint=mint, amount_raw=probe_lamports
    ) if probe_lamports else _none()
    supply_task = _token_supply(mint)
    price_task = jup.price_usd([mint])

    quote, supply, prices = await asyncio.gather(
        quote_task, supply_task, price_task, return_exceptions=True
    )
    quote = None if isinstance(quote, BaseException) else quote
    supply = None if isinstance(supply, BaseException) else supply
    prices = {} if isinstance(prices, BaseException) else prices

    price_usd = prices.get(mint, ZERO)
    tok_decimals = int((supply or {}).get("decimals", decimals))

    # Derive price from the router when the price API has no listing yet.
    if price_usd <= 0 and quote and quote.out_amount_raw:
        out_ui = raw_to_ui(quote.out_amount_raw, tok_decimals)
        if out_ui > 0:
            price_usd = _PROBE_USD / out_ui

    price_sol = (price_usd / sol_usd) if sol_usd > 0 else ZERO

    market_cap = ZERO
    if supply and price_usd > 0:
        total = raw_to_ui(int(supply.get("amount", 0) or 0), tok_decimals)
        market_cap = total * price_usd

    # Liquidity proxy: invert the router's own price impact on a known probe size.
    # impact ~= size / depth, so depth ~= size / impact. Crude but venue-agnostic,
    # and it is measured against the exact path we would actually trade.
    liquidity = ZERO
    if quote and quote.price_impact_pct > 0:
        liquidity = _PROBE_USD / (quote.price_impact_pct / Decimal(100))
    elif quote:
        liquidity = Decimal("1000000")  # impact rounded to zero => deep

    return MarketState(
        mint=mint,
        price=price_sol,
        price_usd=price_usd,
        liquidity_usd=liquidity,
        market_cap_usd=market_cap,
        decimals=tok_decimals,
        token_age_seconds=await _token_age(mint),
        fetched_at=datetime.now(UTC),
        stale=price_usd <= 0,
    )


async def _none():
    return None


async def _token_supply(mint: str) -> dict | None:
    """Cached token supply. Keeps Helius off the continuous marking path."""
    r = get_redis()
    k = f"asm:cache:supply:{mint}"
    cached = await r.get(k)
    if cached:
        import orjson

        return orjson.loads(cached)

    try:
        supply = await helius().get_token_supply(mint)
    except Exception:
        return None
    if not supply:
        return None

    # Only treat supply as fixed once the mint authority is gone.
    ttl = SUPPLY_TTL_MUTABLE
    try:
        info = await helius().get_mint_account(mint)
        if info is not None and not info.get("mintAuthority"):
            ttl = SUPPLY_TTL_FIXED
    except Exception:
        pass

    import orjson

    await r.set(k, orjson.dumps(supply), ex=ttl)
    return supply


async def _token_age(mint: str) -> int | None:
    """First observed signature is a good enough proxy for launch time."""
    r = get_redis()
    k = f"asm:cache:tokenage:{mint}"
    cached = await r.get(k)
    if cached:
        created = datetime.fromtimestamp(int(cached), tz=UTC)
        return int((datetime.now(UTC) - created).total_seconds())
    try:
        sigs = await helius().get_signatures_for_address(mint, limit=1000)
    except Exception:
        return None
    if not sigs:
        return None
    oldest = sigs[-1].get("blockTime")
    if not oldest:
        return None
    await r.set(k, str(oldest), ex=META_TTL * 24)
    return int((datetime.now(UTC) - datetime.fromtimestamp(oldest, tz=UTC)).total_seconds())
