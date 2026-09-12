"""Runtime settings: validation happens before anything is stored."""
from __future__ import annotations

import pytest

from asm import runtime_config as rc
from asm.config import settings


class TestWalletAddresses:
    """The two wallets are set from the dashboard, so the dashboard has to validate
    them. An invalid address otherwise surfaces as a failed transaction with real
    money already committed."""

    GOOD = "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o"
    OTHER = "2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f"

    def test_accepts_a_real_address(self):
        f = rc.BY_KEY["trading_wallet_pubkey"]
        assert rc.coerce(f, self.GOOD) == self.GOOD

    def test_strips_surrounding_whitespace(self):
        f = rc.BY_KEY["trading_wallet_pubkey"]
        assert rc.coerce(f, f"  {self.GOOD}\n") == self.GOOD

    def test_empty_is_allowed(self):
        """Clearing it is legitimate; the shadow -> live gate is what blocks."""
        f = rc.BY_KEY["trading_wallet_pubkey"]
        assert rc.coerce(f, "") == ""

    @pytest.mark.parametrize("bad", ["0" * 40, "O" * 40, "I" * 40, "l" * 40])
    def test_rejects_the_confusable_characters(self, bad):
        """base58 omits 0, O, I and l precisely because people transcribe them wrong."""
        f = rc.BY_KEY["trading_wallet_pubkey"]
        with pytest.raises(rc.ConfigError, match="not a Solana address"):
            rc.coerce(f, bad)

    def test_rejects_an_ethereum_address(self):
        f = rc.BY_KEY["treasury_wallet_pubkey"]
        with pytest.raises(rc.ConfigError):
            rc.coerce(f, "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0")

    @pytest.mark.parametrize("bad", ["abc", "A" * 31, "A" * 45])
    def test_rejects_wrong_length(self, bad):
        f = rc.BY_KEY["trading_wallet_pubkey"]
        with pytest.raises(rc.ConfigError, match="32-44 characters"):
            rc.coerce(f, bad)

    async def test_treasury_may_not_equal_the_trading_wallet(self, redis):
        """Harvesting into the trading wallet moves profit from one pocket to the
        same pocket - it would read as banked while still fully at risk."""
        with pytest.raises(rc.ConfigError, match="different wallet"):
            await rc.apply({"trading_wallet_pubkey": self.GOOD,
                            "treasury_wallet_pubkey": self.GOOD})

    async def test_two_different_wallets_are_accepted(self, redis):
        out = await rc.apply({"trading_wallet_pubkey": self.GOOD,
                              "treasury_wallet_pubkey": self.OTHER})
        assert out["trading_wallet_pubkey"] == self.GOOD
        assert settings.treasury_wallet_pubkey == self.OTHER

    async def test_reset_keeps_the_wallets(self, redis):
        """'Reset to defaults' means the strategy numbers, not your addresses."""
        await rc.apply({"trading_wallet_pubkey": self.GOOD,
                        "treasury_wallet_pubkey": self.OTHER})
        await rc.reset()
        assert settings.trading_wallet_pubkey == self.GOOD
        assert settings.treasury_wallet_pubkey == self.OTHER
