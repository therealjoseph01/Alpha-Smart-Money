"""Runtime-tunable settings (PRD 57).

Most configuration belongs in code: it is versioned, reviewed and defaulted sensibly.
Only the handful an operator genuinely changes while running belongs in a UI — and
those need three things a .env file cannot give:

  * **bounds**, so a typo cannot set the daily loss limit to 100%;
  * **an audit trail**, because a risk limit change is a decision (PRD 58);
  * **propagation**, since the four services are separate processes — a change made in
    the API must reach the decision loop without a restart.

**The database holds the values.** Code supplies the initial defaults only; once a
setting is saved it lives in `strategy_configs` and survives a wiped Redis, a restart,
or a redeploy. Redis is a cache so the other three processes notice a change within
seconds rather than at their next restart.

Resetting deletes the stored record, and the code defaults apply again.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import orjson

from asm.config import settings
from asm.logging import get_logger
from asm.state.client import get_redis

log = get_logger(__name__)

KEY = "asm:config:overrides"
REFRESH_SECONDS = 5


@dataclass(frozen=True)
class Field:
    key: str
    section: str          # "risk" | "copyability" | "root"
    attr: str
    label: str
    kind: str             # "decimal" | "int"
    lo: Decimal
    hi: Decimal
    unit: str = ""
    help: str = ""
    danger: bool = False  # a hard risk limit - warn before widening


# The complete set an operator may change at runtime. Anything not listed here is
# code-only, deliberately: a setting that can be changed is a setting that can be
# changed wrongly.
FIELDS: tuple[Field, ...] = (
    Field("starting_capital_usd", "root", "starting_capital_usd",
          "Trading capital", "decimal", Decimal("10"), Decimal("10000000"), "$",
          "Every percentage limit is measured against this. Set it to what you "
          "actually intend to risk, even in paper mode."),
    Field("target_roster_size", "root", "target_roster_size",
          "Wallets to copy", "int", Decimal(1), Decimal(100), "",
          "How many traders are copied at once. The best N by your own score."),

    Field("base_position_pct", "risk", "base_position_pct",
          "Position size", "decimal", Decimal("0.05"), Decimal("50"), "%",
          "Typical position as a share of equity, before any gate reduces it."),
    Field("max_position_pct", "risk", "max_position_pct",
          "Max position", "decimal", Decimal("0.1"), Decimal("50"), "%",
          "Hard ceiling. No multiplier or confirmation can exceed it.", danger=True),
    Field("min_position_usd", "risk", "min_position_usd",
          "Minimum position", "decimal", Decimal("1"), Decimal("10000"), "$",
          "Below this a trade is skipped — fixed costs would eat the edge."),

    Field("max_daily_loss_pct", "risk", "max_daily_loss_pct",
          "Daily loss limit", "decimal", Decimal("0.5"), Decimal("50"), "%",
          "Stops new entries once realized losses hit this on the day.", danger=True),
    Field("max_drawdown_pct", "risk", "max_drawdown_pct",
          "Drawdown limit", "decimal", Decimal("1"), Decimal("90"), "%",
          "Stops new entries this far below the high-water mark.", danger=True),
    Field("max_portfolio_deployed_pct", "risk", "max_portfolio_deployed_pct",
          "Max deployed", "decimal", Decimal("5"), Decimal("100"), "%",
          "Cap on how much of equity can be in open positions at once."),

    Field("stop_loss_pct", "risk", "stop_loss_pct",
          "Stop loss", "decimal", Decimal("-95"), Decimal("-1"), "%",
          "Exit when a position falls this far. Negative.", danger=True),
    Field("trailing_stop_pct", "risk", "trailing_stop_pct",
          "Trailing stop", "decimal", Decimal("1"), Decimal("90"), "%",
          "Exit after giving back this much from the peak."),
    Field("trailing_arm_pct", "risk", "trailing_arm_pct",
          "Arm trailing at", "decimal", Decimal("1"), Decimal("500"), "%",
          "The trailing stop only activates after this much gain."),

    Field("harvest_threshold_pct", "risk", "harvest_threshold_pct",
          "Harvest trigger", "decimal", Decimal("1"), Decimal("500"), "%",
          "Move profit out once equity rises this far above the last harvest."),
    Field("harvest_take_pct", "risk", "harvest_take_pct",
          "Harvest share", "decimal", Decimal("1"), Decimal("100"), "%",
          "How much of that profit leaves the trading wallet."),

    Field("min_liquidity_usd", "copyability", "min_liquidity_usd",
          "Min liquidity", "decimal", Decimal("500"), Decimal("100000000"), "$",
          "Reject tokens with a thinner pool than this."),
    Field("max_expected_slippage_pct", "copyability", "max_expected_slippage_pct",
          "Max slippage", "decimal", Decimal("0.1"), Decimal("50"), "%",
          "Reject when the route's price impact exceeds this."),
    Field("min_token_age_seconds", "copyability", "min_token_age_seconds",
          "Min token age", "int", Decimal(0), Decimal(2_592_000), "s",
          "Reject tokens younger than this."),
    Field("max_open_positions", "copyability", "max_open_positions",
          "Max open positions", "int", Decimal(1), Decimal(200), "",
          "Hard cap on concurrent positions."),
    Field("min_trader_score", "copyability", "min_trader_score",
          "Min trader score", "decimal", Decimal(0), Decimal(100), "",
          "A trader below this is never copied."),
    Field("price_move_caution_pct", "copyability", "price_move_caution_pct",
          "Max price chase", "decimal", Decimal("0.1"), Decimal("100"), "%",
          "Reject once the price has already moved this far since the trader bought."),
)

BY_KEY = {f.key: f for f in FIELDS}
_last_refresh = 0.0


class ConfigError(ValueError):
    pass


def _target(field: Field) -> Any:
    return {"risk": settings.risk, "copyability": settings.copyability,
            "root": settings}[field.section]


def coerce(field: Field, raw: Any) -> Decimal | int:
    try:
        value = Decimal(str(raw).strip().rstrip("%$").strip())
    except (InvalidOperation, AttributeError, TypeError) as exc:
        raise ConfigError(f"{field.label}: '{raw}' is not a number") from exc
    if not (field.lo <= value <= field.hi):
        raise ConfigError(
            f"{field.label} must be between {field.lo} and {field.hi} (got {value})")
    return int(value) if field.kind == "int" else value


def current() -> dict[str, Any]:
    """Every tunable field with its live value, bounds and help text."""
    out = {}
    for f in FIELDS:
        value = getattr(_target(f), f.attr)
        out[f.key] = {
            "label": f.label, "value": str(value), "unit": f.unit,
            "min": str(f.lo), "max": str(f.hi), "kind": f.kind,
            "help": f.help, "danger": f.danger,
            "section": {"root": "Capital", "risk": "Risk",
                        "copyability": "Gates"}[f.section],
        }
    return out


async def apply(changes: dict[str, Any], *, actor: str = "api") -> dict[str, Any]:
    """Validate, save to the database, apply here, and tell the other processes."""
    validated: dict[str, Any] = {}
    for key, raw in changes.items():
        field = BY_KEY.get(key)
        if field is None:
            raise ConfigError(f"unknown setting: {key}")
        validated[key] = coerce(field, raw)

    before = {k: str(getattr(_target(BY_KEY[k]), BY_KEY[k].attr)) for k in validated}

    stored = await load_stored()
    stored.update({k: str(v) for k, v in validated.items()})

    await _save_stored(stored, actor=actor, before=before,
                       after={k: str(v) for k, v in validated.items()})

    for key, value in validated.items():
        field = BY_KEY[key]
        setattr(_target(field), field.attr, value)

    log.warning("config_changed", actor=actor,
                changes={k: str(v) for k, v in validated.items()})
    return {k: str(v) for k, v in validated.items()}


async def _save_stored(values: dict[str, str], *, actor: str,
                       before: dict | None = None, after: dict | None = None) -> None:
    """Database is the record; Redis is a cache for the other processes."""
    from sqlalchemy import select

    from asm.db import models as M
    from asm.db import repo
    from asm.db.session import session_scope

    async with session_scope() as s:
        row = (await s.execute(
            select(M.StrategyConfig).where(M.StrategyConfig.name == "runtime",
                                           M.StrategyConfig.version == "current")
        )).scalar_one_or_none()
        if row is None:
            s.add(M.StrategyConfig(name="runtime", version="current", active=True,
                                   config=values))
        else:
            row.config = values
            row.active = True
        await repo.log_audit(s, actor=actor, action="config_changed",
                             target=",".join(after or values),
                             before=before or {}, after=after or values)

    await get_redis().set(KEY, orjson.dumps(values))


async def load_stored() -> dict[str, str]:
    """Read the saved values. Database first - Redis may be empty or wiped."""
    from sqlalchemy import select

    from asm.db import models as M
    from asm.db.session import session_scope

    try:
        async with session_scope() as s:
            row = (await s.execute(
                select(M.StrategyConfig).where(M.StrategyConfig.name == "runtime",
                                               M.StrategyConfig.version == "current")
            )).scalar_one_or_none()
            if row and row.config:
                return dict(row.config)
    except Exception as exc:
        log.warning("config_db_read_failed", error=repr(exc))
    return await _stored()


async def reset(*, actor: str = "api") -> dict[str, str]:
    """Drop all overrides and fall back to the code defaults."""
    from sqlalchemy import delete

    from asm.config import CopyabilityConfig, RiskConfig
    from asm.db import models as M
    from asm.db import repo
    from asm.db.session import session_scope

    await get_redis().delete(KEY)
    async with session_scope() as s:
        await s.execute(delete(M.StrategyConfig).where(
            M.StrategyConfig.name == "runtime"))

    defaults_risk, defaults_copy = RiskConfig(), CopyabilityConfig()
    for f in FIELDS:
        src = {"risk": defaults_risk, "copyability": defaults_copy,
               "root": None}[f.section]
        if src is not None:
            setattr(_target(f), f.attr, getattr(src, f.attr))
    async with session_scope() as s:
        await repo.log_audit(s, actor=actor, action="config_reset")
    log.warning("config_reset", actor=actor)
    return {"reset": "true"}


async def _stored() -> dict[str, str]:
    raw = await get_redis().get(KEY)
    if not raw:
        return {}
    try:
        return orjson.loads(raw)
    except Exception:
        return {}


async def refresh(force: bool = False) -> None:
    """Pull saved values into this process. Cheap, and safe to call in a loop.

    The services are separate processes, so a change saved by the API only reaches the
    decision loop because each process re-reads this. `force=True` is used at startup,
    where the database is read directly rather than trusting the cache.
    """
    global _last_refresh
    now = time.monotonic()
    if not force and now - _last_refresh < REFRESH_SECONDS:
        return
    _last_refresh = now
    try:
        values = await load_stored() if force else await _stored()
        for key, raw in values.items():
            field = BY_KEY.get(key)
            if field is None:
                continue
            setattr(_target(field), field.attr, coerce(field, raw))
    except Exception as exc:
        # Never let a bad override take a trading service down; keep what we have.
        log.warning("config_refresh_failed", error=repr(exc))
