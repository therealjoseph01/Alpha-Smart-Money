"""PRD 40 - the AI layer.

PRD 5.6 is the governing rule: *deterministic controls dominate AI*. So this module is
built so that it structurally cannot influence a trade, rather than merely being asked
not to:

  * it is never imported by the decision engine, the gates, the risk ledger, or the
    executors - `test_ai_layer_is_not_reachable_from_the_hot_path` enforces that;
  * it receives only the aggregates assembled in `redact()` - never keys, never a
    wallet it could act on, never a raw config it could echo back as an instruction;
  * its output is typed as commentary and stored as an alert or report. There is no
    code path from a model response to an order.

What it is genuinely good at: summarising a day of decisions, naming a pattern across
hundreds of rejections, and drafting the weekly review a human then acts on (PRD 68).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from asm.config import settings
from asm.logging import get_logger

log = get_logger(__name__)

MODEL = "claude-sonnet-5"
MAX_TOKENS = 2000

# Keys that must never reach a model, however they got into a payload.
FORBIDDEN_KEYS = frozenset({
    "private_key", "secret", "keypair", "seed_phrase", "mnemonic", "api_key",
    "authorization", "token", "password", "signer", "solana_keypair",
})

SYSTEM_PROMPT = """You are a trading-systems analyst reviewing an automated Solana \
copy-trading system.

You are given aggregate statistics only. You cannot place, size, approve or block trades, \
and nothing you write will be executed. Your output is read by a human operator.

Your job:
- describe what the numbers show, plainly
- name the single most likely explanation for the biggest anomaly
- suggest at most three specific, testable changes, each as a hypothesis to run as a \
controlled experiment

Rules:
- never claim to predict prices or market direction
- never recommend removing or loosening a risk limit, stop loss, or circuit breaker
- if the sample is too small to support a conclusion, say so and stop
- prefer "the data does not say" over a confident guess
"""


class AIUnavailable(Exception):
    pass


@dataclass
class Commentary:
    """Advisory text. Explicitly not a decision, and typed so it cannot be mistaken."""

    text: str
    model: str
    generated_at: datetime
    inputs_digest: str
    advisory_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text, "model": self.model,
            "generated_at": self.generated_at.isoformat(),
            "inputs_digest": self.inputs_digest, "advisory_only": True,
        }


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip anything sensitive, and truncate wallet addresses to a non-actionable prefix.

    A model does not need a full address to reason about a trader's statistics, and a
    truncated one cannot be copied into a transaction.
    """
    def clean(value: Any, key: str = "") -> Any:
        if any(f in key.lower() for f in FORBIDDEN_KEYS):
            return "[redacted]"
        if isinstance(value, dict):
            return {k: clean(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v, key) for v in value[:100]]
        if isinstance(value, str):
            looks_like_address = (
                32 <= len(value) <= 44 and value.isalnum()
                and not value.isdigit()
            )
            if looks_like_address or "wallet" in key.lower() or "mint" in key.lower():
                return f"{value[:6]}…"
            return value[:500]
        return value

    return {k: clean(v, k) for k, v in payload.items()}


def _digest(payload: dict[str, Any]) -> str:
    import hashlib

    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class Analyst:
    """Thin Anthropic client. Absent an API key, the whole layer is simply off."""

    def __init__(self, api_key: str | None = None, model: str = MODEL):
        self.api_key = api_key if api_key is not None else settings.anthropic_api_key
        self.model = model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def analyse(self, question: str, data: dict[str, Any]) -> Commentary:
        if not self.enabled:
            raise AIUnavailable("ANTHROPIC_API_KEY not configured; AI layer is off")

        safe = redact(data)
        prompt = (
            f"{question}\n\nAggregate statistics:\n"
            f"```json\n{json.dumps(safe, indent=2, default=str)}\n```"
        )

        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": MAX_TOKENS,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code >= 400:
            raise AIUnavailable(f"anthropic api {resp.status_code}: {resp.text[:200]}")

        body = resp.json()
        text = "".join(
            block.get("text", "") for block in body.get("content", [])
            if block.get("type") == "text"
        ).strip()

        return Commentary(
            text=text or "(empty response)", model=self.model,
            generated_at=datetime.now(UTC), inputs_digest=_digest(safe),
        )


async def explain_rejections(stats: dict[str, Any]) -> Commentary:
    return await Analyst().analyse(
        "These are the rejection reason codes from the copyability gates over the last "
        "period. Which gate is binding hardest, is that plausibly correct, and what "
        "experiment would test whether it is set too tight?",
        stats,
    )


async def weekly_review(report: dict[str, Any]) -> Commentary:
    """PRD 68 - drafts the weekly strategy review for a human to act on."""
    return await Analyst().analyse(
        "This is a week of operating statistics for the copy-trading system. Summarise "
        "what changed, identify the largest risk that is not already controlled, and "
        "propose at most three experiments.",
        report,
    )
