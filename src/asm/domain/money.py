"""All amounts are Decimal. Token amounts are raw integer units; never floats."""
from __future__ import annotations

from decimal import Decimal, getcontext

getcontext().prec = 50

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS_PER_SOL = 1_000_000_000
ZERO = Decimal(0)


def raw_to_ui(raw: int | str | Decimal, decimals: int) -> Decimal:
    return Decimal(int(raw)) / (Decimal(10) ** decimals)


def ui_to_raw(ui: Decimal | str | float, decimals: int) -> int:
    return int((Decimal(str(ui)) * (Decimal(10) ** decimals)).to_integral_value())


def lamports_to_sol(lamports: int) -> Decimal:
    return Decimal(lamports) / Decimal(LAMPORTS_PER_SOL)


def sol_to_lamports(sol: Decimal) -> int:
    return int((Decimal(str(sol)) * Decimal(LAMPORTS_PER_SOL)).to_integral_value())


def pct_change(old: Decimal, new: Decimal) -> Decimal:
    """Percent change, guarded against a zero base."""
    if old is None or new is None or old == 0:
        return ZERO
    return (new - old) / old * Decimal(100)


def bps(value: Decimal) -> Decimal:
    return value * Decimal(100)


def safe_div(a: Decimal, b: Decimal, default: Decimal = ZERO) -> Decimal:
    return default if not b else a / b


def clamp(v: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(hi, v))
