"""PRD 16 - parse a Solana transaction into a SourceTrade.

Design choice: classify from *balance deltas*, not per-program instruction decoding.
A memecoin router set changes weekly (Raydium, Orca, Meteora, Pump.fun, Moonshot,
aggregators...). Net token balance change for the wallet is venue-agnostic and cannot
be spoofed by an unfamiliar inner instruction layout.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from asm.domain.enums import Action, Chain
from asm.domain.models import SourceTrade
from asm.domain.money import SOL_MINT, USDC_MINT, lamports_to_sol, raw_to_ui
from asm.logging import get_logger

log = get_logger(__name__)

# Mints treated as the "money" side of a swap.
QUOTE_MINTS = {
    SOL_MINT,
    USDC_MINT,
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
LP_PROGRAM_HINTS = ("addLiquidity", "removeLiquidity", "deposit", "withdraw")
DUST_USD = Decimal("1")


def _owner_token_deltas(tx: dict, wallet: str) -> dict[str, tuple[int, int]]:
    """mint -> (raw_delta, decimals) for token accounts owned by `wallet`."""
    meta = tx.get("meta") or {}
    pre = {}
    decimals: dict[str, int] = {}
    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") == wallet:
            key = (b["accountIndex"], b["mint"])
            pre[key] = int(b["uiTokenAmount"]["amount"])
            decimals[b["mint"]] = int(b["uiTokenAmount"]["decimals"])
    post = {}
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") == wallet:
            key = (b["accountIndex"], b["mint"])
            post[key] = int(b["uiTokenAmount"]["amount"])
            decimals[b["mint"]] = int(b["uiTokenAmount"]["decimals"])
    for (_idx, mint) in pre:
        decimals.setdefault(mint, 9)

    deltas: dict[str, int] = {}
    for key in set(pre) | set(post):
        _idx, mint = key
        deltas[mint] = deltas.get(mint, 0) + post.get(key, 0) - pre.get(key, 0)
    return {m: (d, decimals.get(m, 9)) for m, d in deltas.items() if d != 0}


def _native_sol_delta(tx: dict, wallet: str) -> int:
    """Lamport delta for the wallet, fee-adjusted when the wallet is the fee payer."""
    meta = tx.get("meta") or {}
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys") or []
    idx = None
    for i, k in enumerate(keys):
        addr = k.get("pubkey") if isinstance(k, dict) else k
        if addr == wallet:
            idx = i
            break
    if idx is None:
        return 0
    pre = (meta.get("preBalances") or [])
    post = (meta.get("postBalances") or [])
    if idx >= len(pre) or idx >= len(post):
        return 0
    delta = post[idx] - pre[idx]
    if idx == 0:  # fee payer: add the fee back so it is not read as a sell
        delta += int(meta.get("fee", 0))
    return delta


def _is_lp_interaction(tx: dict) -> bool:
    logs = " ".join((tx.get("meta") or {}).get("logMessages") or [])
    return any(hint in logs for hint in LP_PROGRAM_HINTS)


def parse_transaction(
    tx: dict[str, Any],
    wallet: str,
    *,
    signature: str | None = None,
    sol_price_usd: Decimal = Decimal(0),
    detected_at: datetime | None = None,
) -> SourceTrade | None:
    """Return a SourceTrade, or None when the tx is not a copyable trade."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None

    sig = signature or ((tx.get("transaction") or {}).get("signatures") or [None])[0]
    if not sig:
        return None

    block_time = tx.get("blockTime")
    confirmed_at = (
        datetime.fromtimestamp(block_time, tz=UTC) if block_time else datetime.now(UTC)
    )
    now = detected_at or datetime.now(UTC)

    token_deltas = _owner_token_deltas(tx, wallet)
    sol_delta = _native_sol_delta(tx, wallet)

    # Fold wrapped SOL into the native side so wSOL routes classify identically.
    if SOL_MINT in token_deltas:
        wsol_delta, _ = token_deltas.pop(SOL_MINT)
        sol_delta += wsol_delta

    non_quote = {m: v for m, v in token_deltas.items() if m not in QUOTE_MINTS}
    stable = {m: v for m, v in token_deltas.items() if m in QUOTE_MINTS}

    if not non_quote:
        return None

    # The traded token is the non-quote mint with the largest absolute movement.
    mint, (raw_delta, decimals) = max(non_quote.items(), key=lambda kv: abs(kv[1][0]))

    # Quote side: native SOL, else a stablecoin leg.
    if stable:
        quote_mint, (quote_delta, quote_decimals) = max(
            stable.items(), key=lambda kv: abs(kv[1][0])
        )
        quote_ui = raw_to_ui(abs(quote_delta), quote_decimals)
        quote_usd = quote_ui
    elif sol_delta != 0:
        quote_mint = SOL_MINT
        quote_delta = sol_delta
        quote_decimals = 9
        quote_ui = lamports_to_sol(abs(quote_delta))
        quote_usd = quote_ui * sol_price_usd
    else:
        quote_mint, quote_delta, quote_decimals = SOL_MINT, 0, 9
        quote_ui = Decimal(0)
        quote_usd = Decimal(0)

    if _is_lp_interaction(tx):
        action = Action.LP_INTERACTION
    elif raw_delta > 0 and quote_delta < 0:
        action = Action.BUY
    elif raw_delta < 0 and quote_delta > 0:
        action = Action.SELL
    elif raw_delta > 0 and quote_delta == 0:
        # PRD 19: received tokens rather than buying them - never copy this
        action = Action.TOKEN_RECEIPT
    elif raw_delta < 0 and quote_delta == 0:
        action = Action.TRANSFER
    else:
        action = Action.UNKNOWN

    token_ui = raw_to_ui(abs(raw_delta), decimals)
    source_price = (quote_ui / token_ui) if token_ui else Decimal(0)

    if action is Action.SELL:
        # PRD 16: distinguish a full exit from a scale-out using the remaining balance.
        remaining = _post_balance(tx, wallet, mint)
        if remaining > 0:
            action = Action.PARTIAL_SELL

    if action.is_copyable and quote_usd < DUST_USD:
        return None

    return SourceTrade(
        chain=Chain.SOLANA,
        wallet=wallet,
        signature=sig,
        slot=int(tx.get("slot", 0) or 0),
        action=action,
        token_mint=mint,
        quote_mint=quote_mint,
        token_amount_raw=abs(raw_delta),
        quote_amount_raw=abs(quote_delta),
        token_decimals=decimals,
        quote_decimals=quote_decimals,
        source_price=source_price,
        source_value_usd=quote_usd,
        source_confirmed_at=confirmed_at,
        detected_at=now,
        raw={"fee": meta.get("fee", 0), "slot": tx.get("slot", 0),
             "sold_fraction": str(Decimal(abs(raw_delta)) / Decimal(abs(raw_delta) +
                                  _post_balance(tx, wallet, mint)))
             if action in (Action.SELL, Action.PARTIAL_SELL) else None},
    )


