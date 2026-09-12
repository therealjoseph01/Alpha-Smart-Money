# Before you trade real money

Work down the list. Nothing here is optional, and none of it is urgent — the gates
will not let you go live early anyway.

## 1. Evidence the strategy survives contact

- [ ] **30+ closed paper trades.** Fewer than that and a good week is luck.
- [ ] **20+ shadow trades.** Shadow prices real fills at real slippage; paper does not.
- [ ] **Shadow P&L is positive.** If shadow lost money, live loses money faster.
- [ ] **A completed backtest saying VIABLE.** Settings → Run a job now → backtest.
- [ ] **Shadow return is within ~30% of paper return.** A big gap means latency and
      slippage are eating the edge, and that gap only widens with real size.

The `Go live` button enforces the first four. The fifth is your judgement — look at
the Research tab.

## 2. Know what you are copying

- [ ] **At least 3 copyable wallets.** One wallet is a bet on one person.
- [ ] **Check hold times in the Research tab.** Under ~10 minutes, the edge is speed
      and you cannot copy speed. Those wallets should have been filtered out; if
      they are still there, remove them.
- [ ] **Read the rejection reasons** in Decisions. If 90% are rejected for the same
      reason, you are not running the strategy you think you are.

## 3. Money

- [ ] **Fund with an amount you would shrug at losing.** The first live week is when
      you find out whether copyable return matches shadow return. Learn it for $50.
- [ ] **`starting_capital_usd` in Settings matches what is actually in the wallet.**
      Every percentage limit is measured against this number. Set it to 10,000 with
      $500 in the wallet and every position is 20x the size you intended.
- [ ] **Keep 0.25 SOL for gas.** Positions cannot be exited without it.

## 4. Wallets

- [ ] `TRADING_WALLET_PUBKEY` set in Dokploy's Environment tab.
- [ ] `TREASURY_WALLET_PUBKEY` set — a **different** wallet.
- [ ] **The trading system does not hold the treasury's private key.** This is the
      whole point: harvested profit lands somewhere the bot cannot spend from, even
      if everything else goes wrong.

## 5. Signing

- [ ] **Run the `asm-signer` container.** It holds the key and enforces its own
      per-transaction ceiling, so a bug in the trading code cannot exceed it.
- [ ] If instead you put `SOLANA_KEYPAIR` in the environment, understand the trade:
      the trading process signs its own orders, with nothing in between. Acceptable
      at $50. Not at $5,000.

## 6. Access

- [ ] **`API_TOKEN` is 32+ random characters.** One password, no roles — anyone with
      it can go live, change risk limits, and trigger the emergency exit.
- [ ] **You know where the emergency stop is.** Top bar, red. It liquidates
      everything at market.

## 7. The first live week

- [ ] **Check it daily.** Not because it needs you — because this is when you learn
      whether the model was right.
- [ ] **Compare live fills against shadow's predicted fills.** That difference is
      the real cost of copying, and it is the number the whole system exists to find.
- [ ] **Watch the credit burn** in Research. Running out mid-month means detection
      stops while positions are open.

## If something looks wrong

**Pause** stops new positions and keeps managing open ones. **Stop** halts trading.
**Emergency stop** liquidates everything now. Stepping back to shadow is free and
has no gates — use it. Nothing is lost by spending another week in shadow.
