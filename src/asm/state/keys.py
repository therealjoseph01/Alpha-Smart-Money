"""Every Redis key in one place. Namespaced by mode so paper and live never mix."""
from __future__ import annotations

from asm.config import settings


def _m(mode: str | None = None) -> str:
    return mode or settings.mode.value


# risk ledger
def risk_state(mode: str | None = None) -> str:
    return f"asm:{_m(mode)}:risk:state"


def exposure_token(mode: str | None = None) -> str:
    return f"asm:{_m(mode)}:risk:exposure:token"


def exposure_trader(mode: str | None = None) -> str:
    return f"asm:{_m(mode)}:risk:exposure:trader"


def reservation(res_id: str, mode: str | None = None) -> str:
    return f"asm:{_m(mode)}:risk:res:{res_id}"


# system control (PRD 55) - global, not per-mode
SYSTEM_STATE = "asm:system:state"
KILL_SWITCH = "asm:system:kill"


# idempotency (PRD 51)
def dedupe(key: str) -> str:
    return f"asm:dedupe:{key}"


def idem(key: str) -> str:
    return f"asm:idem:{key}"


# caches
def market(mint: str) -> str:
    return f"asm:cache:market:{mint}"


def token_risk(mint: str) -> str:
    return f"asm:cache:tokenrisk:{mint}"


def trader(wallet: str) -> str:
    return f"asm:cache:trader:{wallet}"


FOLLOWED_SET = "asm:followed:wallets"


# learned latency stats (PRD 17.2)
def latency_samples(wallet: str) -> str:
    return f"asm:latency:{wallet}"


# multi-trader confirmation (PRD 20)
def recent_buys(mint: str) -> str:
    return f"asm:confirm:{mint}"


# event bus (PRD 50)
STREAM_EVENTS = "asm:events"
STREAM_SIGNALS = "asm:signals"


# circuit breakers (PRD 53)
def breaker(provider: str) -> str:
    return f"asm:breaker:{provider}"
