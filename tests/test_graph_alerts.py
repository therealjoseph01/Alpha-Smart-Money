"""PRD 39 wallet graph and PRD 43 alert delivery."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from asm.alerts import Alerter, Category, Level
from asm.engine.graph import (
    Relationship,
    TradeEvent,
    cluster_risk_multiplier,
    detect_co_trading,
    detect_funding_links,
    find_clusters,
    merge,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
A, B, C, D = "walletA", "walletB", "walletC", "walletD"


def _events(wallet: str, mints: list[str], offset_seconds: int = 0) -> list[TradeEvent]:
    return [TradeEvent(wallet=wallet, mint=m,
                       at=T0 + timedelta(hours=i, seconds=offset_seconds))
            for i, m in enumerate(mints)]


# --------------------------------------------------------------- co-trading
def test_synchronised_wallets_are_linked():
    mints = ["m1", "m2", "m3", "m4"]
    events = _events(A, mints) + _events(B, mints, offset_seconds=20)
    rels = detect_co_trading(events)
    assert len(rels) == 1
    assert rels[0].strength > Decimal("50")
    assert rels[0].evidence["shared_tokens"] == 4


def test_one_shared_token_is_not_a_relationship():
    """Two good traders buying the same hot token proves nothing."""
    events = _events(A, ["m1"]) + _events(B, ["m1"], offset_seconds=5)
    assert detect_co_trading(events) == []


def test_far_apart_trades_are_not_linked():
    events = _events(A, ["m1", "m2", "m3"])
    late = [TradeEvent(wallet=B, mint=e.mint, at=e.at + timedelta(days=1))
            for e in events]
    assert detect_co_trading(events + late) == []


def test_tight_synchronisation_scores_higher_than_loose():
    mints = ["m1", "m2", "m3", "m4", "m5"]
    tight = detect_co_trading(_events(A, mints) + _events(B, mints, offset_seconds=5))
    loose = detect_co_trading(_events(C, mints) + _events(D, mints, offset_seconds=1500))
    assert tight[0].strength > loose[0].strength


# ------------------------------------------------------------------ funding
def test_funding_link_detected():
    rels = detect_funding_links([(A, B, Decimal("500")), (A, B, Decimal("250"))])
    assert len(rels) == 1
    assert rels[0].relation == "funding"
    assert rels[0].strength >= Decimal("70")
    assert rels[0].evidence["transfers"] == 2


def test_self_transfers_ignored():
    assert detect_funding_links([(A, A, Decimal("100"))]) == []


# ------------------------------------------------------------------ merge
def test_merge_keeps_strongest_and_records_the_other():
    rels = merge([
        Relationship(A, B, "co_trading", Decimal("65"), {}),
        Relationship(A, B, "funding", Decimal("90"), {}),
    ])
    assert len(rels) == 1
    assert rels[0].relation == "funding"
    assert "also_co_trading" in rels[0].evidence


# --------------------------------------------------------------- clusters
def test_transitive_cluster_is_one_risk_unit():
    """A-B and B-C linked means A, B and C are one bet, not three."""
    rels = [
        Relationship(A, B, "funding", Decimal("90"), {}),
        Relationship(B, C, "co_trading", Decimal("75"), {}),
    ]
    clusters = find_clusters(rels)
    assert len(clusters) == 1
    assert clusters[0] == {A, B, C}


def test_weak_links_do_not_form_clusters():
    rels = [Relationship(A, B, "co_trading", Decimal("30"), {})]
    assert find_clusters(rels) == []


def test_cluster_multiplier_divides_the_budget():
    """Five linked wallets must not each get a full allocation."""
    assert cluster_risk_multiplier(1) == Decimal(1)
    assert cluster_risk_multiplier(5) == Decimal("0.2")
    assert cluster_risk_multiplier(2) == Decimal("0.5")


# ------------------------------------------------------------------ alerts
class RecordingChannel:
    name = "recording"

    def __init__(self, fail: bool = False):
        self.sent = []
        self.fail = fail

    async def send(self, level, title, body, detail):
        if self.fail:
            raise RuntimeError("channel down")
        self.sent.append((level, title, body, detail))
        return True


@pytest.fixture
def no_db(monkeypatch):
    import asm.alerts as A_

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def add(self, row):
            pass

    monkeypatch.setattr(A_, "session_scope", lambda: FakeSession())


async def test_alert_delivered_to_channel(redis, no_db):
    ch = RecordingChannel()
    ok = await Alerter([ch], min_level=Level.INFO).send(
        Level.CRITICAL, Category.RISK, "Drawdown limit hit", "equity -21%")
    assert ok
    assert ch.sent[0][1] == "Drawdown limit hit"


async def test_below_min_level_is_stored_but_not_sent(redis, no_db):
    ch = RecordingChannel()
    ok = await Alerter([ch], min_level=Level.CRITICAL).send(
        Level.INFO, Category.TRADE, "a trade happened")
    assert not ok
    assert ch.sent == []


async def test_channel_failure_never_raises(redis, no_db):
    """A Telegram outage must not stop a position from exiting."""
    broken, good = RecordingChannel(fail=True), RecordingChannel()
    ok = await Alerter([broken, good], min_level=Level.INFO).send(
        Level.CRITICAL, Category.SYSTEM, "kill switch")
    assert ok, "a working channel should still deliver"
    assert good.sent


async def test_all_channels_failing_does_not_raise(redis, no_db):
    ok = await Alerter([RecordingChannel(fail=True)], min_level=Level.INFO).send(
        Level.CRITICAL, Category.SYSTEM, "kill switch")
    assert ok is False


async def test_duplicate_alerts_are_rate_limited(redis, no_db):
    ch = RecordingChannel()
    a = Alerter([ch], min_level=Level.INFO)
    first = await a.send(Level.WARNING, Category.RISK, "same", dedupe_key="k1")
    second = await a.send(Level.WARNING, Category.RISK, "same", dedupe_key="k1")
    assert first and not second
    assert len(ch.sent) == 1
