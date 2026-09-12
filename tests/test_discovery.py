"""PRD 11 - the discovery funnel.

The screen's job is narrow and worth stating: decide whether a wallet is worth spending
~300 Helius credits to analyse. It must NOT try to decide whether the wallet is good -
a high GMGN P&L is exactly the "profitable but uncopyable" trap the product exists to
catch.
"""
from __future__ import annotations

from decimal import Decimal

from asm.adapters.gmgn import NullDiscovery, screen, to_profile
from asm.domain.enums import TraderStatus

FLOORS = dict(min_realized_usd=Decimal("1000"), min_trades=20,
              min_winrate=Decimal("40"))


def profits(**kw):
    base = {"realized_profit": "5000", "total_profit": "6000",
            "buy": 40, "sell": 35, "winrate": 0.55}
    return {**base, **kw}


def test_healthy_wallet_passes():
    ok, _ = screen(profits(), **FLOORS)
    assert ok


def test_inactive_wallet_rejected():
    ok, reason = screen(profits(buy=2, sell=1), **FLOORS)
    assert not ok and "trades" in reason


def test_losing_wallet_rejected():
    ok, reason = screen(profits(realized_profit="-800", total_profit="-500"), **FLOORS)
    assert not ok and "no profit" in reason


def test_tiny_profit_rejected():
    ok, reason = screen(profits(realized_profit="50", total_profit="60"), **FLOORS)
    assert not ok and "below" in reason


def test_low_winrate_rejected():
    ok, reason = screen(profits(winrate=0.15), **FLOORS)
    assert not ok and "winrate" in reason


def test_winrate_accepted_as_fraction_or_percent():
    """GMGN documents 0-1, but percent shows up in the wild. Both must work."""
    assert screen(profits(winrate=0.55), **FLOORS)[0]
    assert screen(profits(winrate=55), **FLOORS)[0]


def test_missing_fields_do_not_crash():
    ok, _ = screen({}, **FLOORS)
    assert ok is False


def test_screen_is_permissive_not_selective():
    """A wallet with enormous profit but terrible copyability still PASSES the screen.

    That is deliberate: the screen decides who gets analysed, not who gets copied. If it
    rejected on copyability it would be making the judgement the product exists to make
    from real on-chain data.
    """
    ok, _ = screen(profits(realized_profit="900000", buy=500, sell=480), **FLOORS)
    assert ok


# ----------------------------------------------------- GMGN never sets the score
def test_gmgn_figures_never_enter_the_score():
    """PRD 5.1 - only our own measurement may move capital."""
    p = to_profile("W"*43, profits(realized_profit="999999"), {"source": "smartmoney"})
    assert p.scores.composite == Decimal(0)
    assert p.scores.copyability == Decimal(0)
    assert p.scores.confidence == Decimal(0)
    assert p.status is TraderStatus.DISCOVERED, "discovery must never grant capital"
    assert p.notes["gmgn_realized_profit"] == "999999"


def test_profile_records_provenance():
    p = to_profile("W"*43, profits(), {"source": "kol", "tags": ["kol"],
                                       "twitter": "someone"})
    assert p.source.startswith("gmgn:")
    assert p.notes["gmgn_tags"] == ["kol"]


# ---------------------------------------------------------------- degradation
async def test_null_source_returns_nothing_rather_than_failing():
    """PRD 53 - if GMGN dies, discovery pauses; trading does not."""
    n = NullDiscovery()
    assert await n.discover_wallets(limit=10) == []
    assert await n.wallet_profits(["a"]) == {}
    assert await n.token_security("m") == {}


def test_adapter_disabled_without_api_key():
    from asm.adapters.gmgn import GmgnClient

    assert not GmgnClient(api_key="").enabled


def test_rate_pacing_matches_published_weights():
    """calls/sec = plan weight / endpoint weight. Free plan is weight 5."""
    from asm.adapters.gmgn import WEIGHTS

    free = 5
    assert free / WEIGHTS["/v1/user/smartmoney"] == 5      # discovery: 5/sec
    assert free / WEIGHTS["/v1/user/wallet_profits"] < 2   # bulk P&L: ~1.7/sec
    assert WEIGHTS["/v1/user/wallet_profits"] == 3


