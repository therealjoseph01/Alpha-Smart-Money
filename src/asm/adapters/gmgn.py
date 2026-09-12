"""GMGN Agent API - candidate discovery and cheap pre-screening (PRD 9, 11).

Official API, documented endpoints, API-key auth. Read queries need only the key;
signing is for trading, which we deliberately do not use - execution stays on Jupiter,
where there is no platform fee and no third party in the path that moves money.

The role of this adapter, and its limit:

    GMGN answers "who is worth LOOKING at?"
    It does not answer "who is worth COPYING?"

That second question is the entire product (PRD 5.1), and only our own scoring - what we
actually captured, at our latency, after our fees - is allowed to answer it. GMGN's
numbers are stored as metadata and used to pre-screen. They never reach the composite
score and never move capital.

Why pre-screening matters commercially: a deep Helius backfill costs ~300 credits per
wallet. `wallet_profits` takes 100 wallets in ONE rate-limited call with no monthly
quota. Screening with GMGN first, and only backfilling survivors, is the difference
between evaluating 50 wallets a month and evaluating a thousand.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from asm.adapters.base import HttpAdapter, ProviderError
from asm.config import settings
from asm.domain.enums import Chain, TraderStatus
from asm.domain.models import TraderProfile
from asm.logging import get_logger

log = get_logger(__name__)

BASE_URL = "https://openapi.gmgn.ai"
MAX_WALLETS_PER_CALL = 100

# Endpoint weights, from GMGN's published rate table. calls/sec = plan weight / weight.
# The free plan is weight 5, so smartmoney runs at 5/s and wallet_profits at ~1.7/s.
WEIGHTS = {
    "/v1/user/smartmoney": 1,
    "/v1/user/kol": 1,
    "/v1/user/wallet_stats": 3,
    "/v1/user/wallet_profits": 3,
    "/v1/user/wallet_activity": 3,
    "/v1/token/security": 1,
    "/v1/token/info": 1,
    "/v1/market/token_top_traders": 5,
}


@runtime_checkable
class DiscoverySource(Protocol):
    """Swappable so a provider outage pauses discovery without touching trading."""

    name: str

    async def discover_wallets(self, *, limit: int) -> list[str]: ...
    async def wallet_profits(self, wallets: list[str], *, period: str) -> dict[str, dict]: ...
    async def token_security(self, mint: str) -> dict[str, Any]: ...


def _dec(v: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "", False) else Decimal(default)
    except Exception:
        return Decimal(default)


class GmgnClient(HttpAdapter):
    provider = "gmgn"
    name = "gmgn"
    timeout = 15.0
    retries = 1

    def __init__(self, api_key: str | None = None, plan_weight: int | None = None):
        key = api_key if api_key is not None else settings.gmgn_api_key
        super().__init__(
            base_url=BASE_URL,
            # X-APIKEY, not Authorization: Bearer. Verified against the official
            # gmgn-cli client rather than inferred - every Bearer variant returns
            # AUTH_INVALID with a message that does not say why.
            headers={
                "X-APIKEY": key,
                "Content-Type": "application/json",
                "User-Agent": "asm/0.1",
            },
            breaker_threshold=3,
            breaker_cooldown=120.0,
        )
        self._key = key
        self.plan_weight = plan_weight or settings.gmgn_plan_weight
        self._last: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._key) and settings.gmgn_enabled and not self.breaker.is_open

    async def _pace(self, path: str) -> None:
        """Respect the per-endpoint rate: calls/sec = plan weight / endpoint weight."""
        import asyncio

        weight = WEIGHTS.get(path, 3)
        min_interval = weight / max(1, self.plan_weight)
        now = asyncio.get_event_loop().time()
        wait = min_interval - (now - self._last.get(path, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        self._last[path] = asyncio.get_event_loop().time()

    @staticmethod
    def _auth_query() -> dict[str, Any]:
        """Every request carries a unix timestamp and a fresh client_id.

        Not documented as required, but the API returns AUTH_INVALID without them -
        with a message that blames the key, which is a long way from the real cause.
        """
        import time
        import uuid

        return {"timestamp": int(time.time()), "client_id": str(uuid.uuid4())}

    async def _get(self, path: str, **params) -> Any:
        await self._pace(path)
        query = {k: v for k, v in params.items() if v is not None}
        query.update(self._auth_query())
        data = await self.get(path, params=query)
        return self._unwrap(data)

    async def _post(self, path: str, body: dict) -> Any:
        await self._pace(path)
        data = await self.post(path, params=self._auth_query(), json=body)
        return self._unwrap(data)

    @staticmethod
    def _unwrap(data: Any) -> Any:
        if isinstance(data, dict):
            if data.get("code") not in (0, 200, None):
                raise ProviderError("gmgn", str(data.get("msg", data.get("message", "error")))[:160])
            return data.get("data", data)
        return data

    # ------------------------------------------------------------ discovery
    async def discover_wallets(self, *, limit: int = 100, chain: str = "sol",
                               include_kol: bool = True) -> list[str]:
        """Distinct wallets currently active in the smart-money / KOL feeds.

        The feed is a stream of TRADES, not a leaderboard - each record carries a
        `maker`. Taking the distinct makers surfaces wallets that are trading NOW,
        which is a better candidate pool than an all-time ranking: a wallet that
        stopped trading in March cannot be copied today.
        """
        if not self.enabled:
            return []

        seen: dict[str, dict] = {}
        for path in ("/v1/user/smartmoney", *(("/v1/user/kol",) if include_kol else ())):
            try:
                data = await self._get(path, chain=chain, limit=limit)
            except ProviderError as exc:
                log.warning("gmgn_discovery_failed", path=path, error=str(exc))
                continue
            rows = data.get("list", data) if isinstance(data, dict) else (data or [])
            for row in rows or []:
                wallet = row.get("maker") or row.get("wallet_address") or row.get("address")
                if not wallet:
                    continue
                info = row.get("maker_info") or {}
                entry = seen.setdefault(wallet, {
                    "tags": info.get("tags") or [],
                    "twitter": info.get("twitter_username"),
                    "source": "smartmoney" if "smartmoney" in path else "kol",
                    "trades_seen": 0,
                })
                entry["trades_seen"] += 1

        self._last_discovery = seen
        log.info("gmgn_discovered", wallets=len(seen))
        return list(seen)

    def discovery_meta(self) -> dict[str, dict]:
        return getattr(self, "_last_discovery", {})

    # -------------------------------------------------------- pre-screening
    async def wallet_profits(self, wallets: list[str], *, period: str = "30d",
                             chain: str = "sol") -> dict[str, dict]:
        """Bulk P&L for up to 100 wallets in ONE call. The cheap screen."""
        if not self.enabled or not wallets:
            return {}

        out: dict[str, dict] = {}
        for i in range(0, len(wallets), MAX_WALLETS_PER_CALL):
            batch = wallets[i:i + MAX_WALLETS_PER_CALL]
            try:
                data = await self._post("/v1/user/wallet_profits", {
                    "chain": chain, "period": period, "wallet_addresses": batch,
                })
            except ProviderError as exc:
                log.warning("gmgn_profits_failed", batch=len(batch), error=str(exc))
                continue
            rows = data.get("list", data) if isinstance(data, dict) else (data or [])
            for row in rows or []:
                wallet = row.get("wallet_address") or row.get("wallet")
                if wallet:
                    out[wallet] = row
        return out

    async def wallet_stats(self, wallet: str, *, period: str = "30d",
                           chain: str = "sol") -> dict[str, Any]:
        if not self.enabled:
            return {}
        try:
            return await self._get("/v1/user/wallet_stats", chain=chain,
                                   wallet_address=wallet, period=period) or {}
        except ProviderError as exc:
            log.warning("gmgn_wallet_stats_failed", wallet=wallet[:8], error=str(exc))
            return {}

    async def token_security(self, mint: str, *, chain: str = "sol") -> dict[str, Any]:
        """PRD 18 - one input to token safety, never the only one."""
        if not self.enabled:
            return {}
        try:
            return await self._get("/v1/token/security", chain=chain, token_address=mint) or {}
        except ProviderError:
            return {}

    async def token_top_traders(self, mint: str, *, limit: int = 50,
                                chain: str = "sol") -> list[str]:
        if not self.enabled:
            return []
        try:
            data = await self._get("/v1/market/token_top_traders", chain=chain,
                                   address=mint, limit=limit)
        except ProviderError:
            return []
        rows = data.get("list", data) if isinstance(data, dict) else (data or [])
        return [r.get("address") or r.get("wallet_address") for r in rows or []
                if r.get("address") or r.get("wallet_address")]


def screen(profits: dict[str, Any], *, min_realized_usd: Decimal,
           min_trades: int, min_winrate: Decimal) -> tuple[bool, str]:
    """Cheap first filter. Deliberately permissive.

    This decides only whether a wallet is worth spending ~300 Helius credits to analyse
    properly. It must not try to decide whether the wallet is good - a high GMGN P&L is
    exactly the "profitable but uncopyable" trap the product exists to catch. Screening
    out the obviously dead and obviously tiny is all it is for.
    """
    realized = _dec(profits.get("realized_profit"))
    total = _dec(profits.get("total_profit"), str(realized))
    buys = int(profits.get("buy", 0) or 0)
    sells = int(profits.get("sell", 0) or 0)
    trades = buys + sells

    if trades < min_trades:
        return False, f"only {trades} trades in period"
    if total <= 0 and realized <= 0:
        return False, "no profit in period"
    if realized < min_realized_usd:
        return False, f"realized ${realized} below ${min_realized_usd} floor"

    winrate = _dec(profits.get("winrate"))
    if winrate and winrate <= 1:
        winrate *= Decimal(100)
    if winrate and winrate < min_winrate:
        return False, f"winrate {winrate}% below {min_winrate}%"

    return True, "passed screen"


def to_profile(wallet: str, profits: dict[str, Any], meta: dict | None = None) -> TraderProfile:
    """Build a candidate profile. Scores stay ZERO - GMGN's numbers are metadata only.

    A trader must earn its score from our own backfill (PRD 5.3), so nothing here may
    populate composite, copyability or confidence.
    """
    meta = meta or {}
    winrate = _dec(profits.get("winrate"))
    if winrate and winrate <= 1:
        winrate *= Decimal(100)

    return TraderProfile(
        wallet=wallet,
        chain=Chain.SOLANA,
        status=TraderStatus.DISCOVERED,
        source=f"gmgn:{meta.get('source', 'smartmoney')}",
        win_rate=winrate or None,
        trade_count=int(profits.get("buy", 0) or 0) + int(profits.get("sell", 0) or 0),
        notes={
            "gmgn_realized_profit": str(_dec(profits.get("realized_profit"))),
            "gmgn_total_profit": str(_dec(profits.get("total_profit"))),
            "gmgn_total_cost": str(_dec(profits.get("total_cost"))),
            "gmgn_tags": meta.get("tags", []),
            "gmgn_twitter": meta.get("twitter"),
            "gmgn_screened_at": datetime.now(UTC).isoformat(),
            "_note": "GMGN figures are screening metadata and never enter the score",
        },
    )


class NullDiscovery:
    """Used when GMGN is disabled or down. Discovery pauses; trading does not."""

    name = "null"

    async def discover_wallets(self, *, limit: int = 100, **kw) -> list[str]:
        return []

    async def wallet_profits(self, wallets: list[str], **kw) -> dict[str, dict]:
        return {}

    async def token_security(self, mint: str, **kw) -> dict[str, Any]:
        return {}

    def discovery_meta(self) -> dict[str, dict]:
        return {}


_client: GmgnClient | None = None


def gmgn() -> GmgnClient:
    global _client
    if _client is None:
        _client = GmgnClient()
    return _client


def discovery_source() -> DiscoverySource:
    client = gmgn()
    return client if client.enabled else NullDiscovery()
