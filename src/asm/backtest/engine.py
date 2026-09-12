"""PRD 44 - the backtest engine.

This is the gate on live trading. It answers the only question that matters before
risking money: does the alpha survive our latency, our size, and our costs?

It runs the REAL decision engine, the REAL gates, and the REAL atomic risk ledger
(namespaced to the run). Only the data provider and the executor are swapped. If the
backtester had its own rules, it would be validating a copy of the system.

The output deliberately reports three different numbers side by side (PRD 5.1):

    source_pnl     what the wallet we copied made
    copyable_pnl   what a follower could have made, before our risk limits
    realized_pnl   what THIS system, with THESE limits, actually made

The gap between the first and the third is the product.
"""
from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from asm.backtest.executor import BacktestExecutor
from asm.backtest.provider import HistoricalDataProvider, TokenHistory
from asm.config import RiskConfig, settings
from asm.domain.enums import Action, ExitReason, PositionStatus
from asm.domain.models import (
    ApprovedOrder,
    LatencyBreakdown,
    Position,
    SourceTrade,
    TradeCandidate,
    TraderProfile,
)
from asm.domain.money import SOL_MINT, ZERO, raw_to_ui
from asm.engine.decision import DecisionEngine
from asm.engine.exits import evaluate_exits, realized_pnl, update_marks
from asm.engine.sizing import SizingMode
from asm.logging import get_logger
from asm.state.client import get_redis
from asm.state.ledger import RiskLedger

log = get_logger(__name__)


@dataclass
class BacktestConfig:
    starting_capital_usd: Decimal = Decimal("10000")
    assumed_detection_latency_ms: int = 2500
    assumed_analysis_latency_ms: int = 400
    sizing_mode: SizingMode = SizingMode.CONFIDENCE_WEIGHTED
    seed: int = 1337
    sol_price: Decimal = Decimal("150")
    fail_rate: Decimal = Decimal("0.04")
    exit_tick_seconds: int = 60
    # Fixed notional for the copyable counterfactual, so it is comparable across runs
    # and independent of our own position sizing.
    copyable_notional_usd: Decimal = Decimal("100")
    name: str = "backtest"


@dataclass
class ClosedTrade:
    mint: str
    trader: str
    opened_at: datetime
    closed_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    size_usd: Decimal
    realized_pnl_usd: Decimal
    fees_usd: Decimal
    exit_reason: str
    source_return_pct: Decimal | None = None

    @property
    def return_pct(self) -> Decimal:
        return (self.realized_pnl_usd / self.size_usd * Decimal(100)) if self.size_usd else ZERO

    @property
    def hold_seconds(self) -> int:
        return int((self.closed_at - self.opened_at).total_seconds())