# -------------------------------------------------------------- roster sizing
def test_budget_switches_speed_when_roster_fills():
    """Fast while the roster is short, slow once it is full - scoring costs credits."""
    from asm.config import settings

    assert settings.scoring_budget_bootstrap > settings.scoring_budget_steady
    assert settings.target_roster_size > 0
    # Bootstrap must actually fill the roster in a sane number of days.
    needed = settings.target_roster_size * 10          # ~1 in 10 survives
    days = needed / settings.scoring_budget_bootstrap
    assert days <= 45, f"bootstrap would take {days:.0f} days"


# ------------------------------------------ discovery must verify too (PRD 11)
async def test_discovery_rejects_launchpad_tokens(redis, monkeypatch):
    """GMGN's `maker` should always be a wallet. This does not rely on that.

    Trusting an upstream field to be well-formed is what turned a five-address
    watchlist into ~49,000 websocket notifications.
    """
    import asm.services.tasks.discovery as D

    class FakeGmgn:
        enabled = True

        async def discover_wallets(self, *, limit=100, **kw):
            return ["Bj1CbypNWSvA3VAX15m5y92RL3ujftj747yNjwqpump",
                    "5yb3D1KBy13czATSYGLUbZrYJvRvFQiH9XYkAeG2nDzF"]

        def discovery_meta(self):
            return {}

        async def wallet_profits(self, wallets, **kw):
            # Only the non-token address should ever reach this call.
            assert all(not w.endswith("pump") for w in wallets), \
                "a launchpad token reached the profit screen"
            return {w: {"realized_profit": "5000", "total_profit": "6000",
                        "buy": 40, "sell": 35, "winrate": 0.55} for w in wallets}

    monkeypatch.setattr(D, "gmgn", lambda: FakeGmgn())

    async def no_wallets_known(*a, **kw):
        class R:
            @staticmethod
            def all():
                return []
        return R()

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def execute(self, *a, **kw):
            return await no_wallets_known()
        def add(self, *a):
            pass

    monkeypatch.setattr(D, "session_scope", lambda: FakeSession())

    async def fake_verify_many(addresses):
        from asm.ingest.verify import Verdict
        return {a: Verdict(a, True, "wallet", "ok") for a in addresses}

    monkeypatch.setattr("asm.ingest.verify.verify_many", fake_verify_many)

    async def fake_upsert(*a, **kw):
        return None

    monkeypatch.setattr(D.repo, "upsert_trader", fake_upsert)
    monkeypatch.setattr(D.repo, "log_event", fake_upsert)

    result = await D.discover_wallets({}, auto_add=True)
    assert result["rejected_not_wallet"] >= 1


# ------------------------------------------ frequency ceiling (the real filter)
def test_hyperactive_wallets_are_rejected():
    """Measured, not theoretical: 27 wallets averaging ~230 trades/day cost 350,000
    Helius credits a day - ten times a 1M monthly plan - and could not have been
    copied anyway, because an edge that lives ~100 seconds is gone before a follower
    sees the trade."""
    ok, reason = screen(
        profits(buy=12_000, sell=12_000),        # 800 trades/day over 30 days
        max_trades_per_day=100, period_days=30, **FLOORS,
    )
    assert not ok
    assert "too fast to copy" in reason


def test_a_swing_trader_passes():
    """20 trades/day, ~70 minutes between trades - an edge that survives 3 seconds."""
    ok, _ = screen(profits(buy=300, sell=300),
                   max_trades_per_day=100, period_days=30, **FLOORS)
    assert ok


def test_frequency_is_judged_before_profitability():
    """A hyperactive wallet with enormous P&L must still be rejected.

    This is the product's thesis as a unit test: profitable and copyable are
    different properties, and speed is the one that cannot be transferred.
    """
    ok, reason = screen(
        profits(buy=15_000, sell=15_000, realized_profit="5000000"),
        max_trades_per_day=100, period_days=30, **FLOORS,
    )
    assert not ok
    assert "too fast" in reason


def test_excluded_tags_reject_outright():
    ok, reason = screen(profits(), tags=["kol", "wash_trader"],
                        excluded_tags=("wash_trader",),
                        max_trades_per_day=100, **FLOORS)
    assert not ok
    assert "wash_trader" in reason


def test_the_ceiling_is_optional():
    """Omitting it must not silently reject everything."""
    ok, _ = screen(profits(buy=12_000, sell=12_000), **FLOORS)
    assert ok
