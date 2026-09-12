"""Jupiter: route + swap transaction, and the honest liquidity/slippage oracle.

PRD 17.3/17.4 say "reject when liquidity is insufficient" and "estimate expected
slippage". Rather than reimplementing AMM math per venue, we quote the *actual*
intended size and read the router's own price impact. A depth probe at 2x size
tells us whether the book is thin beyond our fill.
"""
from __future__ import annotations

from decimal import Decimal

from asm.adapters.base import HttpAdapter, ProviderError
from asm.config import settings
from asm.domain.models import Quote
from asm.domain.money import SOL_MINT
from asm.logging import get_logger

log = get_logger(__name__)


class JupiterClient(HttpAdapter):
    provider = "jupiter"
    timeout = 5.0
    retries = 1

    def __init__(self):
        headers = {"Accept": "application/json"}
        if settings.jupiter_api_key:
            headers["x-api-key"] = settings.jupiter_api_key
        super().__init__(base_url=settings.jupiter_base_url, headers=headers)

    async def quote(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int | None = None,
        only_direct: bool = False,
    ) -> Quote | None:
        if amount_raw <= 0:
            return None
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_raw)),
            "slippageBps": str(slippage_bps if slippage_bps is not None else settings.copyability.execution_slippage_bps),
            "onlyDirectRoutes": str(only_direct).lower(),
            "restrictIntermediateTokens": "true",
        }
        try:
            data = await self.get("/swap/v1/quote", params=params)
        except ProviderError as exc:
            log.warning("jupiter_quote_failed", mint=output_mint, error=str(exc))
            return None
        if not data or not data.get("outAmount"):
            return None

        labels = [
            step.get("swapInfo", {}).get("label", "")
            for step in data.get("routePlan", [])
        ]
        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount_raw=int(data["inAmount"]),
            out_amount_raw=int(data["outAmount"]),
            other_amount_threshold_raw=int(data.get("otherAmountThreshold", 0)),
            # Jupiter returns priceImpactPct as a fraction (0.0123 == 1.23%)
            price_impact_pct=Decimal(str(data.get("priceImpactPct") or "0")) * Decimal(100),
            slippage_bps=int(data.get("slippageBps", 0)),
            route_label=" -> ".join(x for x in labels if x),
            route_plan=data.get("routePlan", []),
            raw=data,
        )

    async def depth_probe(
        self, *, input_mint: str, output_mint: str, amount_raw: int, multiple: int = 2
    ) -> Decimal | None:
        """Price impact at N x our size. A cliff here means the book is thin."""
        q = await self.quote(
            input_mint=input_mint, output_mint=output_mint, amount_raw=amount_raw * multiple
        )
        return q.price_impact_pct if q else None

    async def exit_liquidity(self, *, mint: str, amount_raw: int, decimals: int) -> Quote | None:
        """PRD 28.7 - can we actually get out? Quote the reverse direction."""
        return await self.quote(
            input_mint=mint, output_mint=SOL_MINT, amount_raw=amount_raw
        )

    async def build_swap(
        self,
        *,
        quote: Quote,
        user_pubkey: str,
        priority_fee_lamports: int | None = None,
        wrap_unwrap_sol: bool = True,
    ) -> dict:
        """Returns the unsigned base64 versioned transaction."""
        body = {
            "quoteResponse": quote.raw,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": wrap_unwrap_sol,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": (
                {"priorityLevelWithMaxLamports": {
                    "maxLamports": priority_fee_lamports or settings.priority_fee_lamports,
                    "priorityLevel": "high",
                }}
            ),
        }
        return await self.post("/swap/v1/swap", json=body)

    async def price_usd(self, mints: list[str]) -> dict[str, Decimal]:
        if not mints:
            return {}
        try:
            data = await self.get("/price/v3", params={"ids": ",".join(mints[:50])})
        except ProviderError:
            return {}
        out: dict[str, Decimal] = {}
        payload = data.get("data", data) if isinstance(data, dict) else {}
        for mint, row in (payload or {}).items():
            price = row.get("usdPrice", row.get("price")) if isinstance(row, dict) else row
            if price is not None:
                out[mint] = Decimal(str(price))
        return out

    async def sol_price_usd(self) -> Decimal:
        prices = await self.price_usd([SOL_MINT])
        return prices.get(SOL_MINT, Decimal(0))


_client: JupiterClient | None = None


def jupiter() -> JupiterClient:
    global _client
    if _client is None:
        _client = JupiterClient()
    return _client
