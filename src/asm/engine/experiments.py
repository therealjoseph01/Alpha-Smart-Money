"""PRD 46/57 - experiment framework and strategy versioning.

The point is to change thresholds on evidence rather than on a hunch. A config change
without a controlled comparison is a guess that gets remembered as a decision.

Assignment is deterministic: hash(experiment, unit) -> arm. The same trader always lands
in the same arm, so a wallet cannot drift between arms mid-experiment and contaminate
both. No randomness means no need to store assignments.

Hard rule (PRD 4): an experiment may vary copyability thresholds and sizing. It may NOT
vary portfolio risk limits, stop losses, or the kill switch. Those are not opinions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from asm.config import CopyabilityConfig, settings
from asm.logging import get_logger

log = get_logger(__name__)

# Fields an experiment is allowed to touch. Anything else is refused.
MUTABLE_FIELDS = frozenset({
    "price_move_normal_pct", "price_move_acceptable_pct", "price_move_reduced_pct",
    "price_move_caution_pct", "price_move_reduced_mult", "price_move_caution_mult",
    "max_total_copy_latency_ms", "max_detection_age_ms",
    "min_liquidity_usd", "max_position_pct_of_liquidity",
    "max_expected_slippage_pct", "slippage_safety_margin_pct", "execution_slippage_bps",
    "min_market_cap_usd", "max_market_cap_usd",
    "min_token_age_seconds", "new_token_age_seconds", "new_token_size_mult",
    "max_token_risk_score", "min_trader_score", "min_copyability_score",
    "min_trader_confidence",
})

# Explicitly forbidden, with the reason, so a refusal explains itself.
PROTECTED_FIELDS = {
    "max_daily_loss_pct": "daily loss breaker is a hard limit (PRD 4)",
    "max_drawdown_pct": "drawdown breaker is a hard limit (PRD 4)",
    "stop_loss_pct": "stop loss is a hard limit (PRD 4)",
    "max_position_pct": "position ceiling is a hard limit (PRD 4)",
    "max_portfolio_deployed_pct": "deployment ceiling is a hard limit (PRD 4)",
}


class ExperimentError(Exception):
    pass


@dataclass
class Arm:
    name: str
    overrides: dict[str, Any] = field(default_factory=dict)
    weight: int = 1


@dataclass
class Experiment:
    name: str
    arms: list[Arm]
    enabled: bool = True
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    note: str = ""

    def __post_init__(self) -> None:
        if len(self.arms) < 2:
            raise ExperimentError("an experiment needs at least a control and one variant")
        if sum(a.weight for a in self.arms) <= 0:
            raise ExperimentError("arm weights must sum above zero")
        for arm in self.arms:
            for key in arm.overrides:
                if key in PROTECTED_FIELDS:
                    raise ExperimentError(
                        f"arm '{arm.name}' may not override '{key}': {PROTECTED_FIELDS[key]}")
                if key not in MUTABLE_FIELDS:
                    raise ExperimentError(
                        f"arm '{arm.name}' overrides unknown or protected field '{key}'")

    def assign(self, unit: str) -> Arm:
        """Deterministic, weight-respecting assignment."""
        digest = hashlib.sha256(f"{self.name}:{unit}".encode()).digest()
        total = sum(a.weight for a in self.arms)
        point = int.from_bytes(digest[:8], "big") % total
        cursor = 0
        for arm in self.arms:
            cursor += arm.weight
            if point < cursor:
                return arm
        return self.arms[-1]

    @property
    def control(self) -> Arm:
        return self.arms[0]


class ExperimentRegistry:
    def __init__(self, experiments: list[Experiment] | None = None):
        self._by_name: dict[str, Experiment] = {e.name: e for e in (experiments or [])}

    def register(self, experiment: Experiment) -> None:
        self._by_name[experiment.name] = experiment

    def active(self) -> list[Experiment]:
        return [e for e in self._by_name.values() if e.enabled]

    def config_for(self, unit: str) -> tuple[CopyabilityConfig, dict[str, str]]:
        """Resolve the effective copyability config for a trader, plus its assignments.

        The assignment dict is stored on every decision, so a result can always be
        attributed to the exact configuration that produced it (PRD 5.5).
        """
        overrides: dict[str, Any] = {}
        assignments: dict[str, str] = {}
        for experiment in self.active():
            arm = experiment.assign(unit)
            assignments[experiment.name] = arm.name
            overrides.update(arm.overrides)

        if not overrides:
            return settings.copyability, assignments

        base = settings.copyability.model_dump()
        base.update(overrides)
        return CopyabilityConfig(**base), assignments


def strategy_checksum(config: CopyabilityConfig, risk_preset: str,
                      version: str) -> str:
    """PRD 57 - a fingerprint of the exact rules a decision ran under.

    Stored alongside every decision so 'which config produced this result?' is answerable
    months later, even after the config file has changed a dozen times.
    """
    payload = {
        "version": version,
        "risk_preset": risk_preset,
        "copyability": {k: str(v) for k, v in sorted(config.model_dump().items())},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class ArmResult:
    arm: str
    trades: int = 0
    wins: int = 0
    pnl_usd: Decimal = Decimal(0)

    @property
    def win_rate_pct(self) -> Decimal:
        return (Decimal(self.wins) / Decimal(self.trades) * Decimal(100)
                if self.trades else Decimal(0))

    @property
    def expectancy_usd(self) -> Decimal:
        return (self.pnl_usd / Decimal(self.trades)) if self.trades else Decimal(0)


def compare_arms(results: list[ArmResult], min_trades: int = 30) -> dict[str, Any]:
    """A deliberately conservative read-out.

    No p-values: with this sample size and this much skew they would be theatre. What it
    does say is whether the sample is large enough to look at, and by how much the best
    arm leads. Anything under `min_trades` per arm returns 'inconclusive', full stop.
    """
    if not results:
        return {"verdict": "no_data", "arms": []}

    underpowered = [r.arm for r in results if r.trades < min_trades]
    ranked = sorted(results, key=lambda r: r.expectancy_usd, reverse=True)
    best, rest = ranked[0], ranked[1:]

    payload = {
        "arms": [
            {"arm": r.arm, "trades": r.trades, "win_rate_pct": str(r.win_rate_pct.quantize(Decimal("0.01"))),
             "pnl_usd": str(r.pnl_usd.quantize(Decimal("0.01"))),
             "expectancy_usd": str(r.expectancy_usd.quantize(Decimal("0.01")))}
            for r in ranked
        ],
        "leader": best.arm,
    }

    if underpowered:
        payload["verdict"] = "inconclusive"
        payload["reason"] = (f"arms below {min_trades} trades: {', '.join(underpowered)}; "
                             "do not promote a winner yet")
        return payload

    runner_up = rest[0] if rest else None
    if runner_up is None:
        payload["verdict"] = "single_arm"
        return payload

    lead = best.expectancy_usd - runner_up.expectancy_usd
    payload["lead_usd_per_trade"] = str(lead.quantize(Decimal("0.01")))
    denominator = abs(runner_up.expectancy_usd) or Decimal(1)
    payload["verdict"] = "leader" if lead > denominator * Decimal("0.2") else "too_close"
    return payload
