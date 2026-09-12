"""PRD 16 - classification from balance deltas must survive unknown DEX programs."""
from __future__ import annotations

from decimal import Decimal

from asm.domain.enums import Action
from asm.domain.money import SOL_MINT
from asm.ingest.parser import parse_transaction

WALLET = "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW"
MINT = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def build_tx(*, token_delta: int, sol_delta_lamports: int, post_token: int | None = None,
             fee: int = 5000, err=None, logs=None) -> dict:
    """Minimal jsonParsed transaction with the wallet as fee payer (index 0)."""
    pre_lamports = 10_000_000_000
    post_lamports = pre_lamports + sol_delta_lamports - fee

    if post_token is None:
        pre_token = max(0, -token_delta)
        post_token = pre_token + token_delta
    else:
        pre_token = post_token - token_delta

    return {
        "slot": 123,
        "blockTime": 1_700_000_000,
        "transaction": {
            "signatures": ["SIG"],
            "message": {"accountKeys": [{"pubkey": WALLET}, {"pubkey": "OTHER"}]},
        },
        "meta": {
            "err": err,
            "fee": fee,
            "preBalances": [pre_lamports, 0],
            "postBalances": [post_lamports, 0],
            "logMessages": logs or ["Program log: swap"],
            "preTokenBalances": [{
                "accountIndex": 1, "mint": MINT, "owner": WALLET,
                "uiTokenAmount": {"amount": str(pre_token), "decimals": 6},
            }],
            "postTokenBalances": [{
                "accountIndex": 1, "mint": MINT, "owner": WALLET,
                "uiTokenAmount": {"amount": str(post_token), "decimals": 6},
            }],
        },
    }


def test_buy_classified():
    tx = build_tx(token_delta=1_000_000, sol_delta_lamports=-500_000_000)
    t = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    assert t is not None
    assert t.action is Action.BUY
    assert t.token_mint == MINT
    assert t.quote_mint == SOL_MINT
    assert t.source_value_usd == Decimal("100")   # 0.5 SOL * $200
    assert t.source_price == Decimal("0.5")       # 0.5 SOL / 1.0 token


def test_full_sell_classified():
    tx = build_tx(token_delta=-1_000_000, sol_delta_lamports=600_000_000, post_token=0)
    t = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    assert t.action is Action.SELL


def test_partial_sell_detected_from_remaining_balance():
    """A scale-out and a full exit must not be treated the same (PRD 28.1)."""
    tx = build_tx(token_delta=-500_000, sol_delta_lamports=300_000_000, post_token=500_000)
    t = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    assert t.action is Action.PARTIAL_SELL


def test_token_receipt_not_copyable():
    """PRD 19 - tokens received rather than bought are a classic insider tell."""
    tx = build_tx(token_delta=5_000_000, sol_delta_lamports=0)
    t = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    assert t.action is Action.TOKEN_RECEIPT
    assert not t.action.is_copyable


def test_outgoing_transfer_not_copyable():
    tx = build_tx(token_delta=-5_000_000, sol_delta_lamports=0, post_token=0)
    t = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    assert t.action is Action.TRANSFER
    assert not t.action.is_copyable


def test_failed_transaction_ignored():
    tx = build_tx(token_delta=1_000_000, sol_delta_lamports=-500_000_000,
                  err={"InstructionError": [0, "Custom"]})
    assert parse_transaction(tx, WALLET) is None


def test_fee_only_transaction_ignored():
    tx = build_tx(token_delta=0, sol_delta_lamports=0)
    assert parse_transaction(tx, WALLET) is None


def test_dust_trade_ignored():
    """Sub-dollar trades are noise, or bait for copy bots."""
    tx = build_tx(token_delta=1_000, sol_delta_lamports=-1_000)
    t = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    assert t is None


def test_fee_is_not_mistaken_for_a_sell():
    """The fee payer's lamport balance always drops; that must not read as SOL received."""
    tx = build_tx(token_delta=1_000_000, sol_delta_lamports=-500_000_000, fee=50_000)
    t = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    assert t.action is Action.BUY
    assert t.quote_amount_raw == 500_000_000, "fee must be excluded from the quote leg"


def test_wrapped_sol_folded_into_native_side():
    """A route that uses wSOL must classify identically to one that uses native SOL."""
    tx = build_tx(token_delta=1_000_000, sol_delta_lamports=0)
    tx["meta"]["preTokenBalances"].append({
        "accountIndex": 2, "mint": SOL_MINT, "owner": WALLET,
        "uiTokenAmount": {"amount": "500000000", "decimals": 9},
    })
    tx["meta"]["postTokenBalances"].append({
        "accountIndex": 2, "mint": SOL_MINT, "owner": WALLET,
        "uiTokenAmount": {"amount": "0", "decimals": 9},
    })
    t = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    assert t.action is Action.BUY
    assert t.quote_mint == SOL_MINT
    assert t.source_value_usd == Decimal("100")


def test_dedupe_key_is_stable_and_specific():
    tx = build_tx(token_delta=1_000_000, sol_delta_lamports=-500_000_000)
    a = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    b = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    assert a.dedupe_key == b.dedupe_key
    assert len(a.dedupe_key) == 40


def test_other_wallets_balances_ignored():
    """Only the watched wallet's own token accounts count."""
    tx = build_tx(token_delta=1_000_000, sol_delta_lamports=-500_000_000)
    tx["meta"]["postTokenBalances"].append({
        "accountIndex": 3, "mint": MINT, "owner": "SOMEONE_ELSE",
        "uiTokenAmount": {"amount": "999999999", "decimals": 6},
    })
    t = parse_transaction(tx, WALLET, sol_price_usd=Decimal("200"))
    assert t.token_amount_raw == 1_000_000
