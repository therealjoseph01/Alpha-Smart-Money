"""Helius: detection, enrichment, priority fees, and submission (PRD 10, 26, 27)."""
from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import orjson
import websockets

from asm.adapters.base import HttpAdapter, ProviderError
from asm.config import settings
from asm.domain.money import lamports_to_sol
from asm.logging import get_logger

log = get_logger(__name__)
_ids = itertools.count(1)

# Credits per call, from Helius billing docs. Anything unlisted is a standard RPC call.
CREDIT_COST = {
    "walletHistory": 100,      # Enhanced Transactions API
    "parsedTransactions": 100,  # Enhanced Transactions API
    "getProgramAccounts": 10,
    "sendFast": 0,              # Sender is free
    "wsNotification": 0,        # streaming is metered by volume, not per message
}

# The free tier caps Enhanced APIs at 2 req/s - far below the 10 rps for standard RPC.
# They need their own lane or the expensive calls trigger 429s and get retried.
ENHANCED_RPS = 2


class HeliusClient(HttpAdapter):
    provider = "helius"
    timeout = 10.0
    retries = 2

    def __init__(self):
        super().__init__(base_url="", headers={"Content-Type": "application/json"})
        self.rpc_url = settings.rpc_url
        self.fallback_url = settings.rpc_fallback_url
        # Simple leaky bucket. A 429 costs a retry and still burns quota, so pacing
        # below the limit is cheaper than discovering it.
        self._min_interval = 1.0 / max(1, settings.helius_max_rps)
        self._last_call = 0.0
        self._throttle = asyncio.Lock()
        self._enhanced_interval = 1.0 / ENHANCED_RPS
        self._last_enhanced = 0.0
        self._enhanced_throttle = asyncio.Lock()

    async def _count(self, method: str) -> None:
        """Meter every billed call at its ACTUAL credit cost.

        Costs confirmed from Helius billing docs (Sept 2026):
          standard RPC .................. 1
          getProgramAccounts ............ 10
          Enhanced Transactions API ..... 100   <- dominates everything else
          Priority Fee API .............. 1
          sendTransaction ............... 1
          Sender (fast submit) .......... 0

        Counting calls rather than credits understates wallet history by 100x, which is
        the difference between "comfortable" and "out of credits on the 9th".
        """
        from datetime import UTC, datetime

        from asm.state.client import get_redis

        try:
            cost = CREDIT_COST.get(method, 1)
            r = get_redis()
            day = datetime.now(UTC).strftime("%Y%m%d")
            async with r.pipeline(transaction=False) as p:
                p.hincrby(f"asm:credits:{day}", method, cost)
                p.hincrby(f"asm:credits:{day}", "_total", cost)
                p.hincrby(f"asm:credits:{day}", "_calls", 1)
                p.expire(f"asm:credits:{day}", 86_400 * 40)
                await p.execute()
        except Exception:
            pass  # metering must never break a trade

    async def _pace(self) -> None:
        async with self._throttle:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_event_loop().time()

    async def _pace_enhanced(self) -> None:
        """Separate, slower lane for the 100-credit Enhanced API calls."""
        async with self._enhanced_throttle:
            now = asyncio.get_event_loop().time()
            wait = self._enhanced_interval - (now - self._last_enhanced)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_enhanced = asyncio.get_event_loop().time()

    # -------------------------------------------------------------- JSON-RPC
    async def rpc(self, method: str, params: list | None = None, *, use_fallback: bool = False) -> Any:
        """PRD 53 - never assume a transaction failed because one RPC timed out."""
        url = self.fallback_url if use_fallback else self.rpc_url
        if not use_fallback:
            await self._pace()
            await self._count(method)
        body = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params or []}
        try:
            data = await self.post(url, json=body)
        except ProviderError:
            if use_fallback:
                raise
            log.warning("rpc_failover", method=method)
            return await self.rpc(method, params, use_fallback=True)
        if "error" in data:
            raise ProviderError(self.provider, str(data["error"])[:200])
        return data.get("result")

    async def get_transaction(self, signature: str) -> dict | None:
        return await self.rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                         "commitment": "confirmed"}],
        )

    async def get_signatures_for_address(self, address: str, limit: int = 100, before: str | None = None):
        opts: dict[str, Any] = {"limit": limit}
        if before:
            opts["before"] = before
        return await self.rpc("getSignaturesForAddress", [address, opts]) or []

    async def get_balance_sol(self, pubkey: str) -> Decimal:
        res = await self.rpc("getBalance", [pubkey])
        return lamports_to_sol(int((res or {}).get("value", 0)))

    async def get_token_accounts(self, owner: str) -> list[dict]:
        res = await self.rpc(
            "getTokenAccountsByOwner",
            [owner, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
             {"encoding": "jsonParsed"}],
        )
        return (res or {}).get("value", [])

    async def get_token_supply(self, mint: str) -> dict | None:
        res = await self.rpc("getTokenSupply", [mint])
        return (res or {}).get("value")

    async def get_largest_token_accounts(self, mint: str) -> list[dict]:
        res = await self.rpc("getTokenLargestAccounts", [mint])
        return (res or {}).get("value", [])

    async def get_mint_account(self, mint: str) -> dict | None:
        """Mint/freeze authority for PRD 18 token safety."""
        res = await self.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        value = (res or {}).get("value")
        if not value:
            return None
        return value.get("data", {}).get("parsed", {}).get("info")

    async def get_latest_blockhash(self) -> tuple[str, int]:
        res = await self.rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
        v = (res or {}).get("value", {})
        return v.get("blockhash", ""), v.get("lastValidBlockHeight", 0)

    async def simulate(self, tx_b64: str) -> dict:
        return await self.rpc(
            "simulateTransaction",
            [tx_b64, {"encoding": "base64", "commitment": "confirmed",
                      "replaceRecentBlockhash": True, "sigVerify": False}],
        )

    async def send_transaction(self, tx_b64: str, *, skip_preflight: bool = True,
                               max_retries: int = 0) -> str:
        return await self.rpc(
            "sendTransaction",
            [tx_b64, {"encoding": "base64", "skipPreflight": skip_preflight,
                      "maxRetries": max_retries, "preflightCommitment": "confirmed"}],
        )

    async def confirm(self, signature: str, deadline_seconds: float = 45.0,
                      poll: float = 0.4) -> dict | None:
        """Poll until confirmed. Returns the status object, or None on timeout."""
        deadline = asyncio.get_event_loop().time() + deadline_seconds
        while asyncio.get_event_loop().time() < deadline:
            res = await self.rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            status = ((res or {}).get("value") or [None])[0]
            if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
                return status
            if status and status.get("err"):
                return status
            await asyncio.sleep(poll)
        return None

    # ------------------------------------------------------- priority fees
    async def priority_fee(self, account_keys: list[str] | None = None,
                           level: str = "high") -> int:
        """PRD 27 - optimize for net realized return, not raw speed."""
        if not settings.helius_api_key:
            return settings.priority_fee_lamports
        try:
            data = await self.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0", "id": next(_ids), "method": "getPriorityFeeEstimate",
                    "params": [{"accountKeys": account_keys or [],
                                "options": {"priorityLevel": level.capitalize(),
                                            "evaluateEmptySlotAsZero": True}}],
                },
            )
            est = int(data.get("result", {}).get("priorityFeeEstimate", 0) or 0)
        except (ProviderError, ValueError, TypeError):
            est = settings.priority_fee_lamports
        return max(1_000, min(est or settings.priority_fee_lamports,
                              settings.max_priority_fee_lamports))

    # --------------------------------------------------- enhanced endpoints
    async def parsed_transactions(self, signatures: list[str]) -> list[dict]:
        """Enhanced Transactions API - human-readable swap events. Cold path."""
        if not settings.helius_api_key or not signatures:
            return []
        await self._pace_enhanced()
        await self._count("parsedTransactions")
        try:
            return await self.post(
                f"https://api.helius.xyz/v0/transactions?api-key={settings.helius_api_key}",
                json={"transactions": signatures[:100]},
            ) or []
        except ProviderError as exc:
            log.warning("helius_parsed_failed", error=str(exc))
            return []

    async def wallet_history(self, address: str, *, limit: int = 100,
                             before: str | None = None, tx_type: str = "SWAP") -> list[dict]:
        """Backfill for PRD 12 wallet intelligence and PRD 44 backtests."""
        if not settings.helius_api_key:
            return []
        await self._pace_enhanced()
        await self._count("walletHistory")
        params = {"api-key": settings.helius_api_key, "limit": str(limit)}
        if before:
            params["before"] = before
        if tx_type:
            params["type"] = tx_type
        try:
            return await self.get(
                f"https://api.helius.xyz/v0/addresses/{address}/transactions",
                params=params,
            ) or []
        except ProviderError as exc:
            log.warning("helius_history_failed", address=address, error=str(exc))
            return []

    async def token_metadata(self, mints: list[str]) -> list[dict]:
        if not settings.helius_api_key or not mints:
            return []
        try:
            return await self.post(
                f"https://api.helius.xyz/v0/token-metadata?api-key={settings.helius_api_key}",
                json={"mintAccounts": mints[:100], "includeOffChain": False,
                      "disableCache": False},
            ) or []
        except ProviderError:
            return []

    # -------------------------------------------------------- fast submission
    async def send_fast(self, tx_b64: str) -> str:
        """Helius Sender: dual-routes to validators + Jito. Requires a tip instruction."""
        await self._count("sendFast")
        data = await self.post(
            settings.helius_sender_url,
            json={"jsonrpc": "2.0", "id": next(_ids), "method": "sendTransaction",
                  "params": [tx_b64, {"encoding": "base64", "skipPreflight": True,
                                      "maxRetries": 0}]},
        )
        if "error" in data:
            raise ProviderError(self.provider, str(data["error"])[:200])
        return data["result"]