def _post_balance(tx: dict, wallet: str, mint: str) -> int:
    total = 0
    for b in (tx.get("meta") or {}).get("postTokenBalances") or []:
        if b.get("owner") == wallet and b.get("mint") == mint:
            total += int(b["uiTokenAmount"]["amount"])
    return total


def parse_helius_enhanced(row: dict, wallet: str) -> SourceTrade | None:
    """Cold-path parse of a Helius Enhanced Transactions record (backfill/backtest)."""
    if row.get("transactionError"):
        return None
    events = (row.get("events") or {}).get("swap")
    if not events:
        return None

    sig = row.get("signature")
    ts = row.get("timestamp")
    confirmed_at = datetime.fromtimestamp(ts, tz=UTC) if ts else datetime.now(UTC)

    native_in = int((events.get("nativeInput") or {}).get("amount", 0) or 0)
    native_out = int((events.get("nativeOutput") or {}).get("amount", 0) or 0)
    token_in = events.get("tokenInputs") or []
    token_out = events.get("tokenOutputs") or []

    if native_in and token_out:
        leg = token_out[0]
        action, quote_raw = Action.BUY, native_in
    elif native_out and token_in:
        leg = token_in[0]
        action, quote_raw = Action.SELL, native_out
    else:
        return None

    amount = leg.get("rawTokenAmount") or {}
    decimals = int(amount.get("decimals", 9))
    token_raw = int(amount.get("tokenAmount", 0) or 0)
    if not token_raw:
        return None

    token_ui = raw_to_ui(abs(token_raw), decimals)
    quote_ui = lamports_to_sol(abs(quote_raw))

    return SourceTrade(
        wallet=wallet,
        signature=sig,
        slot=int(row.get("slot", 0) or 0),
        action=action,
        token_mint=leg.get("mint", ""),
        quote_mint=SOL_MINT,
        token_amount_raw=abs(token_raw),
        quote_amount_raw=abs(quote_raw),
        token_decimals=decimals,
        quote_decimals=9,
        source_price=(quote_ui / token_ui) if token_ui else Decimal(0),
        source_confirmed_at=confirmed_at,
        detected_at=confirmed_at,
        raw={"source": "helius_enhanced", "fee": row.get("fee", 0)},
    )
