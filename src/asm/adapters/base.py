"""PRD 53 - external dependency resilience.

Every outbound provider goes through this: bounded retries, exponential backoff,
a circuit breaker, and an explicit degraded signal the decision engine can read.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from asm.domain.enums import EventType
from asm.logging import get_logger

log = get_logger(__name__)


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, status: int | None = None):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status


class ProviderDegraded(ProviderError):
    """Raised when the breaker is open. Callers must decide whether to proceed."""


@dataclass
class CircuitBreaker:
    """Closed -> open after N consecutive failures -> half-open after cooldown."""

    name: str
    threshold: int = 5
    cooldown: float = 30.0
    failures: int = 0
    opened_at: float = 0.0
    _half_open: bool = field(default=False, repr=False)

    @property
    def is_open(self) -> bool:
        if self.opened_at == 0:
            return False
        if time.monotonic() - self.opened_at >= self.cooldown:
            self._half_open = True
            return False
        return True

    def record_success(self) -> None:
        if self.failures or self.opened_at:
            log.info("breaker_closed", provider=self.name)
        self.failures = 0
        self.opened_at = 0.0
        self._half_open = False

    def record_failure(self) -> None:
        self.failures += 1
        if self._half_open or self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            self._half_open = False
            log.warning(
                "breaker_open",
                provider=self.name,
                failures=self.failures,
                event=EventType.DATA_PROVIDER_DEGRADED.value,
            )


class HttpAdapter:
    """Shared async HTTP client with breaker + backoff."""

    provider = "http"
    retries = 2
    backoff_base = 0.15
    timeout = 8.0

    def __init__(self, base_url: str = "", headers: dict[str, str] | None = None, **kw):
        self._client: httpx.AsyncClient | None = None
        self._base_url = base_url
        self._headers = headers or {}
        self._timeout = kw.pop("timeout", self.timeout)
        self.breaker = CircuitBreaker(
            name=self.provider,
            threshold=kw.pop("breaker_threshold", 5),
            cooldown=kw.pop("breaker_cooldown", 30.0),
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=httpx.Timeout(self._timeout, connect=3.0),
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
                follow_redirects=True,
                http2=True,
            )
        return self._client

    @property
    def degraded(self) -> bool:
        return self.breaker.is_open

    async def request(
        self,
        method: str,
        url: str,
        *,
        allow_degraded: bool = False,
        **kw: Any,
    ) -> Any:
        if self.breaker.is_open and not allow_degraded:
            raise ProviderDegraded(self.provider, "circuit breaker open")

        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await self.client.request(method, url, **kw)
                if resp.status_code == 429:
                    raise ProviderError(self.provider, "rate limited", 429)
                if resp.status_code >= 500:
                    raise ProviderError(self.provider, f"upstream {resp.status_code}", resp.status_code)
                if resp.status_code >= 400:
                    # 4xx is our fault: do not retry, do not trip the breaker
                    self.breaker.record_success()
                    raise ProviderError(self.provider, resp.text[:200], resp.status_code)
                self.breaker.record_success()
                return resp.json()
            except ProviderError as exc:
                last = exc
                if exc.status and 400 <= exc.status < 500 and exc.status != 429:
                    raise
            except (TimeoutError, httpx.HTTPError) as exc:
                last = ProviderError(self.provider, repr(exc))
            if attempt < self.retries:
                await asyncio.sleep(self.backoff_base * (2**attempt))

        self.breaker.record_failure()
        raise last or ProviderError(self.provider, "unknown failure")

    async def get(self, url: str, **kw) -> Any:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> Any:
        return await self.request("POST", url, **kw)

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
