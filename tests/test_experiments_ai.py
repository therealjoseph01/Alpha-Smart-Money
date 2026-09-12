"""PRD 40, 46, 57, 66, 69."""
from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from asm.ai import FORBIDDEN_KEYS, AIUnavailable, Analyst, redact
from asm.config import CopyabilityConfig
from asm.engine.experiments import (
    Arm,
    ArmResult,
    Experiment,
    ExperimentError,
    ExperimentRegistry,
    compare_arms,
    strategy_checksum,
)


# ------------------------------------------------------------ experiments
def test_arms_are_deterministic_per_unit():
    """The same trader must never drift between arms mid-experiment."""
    exp = Experiment("slippage", [Arm("control"), Arm("tight", {"max_expected_slippage_pct": Decimal("1")})])
    first = exp.assign("walletA")
    for _ in range(100):
        assert exp.assign("walletA").name == first.name


def test_arms_split_the_population():
    exp = Experiment("x", [Arm("control"), Arm("variant")])
    names = {exp.assign(f"wallet{i}").name for i in range(200)}
    assert names == {"control", "variant"}


def test_weights_are_respected():
    exp = Experiment("x", [Arm("control", weight=9), Arm("variant", weight=1)])
    variant = sum(exp.assign(f"w{i}").name == "variant" for i in range(1000))
    assert 50 < variant < 160, f"expected roughly 10%, got {variant / 10}%"


def test_experiment_cannot_touch_a_hard_risk_limit():
    """PRD 4 - no autonomous change to hard limits, including via an 'experiment'."""
    with pytest.raises(ExperimentError, match="hard limit"):
        Experiment("reckless", [Arm("control"), Arm("wide", {"stop_loss_pct": Decimal("-90")})])

    with pytest.raises(ExperimentError, match="hard limit"):
        Experiment("reckless", [Arm("control"), Arm("x", {"max_daily_loss_pct": Decimal("50")})])


def test_experiment_rejects_unknown_fields():
    with pytest.raises(ExperimentError, match="unknown or protected"):
        Experiment("typo", [Arm("control"), Arm("v", {"nonexistent_field": 1})])


def test_experiment_needs_a_control():
    with pytest.raises(ExperimentError, match="at least a control"):
        Experiment("solo", [Arm("only")])


def test_registry_applies_overrides_and_records_assignment():
    registry = ExperimentRegistry([
        Experiment("liquidity", [
            Arm("control"),
            Arm("strict", {"min_liquidity_usd": Decimal("100000")}),
        ])
    ])
    cfg, assignments = registry.config_for("walletA")
    assert isinstance(cfg, CopyabilityConfig)
    assert "liquidity" in assignments
    if assignments["liquidity"] == "strict":
        assert cfg.min_liquidity_usd == Decimal("100000")


def test_disabled_experiments_are_ignored():
    registry = ExperimentRegistry([
        Experiment("off", [Arm("control"), Arm("v", {"min_liquidity_usd": Decimal("1")})],
                   enabled=False)
    ])
    _cfg, assignments = registry.config_for("walletA")
    assert assignments == {}


# ------------------------------------------------------ strategy versioning
def test_checksum_changes_when_config_changes():
    a = strategy_checksum(CopyabilityConfig(), "balanced", "v1")
    b = strategy_checksum(CopyabilityConfig(min_liquidity_usd=Decimal("99999")),
                          "balanced", "v1")
    assert a != b


def test_checksum_is_stable_for_identical_config():
    assert (strategy_checksum(CopyabilityConfig(), "balanced", "v1")
            == strategy_checksum(CopyabilityConfig(), "balanced", "v1"))


# ---------------------------------------------------------- arm comparison
def test_small_samples_are_inconclusive():
    """The most important guardrail: no promoting a winner off 5 trades."""
    out = compare_arms([ArmResult("control", 5, 3, Decimal("50")),
                        ArmResult("variant", 4, 4, Decimal("200"))])
    assert out["verdict"] == "inconclusive"
    assert "do not promote" in out["reason"]


def test_clear_leader_identified_with_enough_data():
    out = compare_arms([ArmResult("control", 100, 40, Decimal("100")),
                        ArmResult("variant", 100, 60, Decimal("900"))])
    assert out["verdict"] == "leader"
    assert out["leader"] == "variant"


def test_close_results_are_not_a_winner():
    out = compare_arms([ArmResult("control", 100, 50, Decimal("500")),
                        ArmResult("variant", 100, 51, Decimal("505"))])
    assert out["verdict"] == "too_close"


# ------------------------------------------------------------------- AI
def test_ai_is_off_without_a_key():
    assert not Analyst(api_key="").enabled


async def test_ai_refuses_to_run_when_disabled():
    with pytest.raises(AIUnavailable, match="not configured"):
        await Analyst(api_key="").analyse("anything", {})


def test_secrets_never_reach_the_model():
    dirty = {
        "private_key": "abc", "api_key": "sk-123", "SOLANA_KEYPAIR": "[1,2,3]",
        "nested": {"password": "hunter2", "authorization": "Bearer x"},
        "pnl_usd": "42",
    }
    clean = redact(dirty)
    blob = str(clean)
    assert "sk-123" not in blob
    assert "hunter2" not in blob
    assert "Bearer x" not in blob
    assert clean["pnl_usd"] == "42"


def test_wallet_addresses_are_truncated_to_non_actionable():
    clean = redact({"wallet": "5yb3D1KBy13czATSYGLUbZrYJvRvFQiH9XYkAeG2nDzF"})
    assert clean["wallet"].endswith("…")
    assert len(clean["wallet"]) < 12


def test_ai_layer_is_not_reachable_from_the_hot_path():
    """PRD 5.6 enforced structurally, not by policy.

    If someone imports the AI layer into the decision path, this fails. That is the
    whole guarantee: AI cannot influence a trade because the trading code cannot see it.
    """
    protected = [
        "src/asm/engine/decision.py", "src/asm/engine/gates.py",
        "src/asm/engine/sizing.py", "src/asm/engine/exits.py",
        "src/asm/engine/risk.py", "src/asm/state/ledger.py",
        "src/asm/execution/paper.py", "src/asm/execution/live.py",
        "src/asm/execution/signer.py", "src/asm/services/decision_service.py",
        "src/asm/services/position_service.py",
    ]
    for path in protected:
        f = Path(path)
        if not f.exists():
            continue
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "asm.ai" not in alias.name, f"{path} imports the AI layer"
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "asm.ai", f"{path} imports the AI layer"
                assert not (node.module or "").startswith("asm.ai"), \
                    f"{path} imports the AI layer"


def test_forbidden_key_list_covers_the_obvious_ones():
    for key in ("private_key", "keypair", "mnemonic", "seed_phrase", "api_key"):
        assert key in FORBIDDEN_KEYS
