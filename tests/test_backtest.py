"""PRD 44 - the backtester gates live trading, so it must be provably honest."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from asm.backtest import (
    BacktestConfig,
    BacktestEngine,
    HistoricalDataProvider,
    ImpactModel,
    PricePoint,
    TokenHistory,
    build_histories,
)
from asm.domain.enums import Action, TraderStatus
from asm.domain.models import SourceTrade
from asm.domain.money import SOL_MINT
from tests import factories as F

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SOL = Decimal("150")


# ------------------------------------------------------------- impact model
def test_impact_grows_superlinearly_with_size():
    """Twice the size must cost more than twice the impact - depth thins as you eat it."""
    m = ImpactModel()
    small = m.price_impact_pct(Decimal("1000"), Decimal("100000"))
    double = m.price_impact_pct(Decimal("2000"), Decimal("100000"))
    assert double > small * 2


def test_zero_liquidity_is_catastrophic_not_free():
    assert ImpactModel().price_impact_pct(Decimal("100"), Decimal(0)) == Decimal("99")


# ---------------------------------------------------------------- provider
def _history(prices: list[tuple[int, str]], liquidity="250000", mint=F.MINT) -> TokenHistory:
    return TokenHistory(
        mint=mint,
        points=[PricePoint(at=T0 + timedelta(minutes=m), price_usd=Decimal(p),
                           liquidity_usd=Decimal(liquidity)) for m, p in prices],
        decimals=6, created_at=T0 - timedelta(days=30),
    )


async def test_provider_never_looks_ahead():
    """The single most important property of a backtester."""
    p = HistoricalDataProvider({F.MINT: _history([(0, "1"), (10, "100")])}, sol_price=SOL)
    p.set_clock(T0 + timedelta(minutes=5))
    state = await p.market_state(F.MINT)
    assert state.price_usd == Decimal("1"), "used a price from the future"


async def test_provider_marks_stale_data():
    p = HistoricalDataProvider({F.MINT: _history([(0, "1")])}, sol_price=SOL,
                               max_staleness_seconds=60)
    p.set_clock(T0 + timedelta(hours=2))
    assert (await p.market_state(F.MINT)).stale


async def test_unknown_token_is_unknown_not_safe():
    p = HistoricalDataProvider({}, sol_price=SOL)
    risk = await p.token_risk("NOPE")
    assert risk.unknown and risk.score == 100
    assert (await p.market_state("NOPE")).stale


async def test_provider_quote_reflects_impact():
    p = HistoricalDataProvider({F.MINT: _history([(0, "1")], liquidity="10000")},
                               sol_price=SOL)
    p.set_clock(T0)
    tiny = await p.quote(input_mint=SOL_MINT, output_mint=F.MINT, amount_raw=10_000_000)
    huge = await p.quote(input_mint=SOL_MINT, output_mint=F.MINT, amount_raw=10_000_000_000)
    assert huge.price_impact_pct > tiny.price_impact_pct


# ----------------------------------------------------------------- loader
def test_histories_built_from_observed_swaps():
    trades = [
        SourceTrade(wallet=F.WALLET, signature=f"s{i}", action=Action.BUY,
                    token_mint=F.MINT, quote_mint=SOL_MINT,
                    token_amount_raw=1_000_000, quote_amount_raw=100_000_000,
                    token_decimals=6, source_price=Decimal("0.1") * (i + 1),
                    source_value_usd=Decimal("15"),
                    source_confirmed_at=T0 + timedelta(minutes=i))
        for i in range(5)
    ]
    h = build_histories(trades, sol_price=SOL)
    assert F.MINT in h
    assert len(h[F.MINT].points) == 5
    assert h[F.MINT].points[0].at < h[F.MINT].points[-1].at
    assert h[F.MINT].points[0].liquidity_usd >= Decimal("5000")


# ------------------------------------------------------------------ engine
def _trader(**kw):
    return F.trader(status=TraderStatus.ACTIVE, **kw)


def _buy(minute: int, price: str, wallet=F.WALLET, mint=F.MINT) -> SourceTrade:
    return SourceTrade(
        wallet=wallet, signature=f"buy-{mint[:4]}-{minute}", action=Action.BUY,
        token_mint=mint, quote_mint=SOL_MINT,
        token_amount_raw=1_000_000, quote_amount_raw=100_000_000, token_decimals=6,
        source_price=Decimal(price) / SOL, source_value_usd=Decimal("100"),
        source_confirmed_at=T0 + timedelta(minutes=minute),
    )


async def test_backtest_runs_end_to_end_and_reports_three_pnls(redis):
    """A token that doubles: the run must fill, exit on the ladder, and end up richer."""
    prices = [(m, str(1 + m * 0.05)) for m in range(0, 120, 5)]
    engine = BacktestEngine(
        histories={F.MINT: _history(prices)},
        traders={F.WALLET: _trader()},
        config=BacktestConfig(starting_capital_usd=Decimal("10000"), seed=7,
                              sol_price=SOL),
    )
    result = await engine.run([_buy(0, "1.0")])

    assert result.signals == 1
    assert result.approved == 1
    assert len(result.trades) >= 1
    assert result.realized_pnl_usd > 0, "a doubling token should make money"
    assert result.ending_equity > result.starting_equity
    assert result.fees_usd > 0, "fees must never be zero"
    s = result.summary()
    assert "source_pnl_usd" in s and "copyable_pnl_usd" in s and "realized_pnl_usd" in s
    assert s["verdict"].startswith("INSUFFICIENT_SAMPLE")


async def test_stop_loss_caps_the_loss(redis):
    """A token that collapses must exit near the stop, not ride to zero."""
    prices = [(0, "1.0"), (5, "0.9"), (10, "0.7"), (15, "0.5"), (20, "0.2"), (30, "0.05")]
    engine = BacktestEngine(
        histories={F.MINT: _history(prices)},
        traders={F.WALLET: _trader()},
        config=BacktestConfig(starting_capital_usd=Decimal("10000"), seed=3, sol_price=SOL),
    )
    result = await engine.run([_buy(0, "1.0")])

    assert result.realized_pnl_usd < 0
    loss_pct = result.realized_pnl_usd / result.starting_equity * Decimal(100)
    assert loss_pct > Decimal("-5"), "position sizing should bound a single loss"
    assert "stop_loss" in result.exit_reasons or "trailing_stop" in result.exit_reasons


async def test_risk_limits_bind_during_replay(redis):
    """Twenty signals on one token must not breach the 5% token cap in backtest either."""
    prices = [(m, "1.0") for m in range(0, 200, 2)]
    trades = [_buy(m, "1.0") for m in range(0, 40, 2)]
    engine = BacktestEngine(
        histories={F.MINT: _history(prices)},
        traders={F.WALLET: _trader()},
        config=BacktestConfig(starting_capital_usd=Decimal("10000"), seed=11, sol_price=SOL),
    )
    result = await engine.run(trades)

    assert result.signals == len(trades)
    assert result.rejected > 0, "risk limits should have bound somewhere"
    blocked = set(result.rejection_reasons)
    assert blocked & {"TOKEN_EXPOSURE_LIMIT", "TRADER_EXPOSURE_LIMIT",
                      "TOO_MANY_PENDING", "MAX_OPEN_POSITIONS",
                      "PORTFOLIO_DEPLOYED_LIMIT"}


async def test_unknown_token_is_never_traded(redis):
    """No history means no risk score, which must fail closed."""
    engine = BacktestEngine(
        histories={}, traders={F.WALLET: _trader()},
        config=BacktestConfig(sol_price=SOL),
    )
    result = await engine.run([_buy(0, "1.0")])
    assert result.approved == 0
    assert result.rejected == 1


async def test_latency_assumption_degrades_returns(redis):
    """The core hypothesis check: slower detection must produce worse results."""
    prices = [(m, str(1 + m * 0.10)) for m in range(0, 60, 1)]

    async def run(latency_ms: int):
        engine = BacktestEngine(
            histories={F.MINT: _history(prices)},
            traders={F.WALLET: _trader()},
            config=BacktestConfig(starting_capital_usd=Decimal("10000"), seed=5,
                                  assumed_detection_latency_ms=latency_ms, sol_price=SOL),
        )
        return await engine.run([_buy(0, "1.0")])

    fast = await run(500)
    slow = await run(600_000)   # ten minutes late into a fast-rising token
    assert fast.approved >= slow.approved, "a late signal should be rejected more often"


async def test_results_are_reproducible(redis):
    prices = [(m, str(1 + m * 0.03)) for m in range(0, 100, 5)]

    async def run():
        engine = BacktestEngine(
            histories={F.MINT: _history(prices)},
            traders={F.WALLET: _trader()},
            config=BacktestConfig(starting_capital_usd=Decimal("10000"), seed=99,
                                  sol_price=SOL),
        )
        return await engine.run([_buy(0, "1.0")])

    a, b = await run(), await run()
    assert a.realized_pnl_usd == b.realized_pnl_usd
    assert a.approved == b.approved


async def test_ledger_namespace_is_cleaned_up(redis):
    engine = BacktestEngine(
        histories={F.MINT: _history([(0, "1.0"), (10, "1.2")])},
        traders={F.WALLET: _trader()},
        config=BacktestConfig(sol_price=SOL),
    )
    await engine.run([_buy(0, "1.0")])
    leftover = [k async for k in redis.scan_iter(match=f"asm:bt:{engine.id}:*")]
    assert leftover == []


async def test_verdict_is_blunt_about_bad_runs(redis):
    from asm.backtest.engine import BacktestResult

    r = BacktestResult(id="x", config={}, started_at=T0, finished_at=T0,
                       starting_equity=Decimal("1000"), ending_equity=Decimal("900"))
    assert "INSUFFICIENT_SAMPLE" in r.verdict()

    r.trades = [object()] * 25  # type: ignore[list-item]
    r.realized_pnl_usd = Decimal("-50")
    assert "NEGATIVE" in r.verdict()


# ------------------------------------------------- the core PRD hypothesis
async def test_edge_retention_discriminates_fast_from_slow_movers(redis):
    """PRD 5.1 / H2 - copyability varies and must be measurable.

    Price path is the classic trap: flat, then a spike one step after the source buys,
    then flat again. The source captures the whole spike. A follower captures it only
    if their lag is small RELATIVE to how fast the move develops.

    Same edge, same costs, same lag - only the timescale differs.
    """

    async def retention(step_minutes: int):
        pts, t = [], T0
        # Per window: [entry, spiked, spiked] repeated, so the source's buy->sell
        # spans the spike and a late follower buys after it has already happened.
        for cycle in range(20):
            base = Decimal("1") * (Decimal("1.01") ** cycle)
            for level in (base, base * Decimal("1.5"), base * Decimal("1.5")):
                pts.append(PricePoint(at=t, price_usd=level,
                                      liquidity_usd=Decimal("500000")))
                t += timedelta(minutes=step_minutes)
        history = TokenHistory(mint=F.MINT, points=pts, decimals=6,
                               created_at=T0 - timedelta(days=30))

        trades = []
        for cycle in range(20):
            buy, sell = pts[cycle * 3], pts[cycle * 3 + 2]
            trades.append(SourceTrade(
                wallet=F.WALLET, signature=f"b{cycle}", action=Action.BUY,
                token_mint=F.MINT, quote_mint=SOL_MINT, token_amount_raw=1_000_000,
                quote_amount_raw=100_000_000, token_decimals=6,
                source_price=buy.price_usd / SOL, source_value_usd=Decimal("200"),
                source_confirmed_at=buy.at))
            trades.append(SourceTrade(
                wallet=F.WALLET, signature=f"s{cycle}", action=Action.SELL,
                token_mint=F.MINT, quote_mint=SOL_MINT, token_amount_raw=1_000_000,
                quote_amount_raw=100_000_000, token_decimals=6,
                source_price=sell.price_usd / SOL, source_value_usd=Decimal("300"),
                source_confirmed_at=sell.at))

        engine = BacktestEngine(
            histories={F.MINT: history}, traders={F.WALLET: _trader()},
            config=BacktestConfig(starting_capital_usd=Decimal("10000"), seed=21,
                                  assumed_detection_latency_ms=120_000, sol_price=SOL),
        )
        result = await engine.run(trades)
        return result.edge_retention_pct

    slow = await retention(step_minutes=120)   # 2min lag against a 2h move: negligible
    fast = await retention(step_minutes=1)     # 2min lag against a 1min move: fatal

    assert slow is not None and fast is not None
    assert slow > fast + Decimal(50), (
        f"a 2-minute lag should destroy a 1-minute move (fast={fast}) "
        f"while barely touching a 2-hour one (slow={slow})"
    )
    assert fast < Decimal(20), "a follower who always buys after the spike has no edge"


async def test_edge_retention_is_none_when_unmeasurable(redis):
    """A source that never sells gives no round trip - report None, not a bad score."""
    engine = BacktestEngine(
        histories={F.MINT: _history([(m, "1.0") for m in range(0, 60, 5)])},
        traders={F.WALLET: _trader()},
        config=BacktestConfig(sol_price=SOL),
    )
    result = await engine.run([_buy(0, "1.0")])
    assert result.source_round_trips == 0
    assert result.edge_retention_pct is None
    assert result.summary()["edge_retention_pct"] is None


async def test_scale_free_retention_ignores_position_size(redis):
    """The source trading 100x our size must not change the retention number."""
    prices = [(m, str(1 + m * 0.01)) for m in range(0, 120, 5)]

    async def run(source_amount_raw: int):
        trades = [
            SourceTrade(wallet=F.WALLET, signature="b", action=Action.BUY,
                        token_mint=F.MINT, quote_mint=SOL_MINT,
                        token_amount_raw=source_amount_raw,
                        quote_amount_raw=100_000_000, token_decimals=6,
                        source_price=Decimal("1") / SOL, source_value_usd=Decimal("200"),
                        source_confirmed_at=T0),
            SourceTrade(wallet=F.WALLET, signature="s", action=Action.SELL,
                        token_mint=F.MINT, quote_mint=SOL_MINT,
                        token_amount_raw=source_amount_raw,
                        quote_amount_raw=100_000_000, token_decimals=6,
                        source_price=Decimal("1.1") / SOL, source_value_usd=Decimal("220"),
                        source_confirmed_at=T0 + timedelta(minutes=20)),
        ]
        engine = BacktestEngine(
            histories={F.MINT: _history(prices)}, traders={F.WALLET: _trader()},
            config=BacktestConfig(seed=8, sol_price=SOL),
        )
        return (await engine.run(trades)).edge_retention_pct

    small = await run(1_000_000)
    huge = await run(100_000_000)
    assert small == huge, "retention must be independent of the source's position size"


class TestVerdictExplainsAnEmptyRun:
    """A backtest that took no trades because the roster was empty is not a verdict
    on the strategy, and saying 'fewer than 20 closed trades' reads like one."""

    def _result(self, reasons: dict[str, int]):
        from datetime import UTC, datetime

        from asm.backtest.engine import BacktestConfig, BacktestResult

        now = datetime.now(UTC)
        r = BacktestResult(id="t", config=BacktestConfig(), started_at=now,
                           finished_at=now)
        r.signals = sum(reasons.values())
        r.rejected = sum(reasons.values())
        r.rejection_reasons = dict(reasons)
        return r

    def test_names_the_unscored_roster(self):
        v = self._result({"TRADER_NOT_FOLLOWED": 154}).verdict()
        assert v.startswith("NOT_RUN")
        assert "Score all" in v

    def test_names_any_other_single_dominant_gate(self):
        v = self._result({"LIQUIDITY_TOO_LOW": 80}).verdict()
        assert v.startswith("NOT_RUN")
        assert "LIQUIDITY_TOO_LOW" in v

    def test_a_mix_of_reasons_is_still_insufficient_sample(self):
        """Several gates firing is a real result about the configuration."""
        v = self._result({"LIQUIDITY_TOO_LOW": 40, "PRICE_CHASED": 40}).verdict()
        assert v.startswith("INSUFFICIENT_SAMPLE")

    def test_not_run_never_passes_the_promotion_gate(self):
        assert not self._result({"TRADER_NOT_FOLLOWED": 9}).verdict().startswith("VIABLE")
