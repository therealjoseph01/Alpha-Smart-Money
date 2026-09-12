"""Central configuration. Every tunable in the PRD lives here or in strategy_configs."""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(StrEnum):
    """PRD 8 - operating modes."""

    BACKTEST = "backtest"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"


class RiskPreset(StrEnum):
    """PRD 64."""

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class CopyabilityConfig(BaseSettings):
    """PRD 17 - the gates that decide whether a detected trade is still worth taking."""

    model_config = SettingsConfigDict(env_prefix="COPY_", env_file=".env", extra="ignore")

    # 17.1 price chase protection: (max_move_pct, size_multiplier)
    price_move_normal_pct: Decimal = Decimal("1.0")
    price_move_acceptable_pct: Decimal = Decimal("3.0")
    price_move_reduced_pct: Decimal = Decimal("5.0")
    price_move_caution_pct: Decimal = Decimal("8.0")
    price_move_reduced_mult: Decimal = Decimal("0.5")
    price_move_caution_mult: Decimal = Decimal("0.25")

    # 17.2 latency
    max_total_copy_latency_ms: int = 30_000
    max_detection_age_ms: int = 15_000

    # 17.3 liquidity
    min_liquidity_usd: Decimal = Decimal("25000")
    max_position_pct_of_liquidity: Decimal = Decimal("1.0")

    # 17.4 slippage
    max_expected_slippage_pct: Decimal = Decimal("3.0")
    slippage_safety_margin_pct: Decimal = Decimal("1.0")
    execution_slippage_bps: int = 300

    # 17.5 market cap
    min_market_cap_usd: Decimal = Decimal("50000")
    max_market_cap_usd: Decimal = Decimal("500000000")

    # 17.6 token age
    min_token_age_seconds: int = 300
    new_token_age_seconds: int = 3600
    new_token_size_mult: Decimal = Decimal("0.5")

    # 17.7 concentration
    max_token_exposure_pct: Decimal = Decimal("5.0")
    max_trader_exposure_pct: Decimal = Decimal("15.0")
    max_open_positions: int = 20

    # 18 token safety
    max_token_risk_score: int = 40
    min_trader_score: Decimal = Decimal("60")
    min_copyability_score: Decimal = Decimal("55")
    min_trader_confidence: Decimal = Decimal("50")