@dataclass
class BacktestResult:
    id: str
    config: dict[str, Any]
    started_at: datetime
    finished_at: datetime

    signals: int = 0
    approved: int = 0
    rejected: int = 0
    fills: int = 0
    failed_fills: int = 0

    source_pnl_usd: Decimal = ZERO
    copyable_pnl_usd: Decimal = ZERO
    realized_pnl_usd: Decimal = ZERO
    fees_usd: Decimal = ZERO

    starting_equity: Decimal = ZERO
    ending_equity: Decimal = ZERO
    peak_equity: Decimal = ZERO
    max_drawdown_pct: Decimal = ZERO

    trades: list[ClosedTrade] = field(default_factory=list)
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    exit_reasons: dict[str, int] = field(default_factory=dict)
    equity_curve: list[tuple[str, str]] = field(default_factory=list)
    provider_misses: int = 0
    source_round_trips: int = 0
    # Per-round-trip percentage returns. Percentages, not dollars, because the source
    # trades its size and we trade ours - comparing the dollar totals compares position
    # sizing, not edge.
    source_trip_returns_pct: list[Decimal] = field(default_factory=list)
    copyable_trip_returns_pct: list[Decimal] = field(default_factory=list)

    # -------------------------------------------------------------- metrics
    @property
    def total_return_pct(self) -> Decimal:
        if self.starting_equity <= 0:
            return ZERO
        return (self.ending_equity - self.starting_equity) / self.starting_equity * Decimal(100)

    @property
    def win_rate_pct(self) -> Decimal:
        if not self.trades:
            return ZERO
        wins = len([t for t in self.trades if t.realized_pnl_usd > 0])
        return Decimal(wins) / Decimal(len(self.trades)) * Decimal(100)

    @property
    def approval_rate_pct(self) -> Decimal:
        return (Decimal(self.approved) / Decimal(self.signals) * Decimal(100)
                if self.signals else ZERO)

    @property
    def profit_factor(self) -> Decimal:
        gains = sum((t.realized_pnl_usd for t in self.trades if t.realized_pnl_usd > 0), ZERO)
        losses = -sum((t.realized_pnl_usd for t in self.trades if t.realized_pnl_usd < 0), ZERO)
        return (gains / losses) if losses > 0 else (gains if gains else ZERO)

    @property
    def expectancy_usd(self) -> Decimal:
        return (self.realized_pnl_usd / Decimal(len(self.trades))) if self.trades else ZERO

    @staticmethod
    def _median(values: list[Decimal]) -> Decimal | None:
        if not values:
            return None
        return Decimal(str(statistics.median(float(v) for v in values)))

    @property
    def source_return_pct(self) -> Decimal | None:
        """Median % return per round trip, as the source actually filled."""
        return self._median(self.source_trip_returns_pct)

    @property
    def copyable_return_pct(self) -> Decimal | None:
        """Median % return on the same trips, entered and exited `latency` later,
        after impact and fees, with no risk limits applied."""
        return self._median(self.copyable_trip_returns_pct)

    @property
    def edge_retention_pct(self) -> Decimal | None:
        """PRD 5.1 - THE number. What share of the source's edge survives being us.

        Scale-free by construction: a ratio of two median percentage returns, so it is
        unaffected by the source trading $200 while we trade $38.

        None when the source closed no profitable round trips in the window - an
        unmeasurable ratio must never be reported as a bad one.
        """
        src, copy = self.source_return_pct, self.copyable_return_pct
        if src is None or copy is None or src <= 0:
            return None
        return copy / src * Decimal(100)

    @property
    def sortino(self) -> Decimal:
        rets = [float(t.return_pct) for t in self.trades]
        if len(rets) < 3:
            return ZERO
        downside = [r for r in rets if r < 0]
        if not downside:
            return Decimal("99")
        dev = statistics.pstdev(downside) or 1.0
        return Decimal(str(statistics.mean(rets) / dev)).quantize(Decimal("0.001"))

    @property
    def median_hold_seconds(self) -> int:
        return int(statistics.median([t.hold_seconds for t in self.trades])) if self.trades else 0

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "signals": self.signals,
            "approved": self.approved,
            "rejected": self.rejected,
            "approval_rate_pct": str(self.approval_rate_pct.quantize(Decimal("0.01"))),
            "fills": self.fills,
            "failed_fills": self.failed_fills,
            "closed_trades": len(self.trades),
            "starting_equity_usd": str(self.starting_equity),
            "ending_equity_usd": str(self.ending_equity.quantize(Decimal("0.01"))),
            "total_return_pct": str(self.total_return_pct.quantize(Decimal("0.01"))),
            "source_pnl_usd": str(self.source_pnl_usd.quantize(Decimal("0.01"))),
            "copyable_pnl_usd": str(self.copyable_pnl_usd.quantize(Decimal("0.01"))),
            "realized_pnl_usd": str(self.realized_pnl_usd.quantize(Decimal("0.01"))),
            "source_round_trips": self.source_round_trips,
            "source_return_pct_median": (
                str(self.source_return_pct.quantize(Decimal("0.01")))
                if self.source_return_pct is not None else None),
            "copyable_return_pct_median": (
                str(self.copyable_return_pct.quantize(Decimal("0.01")))
                if self.copyable_return_pct is not None else None),
            "edge_retention_pct": (
                str(self.edge_retention_pct.quantize(Decimal("0.01")))
                if self.edge_retention_pct is not None else None),
            "fees_usd": str(self.fees_usd.quantize(Decimal("0.01"))),
            "win_rate_pct": str(self.win_rate_pct.quantize(Decimal("0.01"))),
            "profit_factor": str(self.profit_factor.quantize(Decimal("0.001"))),
            "expectancy_usd": str(self.expectancy_usd.quantize(Decimal("0.01"))),
            "sortino": str(self.sortino),
            "max_drawdown_pct": str(self.max_drawdown_pct.quantize(Decimal("0.01"))),
            "median_hold_seconds": self.median_hold_seconds,
            "rejection_reasons": dict(sorted(self.rejection_reasons.items(),
                                             key=lambda kv: -kv[1])),
            "exit_reasons": self.exit_reasons,
            "provider_misses": self.provider_misses,
            "verdict": self.verdict(),
        }

    def verdict(self) -> str:
        """A blunt read, so nobody talks themselves into a bad number."""
        if len(self.trades) < 20:
            return "INSUFFICIENT_SAMPLE: fewer than 20 closed trades; do not act on this"
        if self.realized_pnl_usd <= 0:
            return "NEGATIVE: this configuration lost money after costs"
        if self.max_drawdown_pct > 35:
            return "RISKY: profitable but drawdown exceeds 35%"
        if self.profit_factor < Decimal("1.2"):
            return "THIN: profit factor under 1.2 is inside the noise"

        retention = self.edge_retention_pct
        if retention is None:
            return ("PROFITABLE_UNVERIFIED: positive after costs, but the source closed "
                    "no profitable round trips here, so copyability is not measurable")
        if retention < 20:
            return (f"MARGINAL: only {retention.quantize(Decimal('0.1'))}% of source edge "
                    "survived; latency or costs dominate")
        return (f"VIABLE: positive after costs, {retention.quantize(Decimal('0.1'))}% of "
                "source edge retained, acceptable drawdown")


