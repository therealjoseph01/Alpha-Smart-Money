from __future__ import annotations

from enum import StrEnum


class Chain(StrEnum):
    SOLANA = "solana"


class Action(StrEnum):
    """PRD 16 - trade classification."""

    BUY = "buy"
    SELL = "sell"
    PARTIAL_SELL = "partial_sell"
    TRANSFER = "transfer"
    LP_INTERACTION = "lp_interaction"
    TOKEN_RECEIPT = "token_receipt"
    UNKNOWN = "unknown"

    @property
    def is_copyable(self) -> bool:
        return self in (Action.BUY, Action.SELL, Action.PARTIAL_SELL)


class Decision(StrEnum):
    COPY = "copy"
    REJECT = "reject"
    DEFER = "defer"


class TraderStatus(StrEnum):
    """PRD 15 - trader lifecycle."""

    DISCOVERED = "discovered"
    ANALYZING = "analyzing"
    CANDIDATE = "candidate"
    PROBATION = "probation"
    ACTIVE = "active"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"
    BLACKLISTED = "blacklisted"

    @property
    def is_followed(self) -> bool:
        return self in (TraderStatus.PROBATION, TraderStatus.ACTIVE)


class PositionStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    REDUCING = "reducing"
    CLOSED = "closed"
    FAILED = "failed"


class PositionEventType(StrEnum):
    OPENED = "opened"
    INCREASED = "increased"
    REDUCED = "reduced"
    CLOSED = "closed"
    MARKED = "marked"


class ExitReason(StrEnum):
    """PRD 28."""

    MIRROR = "mirror_exit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_EXIT = "time_exit"
    TRADER_DEGRADED = "trader_degraded"
    LIQUIDITY_DETERIORATION = "liquidity_deterioration"
    EMERGENCY = "emergency"
    MANUAL = "manual"


class OrderStatus(StrEnum):
    CREATED = "created"
    BUILT = "built"
    SIMULATED = "simulated"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    EXPIRED = "expired"


class SystemState(StrEnum):
    """PRD 55 - emergency controls, plus an explicit off state.

    STOPPED is the default on a fresh install and is distinct from SOFT_PAUSED: it means
    "never started", not "started then paused". Booting a trading system into a state
    where it immediately starts spending money is the wrong default - starting must be a
    deliberate act, and it must survive a restart once taken.
    """

    STOPPED = "stopped"
    RUNNING = "running"
    SOFT_PAUSED = "soft_paused"
    HARD_PAUSED = "hard_paused"
    KILLED = "killed"

    @property
    def allows_entries(self) -> bool:
        return self is SystemState.RUNNING

    @property
    def allows_exits(self) -> bool:
        """Exits keep working in every state except a full kill.

        A stopped or paused system must still be able to close what it already holds -
        otherwise pausing would strand open positions with no stop loss.
        """
        return self is not SystemState.KILLED

    @property
    def is_started(self) -> bool:
        return self is not SystemState.STOPPED