class RiskConfig(BaseSettings):
    """PRD 22-25 - portfolio risk engine."""

    model_config = SettingsConfigDict(env_prefix="RISK_", env_file=".env", extra="ignore")

    preset: RiskPreset = RiskPreset.BALANCED

    base_position_pct: Decimal = Decimal("0.75")
    min_position_usd: Decimal = Decimal("10")
    max_position_pct: Decimal = Decimal("2.0")

    max_daily_loss_pct: Decimal = Decimal("5.0")
    max_drawdown_pct: Decimal = Decimal("20.0")
    max_portfolio_deployed_pct: Decimal = Decimal("60.0")
    max_trader_risk_budget_pct: Decimal = Decimal("15.0")
    max_concurrent_pending: int = 5

    # 30 harvesting
    harvest_threshold_pct: Decimal = Decimal("25.0")
    harvest_take_pct: Decimal = Decimal("40.0")
    min_active_capital_usd: Decimal = Decimal("500")
    sol_gas_reserve: Decimal = Decimal("0.25")

    # 28 exits
    stop_loss_pct: Decimal = Decimal("-25.0")
    take_profit_ladder: list[tuple[Decimal, Decimal]] = Field(
        default_factory=lambda: [
            (Decimal("50"), Decimal("25")),
            (Decimal("100"), Decimal("25")),
            (Decimal("300"), Decimal("25")),
        ]
    )
    trailing_stop_pct: Decimal = Decimal("20.0")
    trailing_arm_pct: Decimal = Decimal("40.0")
    max_hold_seconds: int = 86_400


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: str = "dev"
    mode: Mode = Mode.PAPER
    log_level: str = "INFO"
    log_json: bool = False

    database_url: str = "postgresql+asyncpg://asm:asm@localhost:5432/asm"
    redis_url: str = "redis://localhost:6379/0"

    # Helius (PRD 10)
    helius_api_key: str = ""
    helius_rpc_url: str = ""
    helius_ws_url: str = ""
    helius_sender_url: str = "https://sender.helius-rpc.com/fast"
    # Free tier is ~10 rps. Going over earns 429s, which cost a retry each - so the
    # cheapest setting is the one that never gets rate limited.
    helius_max_rps: int = 8
    # Pages of 100 transactions per wallet during backfill. Each page is one credit.
    backfill_pages: int = 3
    # Your Helius plan's monthly credit allowance, used to project 24/7 sustainability.
    monthly_credit_budget: int = 1_000_000
    rpc_fallback_url: str = "https://api.mainnet-beta.solana.com"

    # Jupiter (PRD 26)
    jupiter_base_url: str = "https://lite-api.jup.ag"
    jupiter_api_key: str = ""

    # GMGN Agent API (PRD 9/11) - candidate discovery and cheap pre-screening.
    # Cold path only: it suggests who to LOOK at, never who to copy.
    gmgn_api_key: str = ""
    gmgn_enabled: bool = True
    gmgn_plan_weight: int = 5          # Free = 5, Plus = 20, Pro = 50
    gmgn_discovery_limit: int = 100    # trades pulled per feed per run
    gmgn_auto_add: bool = True         # add screened wallets automatically

    # Roster sizing (PRD 11/15). The goal is not "collect wallets" - it is to hold a
    # fixed number of genuinely copyable ones and keep replacing the weakest.
    target_roster_size: int = 15
    # Scoring a candidate costs ~300 Helius credits, so the daily budget runs at two
    # speeds: fast while the roster is still being filled, slow once it is full and we
    # are only replacing churn (~2-3 wallets a week).
    scoring_budget_bootstrap: int = 25
    scoring_budget_steady: int = 5
    # Do not re-evaluate a wallet we already rejected for this long.
    candidate_cooldown_days: int = 30
    # Pre-screen floors. Deliberately permissive - this only decides whether a wallet
    # is worth spending ~300 Helius credits to analyse properly.
    gmgn_min_realized_usd: Decimal = Decimal("1000")
    gmgn_min_trades: int = 20
    gmgn_min_winrate_pct: Decimal = Decimal("40")
    seed_file: str = "seeds/wallets.txt"

    # Execution
    trading_wallet_pubkey: str = ""
    treasury_wallet_pubkey: str = ""
    signer_socket: str = "/tmp/asm-signer.sock"
    priority_fee_lamports: int = 100_000
    max_priority_fee_lamports: int = 2_000_000

    # MEV protection (PRD 27). Tips are sized against the trade and capped; protection
    # that costs more than the attack it prevents is a loss, not a safeguard.
    mev_protection_enabled: bool = True
    mev_tip_bps: Decimal = Decimal("15")
    max_tip_lamports: int = 10_000_000  # 0.01 SOL - a competitive Jito tip ceiling
    assumed_sol_price_usd: Decimal = Decimal("150")

    # Portfolio bootstrap
    starting_capital_usd: Decimal = Decimal("1000")

    # Alerts (PRD 43) - all optional; an unset channel is simply not used
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    alert_webhook_url: str = ""
    alert_min_level: str = "warning"

    # AI layer (PRD 40) - advisory only, never in the decision path. Off without a key.
    anthropic_api_key: str = ""

    # Dashboard sessions (PRD 49). The browser gets an httpOnly cookie, not the token.
    session_ttl_seconds: int = 43_200      # 12 hours, sliding on use
    session_cookie_secure: bool = False    # set true when served over HTTPS

    api_token: str = "dev-token-change-me"

    copyability: CopyabilityConfig = Field(default_factory=CopyabilityConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)

    @property
    def rpc_url(self) -> str:
        if self.helius_rpc_url:
            return self.helius_rpc_url
        if self.helius_api_key:
            return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        return self.rpc_fallback_url

    @property
    def ws_url(self) -> str:
        if self.helius_ws_url:
            return self.helius_ws_url
        if self.helius_api_key:
            return f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        return "wss://api.mainnet-beta.solana.com"

    @property
    def is_live(self) -> bool:
        return self.mode is Mode.LIVE


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