class WalletSubscription:
    """Live detection (PRD 16).

    Uses logsSubscribe with `mentions` per wallet. This is the portable path that
    works on any RPC. Upgrade target is Helius LaserStream/Geyser gRPC with
    `transactions.accountInclude` - typically 1-3s faster, which on a sub-1%
    price-move gate is the difference between a fill and a reject.
    """

    def __init__(self, wallets: list[str], on_signature):
        self.wallets = list(wallets)
        self.on_signature = on_signature
        self._subs: dict[int, str] = {}
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    settings.ws_url, ping_interval=20, ping_timeout=20,
                    max_queue=2048, close_timeout=5,
                ) as ws:
                    await self._subscribe_all(ws)
                    backoff = 1.0
                    log.info("wallet_subscription_open", wallets=len(self.wallets))
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("wallet_subscription_reconnect", error=repr(exc), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _subscribe_all(self, ws) -> None:
        self._subs.clear()
        for wallet in self.wallets:
            rid = next(_ids)
            self._subs[rid] = wallet
            await ws.send(orjson.dumps({
                "jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                "params": [{"mentions": [wallet]}, {"commitment": "confirmed"}],
            }).decode())

    async def _handle(self, raw: str | bytes) -> None:
        await helius()._count("wsNotification")
        msg = orjson.loads(raw)
        if msg.get("method") != "logsNotification":
            return
        value = msg.get("params", {}).get("result", {}).get("value", {})
        signature = value.get("signature")
        if not signature or value.get("err"):
            return
        ctx = msg.get("params", {}).get("result", {}).get("context", {})
        await self.on_signature(signature, ctx.get("slot", 0), value.get("logs", []))

    async def iter_signatures(self) -> AsyncIterator[tuple[str, int]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=4096)

        async def push(sig, slot, _logs):
            try:
                queue.put_nowait((sig, slot))
            except asyncio.QueueFull:
                log.error("signature_queue_full", dropped=sig)

        self.on_signature = push
        task = asyncio.create_task(self.run())
        try:
            while True:
                yield await queue.get()
        finally:
            task.cancel()


_client: HeliusClient | None = None


def helius() -> HeliusClient:
    global _client
    if _client is None:
        _client = HeliusClient()
    return _client