class Reason(StrEnum):
    """PRD 41 - every decision carries reason codes. Positive and negative."""

    # approvals
    HIGH_TRADER_SCORE = "HIGH_TRADER_SCORE"
    HIGH_COPYABILITY = "HIGH_COPYABILITY"
    ACCEPTABLE_PRICE_MOVE = "ACCEPTABLE_PRICE_MOVE"
    SUFFICIENT_LIQUIDITY = "SUFFICIENT_LIQUIDITY"
    ACCEPTABLE_SLIPPAGE = "ACCEPTABLE_SLIPPAGE"
    TOKEN_SAFETY_PASS = "TOKEN_SAFETY_PASS"
    MULTI_TRADER_CONFIRMATION = "MULTI_TRADER_CONFIRMATION"

    # trader gates
    TRADER_NOT_FOLLOWED = "TRADER_NOT_FOLLOWED"
    TRADER_SCORE_TOO_LOW = "TRADER_SCORE_TOO_LOW"
    TRADER_CONFIDENCE_TOO_LOW = "TRADER_CONFIDENCE_TOO_LOW"
    COPYABILITY_TOO_LOW = "COPYABILITY_TOO_LOW"
    TRADER_DEGRADED = "TRADER_DEGRADED"
    TRADER_BUDGET_EXHAUSTED = "TRADER_BUDGET_EXHAUSTED"

    # 17.1 / 17.2
    PRICE_MOVED_TOO_FAR = "PRICE_MOVED_TOO_FAR"
    PRICE_MOVE_REDUCED_SIZE = "PRICE_MOVE_REDUCED_SIZE"
    SIGNAL_TOO_OLD = "SIGNAL_TOO_OLD"
    LATENCY_EXCEEDS_TRADER_TOLERANCE = "LATENCY_EXCEEDS_TRADER_TOLERANCE"

    # 17.3 / 17.4
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    POSITION_TOO_LARGE_FOR_LIQUIDITY = "POSITION_TOO_LARGE_FOR_LIQUIDITY"
    EXPECTED_SLIPPAGE_TOO_HIGH = "EXPECTED_SLIPPAGE_TOO_HIGH"
    ALPHA_BELOW_COST = "ALPHA_BELOW_COST"
    NO_ROUTE = "NO_ROUTE"

    # 17.5 / 17.6
    MARKET_CAP_TOO_LOW = "MARKET_CAP_TOO_LOW"
    MARKET_CAP_TOO_HIGH = "MARKET_CAP_TOO_HIGH"
    TOKEN_TOO_NEW = "TOKEN_TOO_NEW"

    # 17.7 / 22-25
    TOKEN_EXPOSURE_LIMIT = "TOKEN_EXPOSURE_LIMIT"
    TRADER_EXPOSURE_LIMIT = "TRADER_EXPOSURE_LIMIT"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    PORTFOLIO_DEPLOYED_LIMIT = "PORTFOLIO_DEPLOYED_LIMIT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    POSITION_BELOW_MINIMUM = "POSITION_BELOW_MINIMUM"
    TOO_MANY_PENDING = "TOO_MANY_PENDING"

    # 18 / 19
    TOKEN_RISK_TOO_HIGH = "TOKEN_RISK_TOO_HIGH"
    MINT_AUTHORITY_ACTIVE = "MINT_AUTHORITY_ACTIVE"
    FREEZE_AUTHORITY_ACTIVE = "FREEZE_AUTHORITY_ACTIVE"
    HOLDER_CONCENTRATION = "HOLDER_CONCENTRATION"
    LP_NOT_BURNED = "LP_NOT_BURNED"
    INSIDER_PATTERN = "INSIDER_PATTERN"
    DEPLOYER_LINKED = "DEPLOYER_LINKED"

    # system
    SYSTEM_NOT_STARTED = "SYSTEM_NOT_STARTED"
    SYSTEM_PAUSED = "SYSTEM_PAUSED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    NON_COPYABLE_ACTION = "NON_COPYABLE_ACTION"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    CRITICAL_DATA_UNAVAILABLE = "CRITICAL_DATA_UNAVAILABLE"
    NO_MATCHING_POSITION = "NO_MATCHING_POSITION"


class EventType(StrEnum):
    """PRD 50."""

    TRADER_DISCOVERED = "TRADER_DISCOVERED"
    TRADER_SCORE_UPDATED = "TRADER_SCORE_UPDATED"
    TRADER_PROMOTED = "TRADER_PROMOTED"
    TRADER_DEGRADED = "TRADER_DEGRADED"
    SOURCE_TRADE_DETECTED = "SOURCE_TRADE_DETECTED"
    TRADE_CANDIDATE_CREATED = "TRADE_CANDIDATE_CREATED"
    TRADE_APPROVED = "TRADE_APPROVED"
    TRADE_REJECTED = "TRADE_REJECTED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_CONFIRMED = "ORDER_CONFIRMED"
    ORDER_FAILED = "ORDER_FAILED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_REDUCED = "POSITION_REDUCED"
    POSITION_CLOSED = "POSITION_CLOSED"
    RISK_THRESHOLD_REACHED = "RISK_THRESHOLD_REACHED"
    TRADING_HALTED = "TRADING_HALTED"
    HARVEST_THRESHOLD_REACHED = "HARVEST_THRESHOLD_REACHED"
    PROFIT_HARVESTED = "PROFIT_HARVESTED"
    DATA_PROVIDER_DEGRADED = "DATA_PROVIDER_DEGRADED"