class BacktestEngine:
    def __init__(self, *, histories: dict[str, TokenHistory],
                 traders: dict[str, TraderProfile],
                 config: BacktestConfig | None = None,
                 risk: RiskConfig | None = None):
        self.histories = histories
        self.traders = traders
        self.cfg = config or BacktestConfig()
        self.risk = risk or settings.risk
        self.id = uuid.uuid4().hex[:12]

        self.provider = HistoricalDataProvider(
            histories, sol_price=self.cfg.sol_price
        )
        self.executor = BacktestExecutor(
            seed=self.cfg.seed, fail_rate=self.cfg.fail_rate,
            sol_price=self.cfg.sol_price,
        )
        # Real ledger, isolated namespace: the atomic risk rules are under test too.
        # Its own system-state key, so a backtest runs whether or not live trading is on.
        self.ledger = RiskLedger(get_redis(), mode=f"bt:{self.id}",
                                 system_state_key=f"asm:bt:{self.id}:state")
        self.engine = DecisionEngine(self.ledger, self.cfg.sizing_mode, data=self.provider)

        self.positions: dict[uuid.UUID, Position] = {}
        self.result = BacktestResult(
            id=self.id, config=self._config_dict(),
            started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
            starting_equity=self.cfg.starting_capital_usd,
            peak_equity=self.cfg.starting_capital_usd,
        )
        self._source_positions: dict[tuple[str, str], list[dict]] = {}
        self._clock: datetime | None = None

    def _config_dict(self) -> dict[str, Any]:
        return {
            "starting_capital_usd": str(self.cfg.starting_capital_usd),
            "assumed_detection_latency_ms": self.cfg.assumed_detection_latency_ms,
            "assumed_analysis_latency_ms": self.cfg.assumed_analysis_latency_ms,
            "sizing_mode": self.cfg.sizing_mode.value,
            "seed": self.cfg.seed,
            "stop_loss_pct": str(self.risk.stop_loss_pct),
            "trailing_stop_pct": str(self.risk.trailing_stop_pct),
            "max_position_pct": str(self.risk.max_position_pct),
        }

    # ------------------------------------------------------------------ run
    async def run(self, trades: list[SourceTrade]) -> BacktestResult:
        trades = sorted(trades, key=lambda t: t.source_confirmed_at)
        if not trades:
            self.result.finished_at = datetime.now(UTC)
            return self.result

        await self.ledger.bootstrap(self.cfg.starting_capital_usd, force=True)
        await get_redis().set(self.ledger.system_state_key, "running")
        timeline = self._build_timeline(trades)
        log.info("backtest_started", id=self.id, trades=len(trades),
                 timeline_steps=len(timeline))

        try:
            for when, kind, payload in timeline:
                # The clock only ever moves forward. Detection latency can push the
                # simulated now past the next timeline entry, and rewinding would let a
                # position close before it opened.
                self._advance(when)
                assert self._clock is not None
                if kind == "trade":
                    await self._on_source_trade(payload, self._clock)
                await self._tick_exits(self._clock)
                await self._mark(self._clock)

            # Close whatever is still open at the end, at the last known price.
            self._advance(timeline[-1][0])
            assert self._clock is not None
            await self._liquidate_all(self._clock)
            await self._mark(self._clock)
        finally:
            snap = await self.ledger.snapshot()
            self.result.ending_equity = snap.total_equity_usd
            self.result.provider_misses = self.provider.misses
            self.result.fills = self.executor.fills
            self.result.failed_fills = self.executor.failures
            self.result.finished_at = datetime.now(UTC)
            await self._cleanup()

        log.info("backtest_complete", **{
            k: v for k, v in self.result.summary().items()
            if not isinstance(v, dict)
        })
        return self.result

    def _advance(self, when: datetime) -> datetime:
        if self._clock is None or when > self._clock:
            self._clock = when
        self.provider.set_clock(self._clock)
        return self._clock

    def _build_timeline(self, trades: list[SourceTrade]):
        """Source trades interleaved with price observations, in time order.

        Exits are evaluated at every price point, not only when the source acts -
        otherwise stops and trailing stops could never fire between signals.
        """
        events: list[tuple[datetime, str, Any]] = [
            (t.source_confirmed_at, "trade", t) for t in trades
        ]
        start = trades[0].source_confirmed_at
        end = trades[-1].source_confirmed_at + timedelta(seconds=self.risk.max_hold_seconds)
        for history in self.histories.values():
            for point in history.points:
                if start <= point.at <= end:
                    events.append((point.at, "price", history.mint))
        events.sort(key=lambda e: (e[0], 0 if e[1] == "price" else 1))
        return events

    # -------------------------------------------------------------- signals
    async def _on_source_trade(self, trade: SourceTrade, when: datetime) -> None:
        self._track_source_pnl(trade)

        trader = self.traders.get(trade.wallet)
        if trader is None or trade.action is not Action.BUY:
            if trade.action in (Action.SELL, Action.PARTIAL_SELL):
                await self._mirror_exit(trade, when)
            return

        self.result.signals += 1

        # Detection is not instantaneous: shift the clock forward before deciding.
        detected = trade.source_confirmed_at + timedelta(
            milliseconds=self.cfg.assumed_detection_latency_ms
        )
        self._advance(detected)
        replay = trade.model_copy(update={"detected_at": detected})

        decision, order = await self.engine.evaluate_entry(replay, trader)

        if not decision.approved or order is None:
            self.result.rejected += 1
            for reason in decision.reasons:
                key = reason.value
                self.result.rejection_reasons[key] = self.result.rejection_reasons.get(key, 0) + 1
            return

        self.result.approved += 1
        order.latency = LatencyBreakdown(
            source_confirmed_at=trade.source_confirmed_at,
            detected_at=detected,
            decision_completed_at=detected + timedelta(
                milliseconds=self.cfg.assumed_analysis_latency_ms),
        )
        assert order.reservation_id is not None
        ex = await self.executor.execute(order)

        if not ex.succeeded or ex.out_amount_raw <= 0:
            await self.ledger.release(order.reservation_id)
            return

        await self.ledger.commit(order.reservation_id, ex.value_usd)
        fee = self.executor.fee_usd()
        self.result.fees_usd += fee

        position = Position(
            token_mint=trade.token_mint, trader_wallet=trade.wallet,
            status=PositionStatus.OPEN, decimals=order.candidate.market.decimals,
            amount_raw=ex.out_amount_raw, cost_basis_usd=ex.value_usd,
            fees_usd=fee, entry_price=ex.actual_price, current_price=ex.actual_price,
            peak_price=ex.actual_price, opened_at=detected, mode="backtest",
            source_trade_id=trade.id,
        )
        self.positions[position.id] = position

    def _track_source_pnl(self, trade: SourceTrade) -> None:
        """FIFO round trips for the SOURCE wallet, plus the COPYABLE counterfactual.

        Three numbers, three meanings (PRD 5.1):
          source    - what the wallet made, at its own fills
          copyable  - what a follower entering and exiting `latency` later would make
                      on a fixed notional, after impact and fees, with NO risk limits
          realized  - what this system actually made, limits and all

        source -> copyable isolates the cost of being late.
        copyable -> realized isolates the cost of our own risk discipline.
        """
        key = (trade.wallet, trade.token_mint)
        lots = self._source_positions.setdefault(key, [])
        if trade.action is Action.BUY:
            lots.append({
                "price": trade.source_price, "amount": trade.token_amount_raw,
                "decimals": trade.token_decimals, "at": trade.source_confirmed_at,
            })
            return

        if trade.action not in (Action.SELL, Action.PARTIAL_SELL):
            return

        remaining = trade.token_amount_raw
        while remaining > 0 and lots:
            lot = lots[0]
            used = min(remaining, lot["amount"])
            units = raw_to_ui(used, lot["decimals"])
            self.result.source_pnl_usd += (
                units * (trade.source_price - lot["price"]) * self.cfg.sol_price
            )
            self.result.source_round_trips += 1
            if lot["price"] > 0:
                self.result.source_trip_returns_pct.append(
                    (trade.source_price - lot["price"]) / lot["price"] * Decimal(100)
                )
            self._accrue_copyable(trade.token_mint, lot["at"], trade.source_confirmed_at)

            lot["amount"] -= used
            remaining -= used
            if lot["amount"] <= 0:
                lots.pop(0)

    def _accrue_copyable(self, mint: str, opened_at: datetime, closed_at: datetime) -> None:
        """A follower's version of one source round trip, on a fixed notional."""
        history = self.histories.get(mint)
        if history is None:
            return
        lag = timedelta(milliseconds=self.cfg.assumed_detection_latency_ms
                        + self.cfg.assumed_analysis_latency_ms)
        entry = history.at(opened_at + lag)
        exit_ = history.at(closed_at + lag)
        if entry is None or exit_ is None or entry.price_usd <= 0:
            return

        notional = self.cfg.copyable_notional_usd
        impact_in = self.provider.impact.price_impact_pct(notional, entry.liquidity_usd)
        impact_out = self.provider.impact.price_impact_pct(notional, exit_.liquidity_usd)

        fill_in = entry.price_usd * (Decimal(1) + impact_in / Decimal(100))
        fill_out = exit_.price_usd * (Decimal(1) - impact_out / Decimal(100))
        if fill_in <= 0:
            return

        units = notional / fill_in
        fees = self.executor.fee_usd() * 2
        pnl = units * fill_out - notional - fees
        self.result.copyable_pnl_usd += pnl
        self.result.copyable_trip_returns_pct.append(pnl / notional * Decimal(100))

    async def _mirror_exit(self, trade: SourceTrade, when: datetime) -> None:
        for position in list(self.positions.values()):
            if (position.token_mint == trade.token_mint
                    and position.trader_wallet == trade.wallet
                    and position.status is PositionStatus.OPEN):
                fraction = Decimal(1) if trade.action is Action.SELL else Decimal("0.5")
                await self._close(position, when, ExitReason.MIRROR, fraction)

    # ---------------------------------------------------------------- exits
    async def _tick_exits(self, when: datetime) -> None:
        for position in list(self.positions.values()):
            if position.status is not PositionStatus.OPEN:
                continue
            market = await self.provider.market_state(position.token_mint,
                                                      decimals=position.decimals)
            if market.price_usd <= 0 or market.stale:
                continue
            update_marks(position, market, self.risk)
            trader = self.traders.get(position.trader_wallet)
            intent = evaluate_exits(position, market, trader, cfg=self.risk, now=when)
            if intent is not None:
                await self._close(position, when, intent.reason, intent.fraction,
                                  ladder_index=intent.detail.get("ladder_index"))

    async def _liquidate_all(self, when: datetime) -> None:
        for position in list(self.positions.values()):
            if position.status is PositionStatus.OPEN:
                await self._close(position, when, ExitReason.MANUAL, Decimal(1))

    async def _close(self, position: Position, when: datetime, reason: ExitReason,
                     fraction: Decimal, ladder_index: int | None = None) -> None:
        amount_raw = int(Decimal(position.amount_raw) * fraction)
        if amount_raw <= 0:
            return
        fully = fraction >= Decimal("0.999") or amount_raw >= position.amount_raw
        if fully:
            amount_raw = position.amount_raw

        market = await self.provider.market_state(position.token_mint,
                                                  decimals=position.decimals)
        if market.price_usd <= 0:
            return

        candidate = TradeCandidate(
            source_trade=SourceTrade(
                wallet=position.trader_wallet, signature=f"bt-exit-{position.id.hex[:8]}",
                action=Action.SELL, token_mint=position.token_mint, quote_mint=SOL_MINT,
                token_amount_raw=amount_raw, token_decimals=position.decimals,
                source_price=market.price_usd, source_confirmed_at=when,
            ),
            trader=self.traders.get(position.trader_wallet) or TraderProfile(
                wallet=position.trader_wallet),
            market=market,
            quote=await self.provider.quote(
                input_mint=position.token_mint, output_mint=SOL_MINT,
                amount_raw=amount_raw),
        )
        order = ApprovedOrder(
            idempotency_key=f"bt:{position.id}:{reason.value}:{amount_raw}",
            decision_id=position.id, candidate=candidate, action=Action.SELL,
            input_mint=position.token_mint, output_mint=SOL_MINT,
            in_amount_raw=amount_raw,
            size_usd=raw_to_ui(amount_raw, position.decimals) * market.price_usd,
            slippage_bps=settings.copyability.execution_slippage_bps,
            position_id=position.id, exit_reason=reason,
            latency=LatencyBreakdown(source_confirmed_at=when, detected_at=when,
                                     decision_completed_at=when),
        )
        ex = await self.executor.execute(order)
        if not ex.succeeded:
            return   # could not get out at this price point; try again next tick

        exit_price = ex.actual_price or market.price_usd
        proceeds, pnl = realized_pnl(position, amount_raw, exit_price)
        fee = self.executor.fee_usd()
        pnl -= fee

        await self.ledger.release_position(
            mint=position.token_mint, wallet=position.trader_wallet,
            cost_released_usd=proceeds - pnl - fee, realized_pnl_usd=pnl,
            proceeds_usd=proceeds, fully_closed=fully,
        )

        self.result.realized_pnl_usd += pnl
        self.result.fees_usd += fee
        self.result.exit_reasons[reason.value] = self.result.exit_reasons.get(reason.value, 0) + 1

        entry = position.entry_price
        position.amount_raw -= amount_raw
        position.realized_pnl_usd += pnl
        position.fees_usd += fee
        if ladder_index is not None:
            position.tp_levels_hit.append(ladder_index)
        if fully or position.amount_raw <= 0:
            position.status = PositionStatus.CLOSED
            position.closed_at = when
            self.positions.pop(position.id, None)
        else:
            position.cost_basis_usd -= (proceeds - pnl - fee)

        self.result.trades.append(ClosedTrade(
            mint=position.token_mint, trader=position.trader_wallet,
            opened_at=position.opened_at, closed_at=when,
            entry_price=entry, exit_price=exit_price,
            size_usd=proceeds - pnl, realized_pnl_usd=pnl, fees_usd=fee,
            exit_reason=reason.value,
        ))

    # ----------------------------------------------------------------- mark
    async def _mark(self, when: datetime) -> None:
        unrealized = ZERO
        for position in self.positions.values():
            if position.status is not PositionStatus.OPEN:
                continue
            market = await self.provider.market_state(position.token_mint,
                                                      decimals=position.decimals)
            if market.price_usd > 0:
                position.current_price = market.price_usd
                unrealized += position.unrealized_pnl_usd

        equity, _hwm = await self.ledger.mark(unrealized)
        if equity > self.result.peak_equity:
            self.result.peak_equity = equity
        if self.result.peak_equity > 0:
            dd = (self.result.peak_equity - equity) / self.result.peak_equity * Decimal(100)
            if dd > self.result.max_drawdown_pct:
                self.result.max_drawdown_pct = dd
        if len(self.result.equity_curve) < 5000:
            self.result.equity_curve.append(
                (when.isoformat(), str(equity.quantize(Decimal("0.01"))))
            )

    async def _cleanup(self) -> None:
        """Backtest ledgers are disposable; do not leave them in Redis."""
        r = get_redis()
        async for key in r.scan_iter(match=f"asm:bt:{self.id}:*"):
            await r.delete(key)
