# PRD --- Adaptive Smart-Money Copy Trading System

**Status:** Product / Engineering Specification\
**Version:** 1.0\
**Primary Network:** Solana\
**Primary Intelligence/Data Source:** GMGN\
**Product Type:** Automated on-chain trader intelligence, copy-trading,
risk management, and profit-harvesting system\
**Document scope:** Full product vision, not an MVP

------------------------------------------------------------------------

## 1. Executive Summary

The product is an automated Solana trading system that discovers,
evaluates, follows, and continuously re-evaluates high-performing
on-chain traders. It uses GMGN and direct blockchain/market data to
identify traders with strong historical performance, detects their
trades, determines whether those trades are still economically copyable,
sizes positions according to risk, executes qualifying trades, manages
exits, harvests profits, and feeds actual copy performance back into the
trader-ranking system.

The system must **not** be a blind wallet copier.

Its core thesis is:

> A trader being profitable does not mean that trader is profitable to
> copy.

The platform therefore optimizes for **copyable alpha**: the return that
*our own system* could realistically capture after detection latency,
price movement, liquidity, slippage, fees, failed transactions, position
sizing, and exit latency.

The complete decision pipeline is:

**GMGN + Solana Data → Candidate Discovery → Wallet Intelligence →
Trader Scoring → Trade Detection → Copyability Analysis → Risk Engine →
Position Sizing → Execution → Exit Management → Profit Harvesting →
Performance Attribution → Dynamic Re-scoring**

A second core principle is capital preservation. The system must support
configurable **profit harvesting**: after portfolio gains cross defined
thresholds, a portion of realized profits is automatically moved out of
the active trading balance while the remainder stays available to
compound and continue following qualified traders.

The product is intended to become a self-improving trading system whose
decisions are increasingly based on observed live copyability rather
than leaderboard hype or raw wallet P&L.

This system involves speculative crypto trading and cannot guarantee
profit. Every live-trading feature must be built around explicit risk
limits, auditable rules, secure key handling, and emergency controls.

------------------------------------------------------------------------

# 2. Problem

Public blockchain data makes successful traders visible. Platforms such
as GMGN can surface profitable wallets, smart-money activity, token
trades, P&L, holdings, and related market intelligence.

However, simply finding a wallet that has made money is insufficient.

A wallet can appear highly profitable while being unsuitable for copying
because:

-   its returns came from one extraordinary trade;
-   its current leaderboard position reflects only a short period;
-   it enters tokens before public participants can reasonably react;
-   it trades illiquid assets;
-   its position sizes are impossible to replicate proportionally;
-   its strategy requires execution speeds the follower cannot achieve;
-   the token price moves materially between its fill and detection;
-   it is an insider or deployer-related wallet;
-   it receives tokens rather than purchasing them normally;
-   it uses multiple linked wallets;
-   it has strong gross P&L but poor risk-adjusted performance;
-   it scalps too quickly for delayed followers;
-   it intentionally creates visible transactions that attract copy
    traders;
-   it sells into followers;
-   its historical win rate is inflated by tiny wins and occasional
    catastrophic losses;
-   fees, priority fees and slippage eliminate the apparent edge;
-   a follower's exit arrives significantly later than the source
    wallet's exit.

The actual problem is therefore:

> Given a large universe of on-chain traders, determine which traders
> currently possess repeatable and realistically copyable alpha,
> determine which individual trades should be followed, execute those
> trades safely, and protect accumulated profits.

------------------------------------------------------------------------

# 3. Product Vision

Build an autonomous trading intelligence system capable of answering
five questions continuously:

1.  **Who should we follow?**
2.  **Should we copy this particular trade?**
3.  **How much should we risk?**
4.  **When should we exit?**
5.  **How much profit should remain at risk versus be harvested?**

The system should eventually operate continuously with minimal human
intervention while retaining complete human control over risk parameters
and live execution.

The long-term objective is not to maximize the number of copied trades.

The objective is:

> Maximize risk-adjusted, realistically executable net returns while
> minimizing catastrophic loss and preserving accumulated profits.

------------------------------------------------------------------------

# 4. Non-Goals

The system must not:

-   promise fixed daily returns;
-   assume leaderboard returns can be replicated;
-   blindly copy every transaction;
-   optimize exclusively for win rate;
-   allocate the entire portfolio to one trader or token;
-   treat AI-generated predictions as factual market knowledge;
-   depend on a single external API without graceful degradation;
-   expose private keys to an LLM;
-   permit autonomous changes to hard portfolio risk limits;
-   assume 42% daily returns or any other extraordinary return is
    sustainable;
-   martingale losing positions by default;
-   automatically increase risk because recent trades were profitable;
-   conceal losses, fees, slippage, or failed transactions from
    performance metrics.

------------------------------------------------------------------------

# 5. Product Principles

## 5.1 Copyability over headline profitability

A trader making 42% does not imply the follower can make 42%.

The platform measures:

`Source Return != Copyable Return != Realized System Return`

All ranking and capital allocation decisions should increasingly weight
the second and third values.

## 5.2 Survival before optimization

The system must survive bad trades, rugs, latency spikes, API failures,
RPC failures, extreme volatility, and prolonged drawdowns.

## 5.3 Capital is earned before it is scaled

New traders receive small allocations. Capital allocation increases only
after sufficient observed evidence.

## 5.4 Profit is not protected until removed from active risk

Realized gains inside the trading wallet remain exposed. Profit
harvesting therefore forms part of the risk system.

## 5.5 Every autonomous action must be explainable

For each copied or rejected trade, the system must retain the inputs and
reason codes that produced the decision.

## 5.6 Deterministic controls dominate AI

AI can analyze and summarize. Hard risk constraints must remain
deterministic.

------------------------------------------------------------------------

# 6. Target Network

## 6.1 Primary Chain: Solana

The initial and primary network is Solana.

Reasons include:

-   high transaction throughput;
-   relatively low transaction costs;
-   active on-chain trading ecosystem;
-   substantial token-launch and memecoin activity;
-   strong relevance to GMGN wallet intelligence;
-   fast-moving markets where copyability analysis creates meaningful
    differentiation.

The architecture should remain chain-extensible, but cross-chain support
must not compromise Solana execution quality.

------------------------------------------------------------------------

# 7. System Architecture

``` text
                       ┌─────────────────────────────┐
                       │          GMGN API           │
                       │ Leaderboards / Wallets /    │
                       │ Tokens / Trades / Signals   │
                       └──────────────┬──────────────┘
                                      │
                       ┌──────────────▼──────────────┐
                       │   Candidate Discovery       │
                       │   + Data Normalization      │
                       └──────────────┬──────────────┘
                                      │
                       ┌──────────────▼──────────────┐
                       │ Wallet Intelligence Engine  │
                       │ Performance / Behavior /    │
                       │ Consistency / Risk          │
                       └──────────────┬──────────────┘
                                      │
                       ┌──────────────▼──────────────┐
                       │ Trader Scoring & Ranking    │
                       └──────────────┬──────────────┘
                                      │
                   ┌──────────────────▼──────────────────┐
                   │ Real-Time Trade Detection           │
                   │ GMGN + Solana RPC/WebSocket         │
                   └──────────────────┬──────────────────┘
                                      │
                       ┌──────────────▼──────────────┐
                       │ Copyability Engine          │
                       │ Latency / Price / Liquidity │
                       │ Slippage / Token Risk       │
                       └──────────────┬──────────────┘
                                      │
                       ┌──────────────▼──────────────┐
                       │ Portfolio Risk Engine       │
                       └──────────────┬──────────────┘
                                      │
                       ┌──────────────▼──────────────┐
                       │ Position Sizing Engine      │
                       └──────────────┬──────────────┘
                                      │
                         ┌────────────▼────────────┐
                         │ Execution Engine        │
                         │ Paper / Shadow / Live   │
                         └────────────┬────────────┘
                                      │
                         ┌────────────▼────────────┐
                         │ Position + Exit Engine  │
                         └────────────┬────────────┘
                                      │
                    ┌─────────────────▼─────────────────┐
                    │ Profit Harvesting / Treasury     │
                    └─────────────────┬─────────────────┘
                                      │
                         ┌────────────▼────────────┐
                         │ Attribution & Learning  │
                         └────────────┬────────────┘
                                      │
                         ┌────────────▼────────────┐
                         │ Trader Re-Scoring       │
                         └─────────────────────────┘
```

------------------------------------------------------------------------

# 8. Operating Modes

The product must support four distinct execution modes.

## 8.1 Historical Backtest

Replay historical wallet trades using historical market conditions.

Purpose:

-   evaluate trader selection;
-   test risk rules;
-   compare source P&L versus copyable P&L;
-   evaluate latency assumptions;
-   tune thresholds.

## 8.2 Paper Trading

Process real-time signals but execute no blockchain transaction.

The paper engine must simulate:

-   detection time;
-   decision time;
-   expected submission time;
-   realistic entry price;
-   slippage;
-   fees;
-   priority fees;
-   failed execution probability where measurable;
-   exit timing.

## 8.3 Shadow Live

The system constructs the exact transaction it *would* send and records
the outcome without signing/broadcasting.

This provides a bridge between paper assumptions and live execution.

## 8.4 Live Trading

Transactions are signed and broadcast.

Live mode requires:

-   explicit enablement;
-   configured trading wallet;
-   risk configuration;
-   emergency kill switch;
-   treasury configuration;
-   minimum SOL reserve;
-   health checks;
-   maximum loss controls.

------------------------------------------------------------------------

# 9. GMGN Integration

The GMGN integration layer is responsible for acquiring all permitted
data necessary for candidate discovery, wallet analysis, token analysis,
and trade monitoring.

Exact endpoint availability and rate limits must be validated against
the current official GMGN API before implementation.

The adapter should support, where available:

-   wallet leaderboard data;
-   wallet profitability;
-   wallet trade history;
-   token trades;
-   token metadata;
-   token price;
-   token liquidity;
-   token holders;
-   top traders;
-   smart-money signals;
-   wallet holdings;
-   realized/unrealized P&L;
-   trading activity;
-   token security information;
-   market/candlestick data;
-   execution-related capabilities if officially supported.

All external GMGN responses must be normalized into internal domain
objects so business logic is not tightly coupled to GMGN response
schemas.

------------------------------------------------------------------------

# 10. Direct Solana Integration

GMGN should not be the only observation mechanism.

The system should use direct Solana infrastructure where beneficial for:

-   wallet transaction detection;
-   signature monitoring;
-   confirmation status;
-   balances;
-   token accounts;
-   transaction parsing;
-   execution;
-   priority fees;
-   block/slot timing;
-   transaction simulation.

Potential transport mechanisms:

-   RPC;
-   WebSocket subscriptions;
-   transaction streams;
-   specialized Solana data providers where required.

The system must timestamp every stage to quantify latency.

------------------------------------------------------------------------

# 11. Candidate Trader Discovery

Candidate discovery continuously identifies wallets worthy of deeper
analysis.

Sources may include:

-   GMGN leaderboards;
-   smart-money lists;
-   high-profit traders;
-   high-win-rate traders;
-   token-specific top traders;
-   wallets repeatedly early to successful tokens;
-   wallets discovered through graph relationships;
-   manually supplied wallets.

A leaderboard rank alone must never automatically authorize live
copying.

Candidate wallets enter an evaluation pipeline.

------------------------------------------------------------------------

# 12. Trader Intelligence Profile

Every tracked trader must maintain a persistent profile.

Example:

``` text
wallet_address
first_seen_at
last_seen_at
source
status
score
confidence
copyability_score

lifetime_trade_count
30d_trade_count
60d_trade_count
90d_trade_count

win_rate_7d
win_rate_30d
win_rate_90d

realized_pnl_7d
realized_pnl_30d
realized_pnl_90d

roi_7d
roi_30d
roi_90d

average_win
average_loss
profit_factor
expectancy

max_drawdown
volatility
largest_loss
largest_win

median_holding_period
average_holding_period
trades_per_day

median_entry_liquidity
median_position_size
token_concentration

copy_simulated_roi
copy_live_roi
copy_success_rate
median_copy_latency
median_copy_slippage

suspicion_flags
risk_flags
```

------------------------------------------------------------------------

# 13. Trader Scoring Engine

The scoring engine determines who deserves observation and capital.

A conceptual score:

``` text
TraderScore =
    PerformanceScore
  + ConsistencyScore
  + CopyabilityScore
  + RiskAdjustedScore
  + LongevityScore
  + LiquidityCompatibilityScore
  + LiveReplicationScore
  - DrawdownPenalty
  - ConcentrationPenalty
  - SuspicionPenalty
  - LatencyPenalty
```

Weights must be configurable.

## 13.1 Performance Score

Evaluate:

-   realized P&L;
-   ROI;
-   profit factor;
-   expectancy;
-   median trade return;
-   average trade return.

## 13.2 Consistency Score

Reward traders whose performance persists across time windows.

Example:

A wallet profitable across 7, 30, 60 and 90 days should generally rank
above a wallet whose entire return occurred yesterday.

Measure:

-   profitable weeks;
-   profitable months;
-   rolling expectancy;
-   dispersion;
-   dependency on largest trade.

## 13.3 Longevity Score

Reward evidence accumulated across sufficient trades and time.

A wallet with 500 trades over six months provides stronger evidence than
a wallet with four trades in one day.

## 13.4 Risk-Adjusted Score

Consider:

-   maximum drawdown;
-   downside deviation;
-   loss streaks;
-   average loss;
-   tail losses;
-   return relative to drawdown.

## 13.5 Copyability Score

This is one of the product's most important proprietary metrics.

It estimates:

> If our system had detected and followed this trader using realistic
> latency and execution conditions, how much of the trader's edge would
> we have captured?

Possible inputs:

-   source entry price;
-   price at +1 sec;
-   +2 sec;
-   +3 sec;
-   +5 sec;
-   +10 sec;
-   +30 sec;
-   available liquidity;
-   estimated follower price impact;
-   source holding duration;
-   source exit behavior;
-   follower exit delay;
-   fees;
-   slippage;
-   transaction success.

## 13.6 Live Replication Score

Once the system has shadow/paper/live observations, actual system
performance should outweigh theoretical estimates.

Example:

``` text
ReplicationRatio =
SystemNetReturnFromCopiedSignals /
SourceReturnForSameSignals
```

This ratio must be interpreted carefully when source and follower
position sizes differ.

------------------------------------------------------------------------

# 14. Trader Confidence

Score and confidence are separate.

A trader may have:

`Score: 92/100`

but:

`Confidence: Low`

because only seven trades were observed.

Confidence should increase with:

-   number of trades;
-   duration observed;
-   market regimes experienced;
-   live/shadow observations;
-   consistency of data.

Capital allocation must be constrained by confidence.

------------------------------------------------------------------------

# 15. Trader Lifecycle

Statuses:

``` text
DISCOVERED
EVALUATING
WATCHLIST
PAPER_COPY
SHADOW_COPY
LIVE_PROBATION
LIVE_ACTIVE
DEGRADED
PAUSED
BLACKLISTED
```

Promotion requires sufficient evidence.

Demotion happens automatically when performance deteriorates.

------------------------------------------------------------------------

# 16. Trade Detection

The system monitors qualified wallets continuously.

For every detected transaction:

1.  record source;
2.  record detection timestamp;
3.  parse transaction;
4.  classify action;
5.  identify token;
6.  determine source execution price;
7.  estimate source position size;
8.  fetch current market state;
9.  pass signal to copyability engine.

Trade classifications:

-   buy;
-   sell;
-   partial sell;
-   transfer;
-   LP interaction;
-   token receipt;
-   unknown.

Only legitimate qualifying trading activity should enter the copy
pipeline.

------------------------------------------------------------------------

# 17. Copyability Engine

The system must **not** copy immediately merely because a followed
wallet bought.

Each signal becomes a `TradeCandidate`.

The engine asks:

> Is this trade still worth taking *now*?

## 17.1 Price Chase Protection

Calculate:

``` text
PriceMoveSinceSource =
(CurrentPrice - SourceEntryPrice) / SourceEntryPrice
```

Reject or reduce allocation if the token has already moved beyond
configured thresholds.

Example policy:

``` text
<= 1%       normal
1–3%        acceptable
3–5%        reduced size
5–8%        high caution
> 8%        reject
```

Actual thresholds must be strategy-configurable and empirically tested.

## 17.2 Latency Analysis

Track:

``` text
source_confirmed_at
detected_at
decision_completed_at
transaction_built_at
signed_at
submitted_at
confirmed_at
```

Derive:

-   observation latency;
-   analysis latency;
-   execution latency;
-   total copy latency.

The system should learn each trader's **latency tolerance**.

A trader whose edge disappears after two seconds is fundamentally
different from one whose positions are held for hours.

## 17.3 Liquidity Gate

Reject trades when liquidity is insufficient for the intended size at
acceptable price impact.

## 17.4 Slippage Gate

Estimate expected slippage before execution.

Reject when:

``` text
ExpectedAlpha <= EstimatedFees + Slippage + SafetyMargin
```

## 17.5 Market-Cap Constraints

Configurable:

``` text
min_market_cap
max_market_cap
```

## 17.6 Token Age

Allow policies such as:

-   reject tokens younger than N seconds/minutes;
-   allow new launches only for specially qualified traders;
-   reduce allocation for very new tokens.

## 17.7 Concentration Gate

Do not add exposure if the portfolio already has excessive exposure to:

-   the token;
-   correlated tokens;
-   one trader;
-   one strategy;
-   one launch ecosystem.

------------------------------------------------------------------------

# 18. Token Safety Engine

Before entry, evaluate available indicators such as:

-   mint authority;
-   freeze authority;
-   holder concentration;
-   liquidity;
-   liquidity behavior;
-   suspicious transfers;
-   deployer activity;
-   token age;
-   supply concentration;
-   known scam indicators;
-   unusual wallet clusters;
-   ability to sell where test/simulation mechanisms permit;
-   source wallet relationship to deployer;
-   top-holder behavior.

Output:

``` text
TokenRiskScore: 0–100
RiskLevel: LOW | MEDIUM | HIGH | CRITICAL
ReasonCodes: [...]
```

Critical-risk tokens must be rejected automatically unless a
deliberately configured specialized strategy says otherwise.

------------------------------------------------------------------------

# 19. Insider / Manipulation Detection

The platform should attempt to detect wallets whose public profitability
is not safely reproducible.

Signals may include:

-   wallet buys immediately after token creation repeatedly;
-   funding relationships with deployers;
-   tokens transferred into wallet before trading;
-   unusually favorable fills;
-   repeated participation in coordinated wallet clusters;
-   wallet sells immediately after followers arrive;
-   returns dominated by inaccessible entries;
-   repeated tokens with related deployers;
-   suspicious funding graph;
-   source wallet distributes funds across fresh wallets.

Suspicion should lower score or blacklist the wallet.

------------------------------------------------------------------------

# 20. Multi-Trader Confirmation

A trade can receive additional confidence when multiple independently
qualified traders buy the same token within a configured window.

Example:

``` text
Trader A score: 91
Trader B score: 87
Trader C score: 84

All buy TOKEN within 90 seconds.
```

The system may raise signal confidence, but must ensure wallets are not
linked or simply copying each other.

Multi-trader confirmation must never override hard risk limits.

------------------------------------------------------------------------

# 21. Position Sizing

The platform supports multiple sizing models.

## 21.1 Fixed Size

Example:

`$10 per qualified trade`

Useful during testing/probation.

## 21.2 Portfolio Percentage

Example:

`0.5% of active capital per trade`

## 21.3 Source-Proportional

If the source trader appears to allocate 4% of their portfolio, the
follower may allocate a fraction:

``` text
FollowerAllocation =
SourcePortfolioPercentage × CopyMultiplier
```

## 21.4 Confidence-Weighted

``` text
PositionSize =
BaseRisk
× TraderConfidence
× TradeConfidence
× CopyabilityFactor
× TokenRiskFactor
```

## 21.5 Kelly-Inspired Sizing

A conservative fractional Kelly model may be evaluated after enough
statistically meaningful data exists.

Full Kelly should not be used by default in highly volatile markets.

------------------------------------------------------------------------

# 22. Portfolio Risk Engine

Hard limits include:

``` text
max_position_percent
max_token_exposure
max_trader_exposure
max_open_positions
max_daily_loss
max_weekly_loss
max_drawdown
max_slippage
max_price_chase
min_liquidity
min_wallet_confidence
minimum_SOL_reserve
```

These limits cannot be overridden by AI.

------------------------------------------------------------------------

# 23. Daily Loss Circuit Breaker

Example:

``` text
Starting active capital: $1,000
Daily loss limit: 5%
```

At approximately `$950` net equity:

-   reject new entries;
-   optionally manage/close existing positions according to policy;
-   mark trading session halted;
-   notify operator;
-   require next-session reset or manual review.

------------------------------------------------------------------------

# 24. Portfolio Drawdown Circuit Breaker

If peak equity was `$1,500` and configured max drawdown is `15%`, risk
reduction occurs as equity approaches the threshold.

Possible stages:

``` text
5% drawdown   → reduce new sizes 20%
10% drawdown  → reduce new sizes 50%
15% drawdown  → halt new entries
20% drawdown  → emergency policy
```

Thresholds are configurable.

------------------------------------------------------------------------

# 25. Trader-Specific Risk Budget

No single trader should control the portfolio.

Example:

``` text
Trader A max allocation: 15%
Trader B max allocation: 10%
Trader C max allocation: 8%
```

Allocation is based on score, confidence, correlation, and observed
replication performance.

------------------------------------------------------------------------

# 26. Entry Execution Engine

The execution engine must:

1.  receive approved trade;
2.  fetch current route/quote;
3.  verify slippage;
4.  verify balance;
5.  verify portfolio constraints again;
6.  construct transaction;
7.  simulate where appropriate;
8.  sign securely;
9.  submit;
10. monitor confirmation;
11. retry only under safe idempotent policies;
12. record actual fill;
13. calculate execution quality.

Execution records must include:

``` text
expected_price
actual_price
source_price
expected_slippage
actual_slippage
network_fee
priority_fee
route
submission_latency
confirmation_latency
failure_reason
```

------------------------------------------------------------------------

# 27. MEV and Execution Protection

Where supported and appropriate, execution should consider:

-   protected routing;
-   priority fee optimization;
-   transaction simulation;
-   private transaction pathways;
-   sandwich-risk mitigation;
-   maximum price impact.

Execution infrastructure should optimize for **net realized return**,
not simply fastest submission.

------------------------------------------------------------------------

# 28. Exit Engine

Blindly copying source exits is not always optimal.

The system supports multiple exit strategies.

## 28.1 Mirror Exit

When source wallet sells X% of its position, follower sells
corresponding percentage.

## 28.2 Independent Stop Loss

Example:

`-10% → close`

## 28.3 Take Profit

Example:

``` text
+20% → sell 25%
+50% → sell 25%
+100% → sell 25%
remainder → trailing strategy
```

## 28.4 Trailing Stop

After meaningful appreciation, protect gains.

## 28.5 Time-Based Exit

Exit if expected strategy duration is exceeded.

## 28.6 Trader-Degradation Exit

If the source wallet begins abnormal behavior or its trust state changes
while positions are open, the system may reduce/close exposure.

## 28.7 Liquidity-Deterioration Exit

Reduce risk when liquidity rapidly disappears.

------------------------------------------------------------------------

# 29. Partial Profit Taking

The system should support removing principal from a winning position.

Example:

``` text
Entry: $100
Position reaches: $200
```

A policy could sell `$100` worth, recovering original capital while
leaving the remaining position exposed to further upside.

This is optional and strategy-specific.

------------------------------------------------------------------------

# 30. Portfolio Profit Harvesting

This is a core product requirement.

The system distinguishes:

``` text
Trading Capital
Realized Profit
Harvestable Profit
Reserve/Treasury Capital
```

The goal is to prevent all accumulated gains from remaining permanently
exposed to trading risk.

## 30.1 Threshold-Based Harvesting

Example configuration:

``` yaml
profit_harvesting:
  enabled: true

  thresholds:
    - portfolio_gain: 0.10
      withdraw_profit_percent: 0.25

    - portfolio_gain: 0.25
      withdraw_profit_percent: 0.35

    - portfolio_gain: 0.50
      withdraw_profit_percent: 0.50

    - portfolio_gain: 1.00
      withdraw_profit_percent: 0.60
```

Illustration only; percentages are user-configured rather than promises
or recommendations.

## 30.2 Example

Suppose:

``` text
Initial active capital: $1,000
Current equity: $1,500
Realized/eligible gain: $500
Harvest rule at +50%: 50% of eligible profit
```

Then:

``` text
Profit harvested: $250
Profit retained for compounding: $250
```

The active strategy can continue while some gains have been moved
outside the active risk pool.

## 30.3 High-Water Mark

Harvesting should use a high-water-mark model to avoid repeatedly
counting the same profit.

Fields:

``` text
initial_capital
equity_high_water_mark
last_harvest_equity
total_harvested
current_active_equity
```

## 30.4 Destination

Harvested assets may be:

-   moved to a separate treasury wallet;
-   converted to configured stable asset before transfer;
-   retained in SOL;
-   split across destinations.

No withdrawal may occur without explicit treasury configuration.

## 30.5 Minimum Active Capital

Never harvest below the configured operating balance.

Example:

``` text
minimum_active_capital = $500
```

## 30.6 Gas/SOL Reserve

Maintain enough SOL to continue execution.

------------------------------------------------------------------------

# 31. Compounding Modes

Users can configure:

## Conservative

Harvest a larger portion of gains.

## Balanced

Split gains between treasury and active capital.

## Growth

Retain most profits for compounding.

These are configuration presets, not guarantees of risk level.

------------------------------------------------------------------------

# 32. Profit Lock

A separate optional rule protects portfolio gains.

Example:

``` text
Portfolio starts at $1,000
Reaches $1,500
Profit floor = $1,300
```

If equity falls toward the floor, the system can:

-   reduce new position sizes;
-   stop new trades;
-   harvest additional realized profit;
-   close specified risk positions.

This creates a portfolio-level trailing protection mechanism.

------------------------------------------------------------------------

# 33. Capital Buckets

Portfolio capital can be separated logically:

``` text
Active Trading Capital
Reserved Trading Capital
Pending Settlement
Realized Profit
Treasury
Emergency Reserve
```

Only Active Trading Capital is available for normal new positions.

------------------------------------------------------------------------

# 34. Performance Attribution

Every dollar of P&L must be attributable.

Dimensions:

-   trader;
-   token;
-   strategy;
-   entry rule;
-   exit rule;
-   execution route;
-   time period;
-   latency bucket;
-   score bucket;
-   market-cap bucket;
-   liquidity bucket.

This allows the system to answer:

> Which traders actually make *us* money?

rather than:

> Which traders made themselves money?

------------------------------------------------------------------------

# 35. Source vs Follower Comparison

For every copied signal:

``` text
Source Entry
Our Entry
Entry Delay

Source Exit
Our Exit
Exit Delay

Source Gross Return
Our Gross Return
Our Net Return

Return Capture Ratio
```

Dashboard example:

``` text
Trader: 7x...abc

Source 30d ROI:        +84%
Simulated Copy ROI:    +31%
Live Copy ROI:         +24%
Replication Ratio:      28.6%
Median Entry Delay:      2.8s
Median Slippage:         1.4%
```

A lower-headline-return trader with a high replication ratio may deserve
more capital than a spectacular trader whose trades cannot be copied.

------------------------------------------------------------------------

# 36. Adaptive Re-Scoring

Trader scores update continuously.

Example:

``` text
Trader starts:
Score = 91
Allocation = 10%

After 50 copied trades:
System expectancy deteriorates.
Replication ratio falls.
Drawdown rises.

New score = 72
Allocation = 3%
```

Eventually:

``` text
Score < threshold → PAUSED
```

The system must stop following deteriorating traders automatically.

------------------------------------------------------------------------

# 37. Strategy Decay Detection

Detect whether a previously profitable edge is disappearing.

Signals:

-   falling expectancy;
-   falling replication ratio;
-   rising latency sensitivity;
-   increasing slippage;
-   changed trading frequency;
-   changed token profile;
-   increased drawdown;
-   larger losses;
-   decreased source performance.

Use rolling windows and change-point-style alerts rather than lifetime
averages alone.

------------------------------------------------------------------------

# 38. Portfolio Diversification

Avoid concentration across correlated sources.

If ten wallets consistently buy the same tokens at the same time, they
may represent one underlying strategy.

The system should cluster wallets using:

-   token overlap;
-   timing overlap;
-   funding relationships;
-   behavioral similarity;
-   shared counterparties.

Capital limits should apply to correlated clusters.

------------------------------------------------------------------------

# 39. Wallet Graph Intelligence

Long-term capability:

Construct wallet relationship graphs to identify:

-   common funders;
-   linked wallets;
-   deployer relationships;
-   wallet families;
-   coordinated trading;
-   possible insider clusters;
-   copy relationships between wallets.

This improves both fraud detection and signal independence.

------------------------------------------------------------------------

# 40. AI Layer

AI is useful, but it is not the execution authority.

## Appropriate AI Responsibilities

AI may:

-   summarize why a trader ranks highly;
-   detect behavioral patterns from structured features;
-   explain changes in trader score;
-   classify suspicious patterns for further deterministic checks;
-   produce operator summaries;
-   identify unusual behavior;
-   help compare traders;
-   generate research notes;
-   surface anomalies.

Example:

> Trader 7x...abc was downgraded because 30-day expectancy fell 43%,
> median slippage doubled, and four of its last six profitable source
> trades were no longer profitable when replayed with our observed
> 3-second latency.

## AI Must Not

-   access raw private keys;
-   sign transactions;
-   bypass portfolio limits;
-   increase hard risk limits autonomously;
-   invent token data;
-   override emergency stops;
-   transfer treasury funds outside pre-authorized rules.

------------------------------------------------------------------------

# 41. Decision Engine

The final trade decision should be deterministic and auditable.

Example:

``` text
TRADE SIGNAL
Trader: 7x...abc
Token: XYZ

Trader Score:        91
Confidence:          88
Copyability:         84
Token Risk:          22/100
Price Move:          +1.8%
Liquidity:           PASS
Expected Slippage:   0.9%
Portfolio Exposure:  PASS
Daily Loss Limit:    PASS
Correlation Limit:   PASS

Decision: COPY
Position Size: 0.75% portfolio
Reason Codes:
- HIGH_TRADER_SCORE
- HIGH_COPYABILITY
- ACCEPTABLE_PRICE_MOVE
- SUFFICIENT_LIQUIDITY
```

Rejected example:

``` text
Decision: REJECT

Reason Codes:
- PRICE_MOVED_12_PERCENT
- LOW_LIQUIDITY
- EXPECTED_SLIPPAGE_TOO_HIGH
```

------------------------------------------------------------------------

# 42. Dashboard

## 42.1 Portfolio Overview

Display:

-   active equity;
-   treasury balance;
-   initial capital;
-   total return;
-   realized P&L;
-   unrealized P&L;
-   harvested profits;
-   today's P&L;
-   7d/30d P&L;
-   peak equity;
-   current drawdown;
-   max drawdown;
-   open positions;
-   active traders;
-   current risk utilization.

## 42.2 Trader Leaderboard

Columns:

``` text
Rank
Wallet
System Score
Confidence
Source ROI
Copy ROI
Live ROI
Win Rate
Profit Factor
Max Drawdown
Replication Ratio
Median Latency
Status
Allocation
```

## 42.3 Trade Feed

Show:

``` text
timestamp
trader
token
source action
decision
reason
source price
our price
position size
status
P&L
```

Include rejected trades because rejected-signal analysis is essential.

## 42.4 Position Detail

Show:

-   source trader;
-   source transaction;
-   entry;
-   current price;
-   stop;
-   take-profit stages;
-   trailing status;
-   unrealized P&L;
-   liquidity;
-   risk score;
-   planned exit behavior.

## 42.5 Profit Harvesting

Show:

``` text
Total earned
Total harvested
Total retained
Next harvest threshold
High-water mark
Treasury destination
```

------------------------------------------------------------------------

# 43. Alerts

Alert types:

-   trade executed;
-   trade failed;
-   trader promoted;
-   trader degraded;
-   trader paused;
-   daily loss limit hit;
-   drawdown limit hit;
-   profit harvest executed;
-   RPC outage;
-   GMGN API outage;
-   abnormal slippage;
-   low SOL reserve;
-   treasury transfer failure;
-   suspicious trader behavior;
-   token risk escalation;
-   emergency stop.

Delivery channels may include:

-   dashboard;
-   push;
-   email;
-   Telegram/Discord/Slack where configured.

------------------------------------------------------------------------

# 44. Backtesting Engine

Historical backtesting must account for realistic follower conditions.

A naive backtest using source prices is unacceptable.

Inputs:

``` text
wallet trade history
historical token prices
liquidity
source timestamps
simulated detection latency
decision latency
execution latency
slippage model
fees
exit latency
```

Test latency scenarios such as:

``` text
+1 second
+2 seconds
+3 seconds
+5 seconds
+10 seconds
+30 seconds
```

Output:

``` text
source ROI
copy ROI
net copy ROI
max drawdown
profit factor
expectancy
win rate
replication ratio
trade count
worst trade
best trade
```

------------------------------------------------------------------------

# 45. Paper Trading Requirements

Paper trading must behave like live trading.

It must not pretend we received the source wallet's fill.

For each paper trade:

``` text
Detected at: 12:00:03
Source bought: 12:00:00
Source price: $0.0100
Market price at simulated execution: $0.0108
Expected slippage: 1.1%
Simulated fill: $0.01092
```

Performance must use `$0.01092`, not `$0.0100`.

This is essential to determining whether the strategy has genuine
copyable alpha.

------------------------------------------------------------------------

# 46. Experiment Framework

Every strategy rule should support controlled comparison.

Examples:

-   copy only score \>90 versus \>80;
-   maximum chase 3% versus 5%;
-   fixed sizing versus confidence sizing;
-   source exit versus independent take-profit;
-   one-trader signals versus multi-trader confirmation;
-   token age filters;
-   different slippage limits.

Metrics must be comparable over identical periods.

------------------------------------------------------------------------

# 47. Database Model

Core entities:

``` text
traders
trader_scores
trader_daily_metrics
trader_relationships

tokens
token_snapshots
token_risk_scores

source_trades
trade_candidates
trade_decisions

paper_orders
live_orders
executions
positions
position_events

portfolio_snapshots
risk_events

profit_harvest_rules
profit_harvest_events
treasury_transfers

strategy_configs
strategy_versions

system_events
alerts
audit_logs
```

------------------------------------------------------------------------

# 48. Suggested Schema

## traders

``` text
id
chain
wallet_address
source
status
first_seen_at
last_seen_at
created_at
updated_at
```

## trader_scores

``` text
id
trader_id
score
confidence
performance_score
consistency_score
copyability_score
risk_adjusted_score
longevity_score
replication_score
suspicion_penalty
calculated_at
model_version
```

## source_trades

``` text
id
trader_id
signature
token_address
side
source_amount
source_price
source_timestamp
detected_timestamp
raw_metadata
```

## trade_decisions

``` text
id
source_trade_id
decision
reason_codes
trader_score
copyability_score
token_risk_score
price_move
expected_slippage
requested_size
approved_size
decision_timestamp
strategy_version
```

## executions

``` text
id
trade_decision_id
mode
signature
expected_price
actual_price
quantity
fees
priority_fee
slippage
submitted_at
confirmed_at
status
failure_reason
```

## positions

``` text
id
token_address
source_trader_id
entry_execution_id
quantity
average_entry
realized_pnl
unrealized_pnl
status
opened_at
closed_at
```

## portfolio_snapshots

``` text
id
timestamp
active_capital
reserved_capital
treasury_value
realized_pnl
unrealized_pnl
equity
high_water_mark
drawdown
```

## profit_harvest_events

``` text
id
trigger_type
threshold
eligible_profit
harvest_percentage
harvest_amount
asset
destination
transaction_signature
status
created_at
```

------------------------------------------------------------------------

# 49. API Design

Example internal APIs:

``` text
GET  /traders
GET  /traders/:wallet
GET  /traders/:wallet/performance
GET  /traders/:wallet/copyability

GET  /trades
GET  /trades/:id

GET  /positions
GET  /positions/:id

GET  /portfolio
GET  /portfolio/performance

GET  /risk
PUT  /risk/config

GET  /harvesting
PUT  /harvesting/config

POST /system/pause
POST /system/resume
POST /system/emergency-stop

POST /backtests
GET  /backtests/:id
```

Sensitive mutation endpoints require strong authentication and
authorization.

------------------------------------------------------------------------

# 50. Event-Driven Architecture

Important internal events:

``` text
TRADER_DISCOVERED
TRADER_SCORE_UPDATED
TRADER_PROMOTED
TRADER_DEGRADED

SOURCE_TRADE_DETECTED
TRADE_CANDIDATE_CREATED
TRADE_APPROVED
TRADE_REJECTED

ORDER_SUBMITTED
ORDER_CONFIRMED
ORDER_FAILED

POSITION_OPENED
POSITION_REDUCED
POSITION_CLOSED

RISK_THRESHOLD_REACHED
TRADING_HALTED

HARVEST_THRESHOLD_REACHED
PROFIT_HARVESTED

DATA_PROVIDER_DEGRADED
```

Events allow asynchronous processing and complete audit history.

------------------------------------------------------------------------

# 51. Idempotency

Duplicate signals must never produce duplicate unintended orders.

Every source transaction should have a unique internal identity based on
fields such as:

``` text
chain
source_wallet
transaction_signature
token
action
```

Order submission requires idempotency keys.

------------------------------------------------------------------------

# 52. Concurrency

Race conditions must be handled.

Examples:

-   two followed traders buy the same token simultaneously;
-   source trader sells while our buy is pending;
-   risk limit is reached while several trades await execution;
-   harvesting and trade execution modify balances simultaneously.

Portfolio risk must be revalidated immediately before submission.

------------------------------------------------------------------------

# 53. External Dependency Resilience

For GMGN/API failures:

-   stop relying on stale signals after TTL;
-   retry with exponential backoff;
-   use cached metadata only when safe;
-   direct Solana observation may continue;
-   disable new trades when critical validation data is unavailable.

For RPC failures:

-   fail over to secondary provider;
-   never assume transaction failure solely because one RPC timed out;
-   reconcile signatures before retrying.

------------------------------------------------------------------------

# 54. Security

## Private Keys

Never:

-   store plaintext keys in application logs;
-   send private keys to AI APIs;
-   expose keys to frontend clients;
-   commit secrets to Git.

Use:

-   secure key management;
-   encrypted secrets;
-   dedicated trading wallet;
-   limited active capital;
-   separate treasury wallet.

## Wallet Separation

Recommended architecture:

``` text
Funding Wallet
     ↓
Trading Wallet
     ↓ profit harvesting
Treasury Wallet
```

The trading wallet should contain only capital intended to be exposed.

## Withdrawal Allowlist

Automatic harvesting can transfer only to pre-authorized addresses.

Changing an allowlisted destination requires strong authentication and
preferably a cooldown.

------------------------------------------------------------------------

# 55. Emergency Controls

The system requires:

## Soft Pause

No new entries; existing positions continue under exit rules.

## Hard Pause

No autonomous entries or non-protective actions.

## Emergency Exit

Close configured positions subject to liquidity and execution safety.

## Global Kill Switch

Immediately prevents new order creation.

------------------------------------------------------------------------

# 56. Observability

Track:

-   GMGN request latency/error rate;
-   Solana RPC latency;
-   WebSocket disconnects;
-   source detection latency;
-   decision latency;
-   transaction latency;
-   transaction success rate;
-   actual slippage;
-   quote-vs-fill difference;
-   P&L;
-   drawdown;
-   rejection rates;
-   risk events;
-   harvest events.

------------------------------------------------------------------------

# 57. Strategy Versioning

Every decision must record which strategy configuration produced it.

Example:

``` text
strategy_version = v17
```

If thresholds change tomorrow, historical trades remain reproducible.

------------------------------------------------------------------------

# 58. Audit Log

Every significant action should record:

``` text
timestamp
actor
action
before
after
reason
strategy_version
```

Actors:

``` text
SYSTEM
USER
RISK_ENGINE
HARVEST_ENGINE
AI_ANALYSIS
```

------------------------------------------------------------------------

# 59. Core Metrics

## Trading

-   net P&L;
-   ROI;
-   win rate;
-   expectancy;
-   profit factor;
-   max drawdown;
-   Sharpe-like risk-adjusted measures where appropriate;
-   average win/loss;
-   loss streak.

## Copyability

-   replication ratio;
-   median entry latency;
-   median exit latency;
-   median slippage;
-   source vs follower return gap;
-   profitable-source-trade replication rate.

## Trader Selection

-   percentage of promoted traders profitable for system;
-   performance by score decile;
-   performance by confidence;
-   degradation detection accuracy.

## Risk

-   worst daily loss;
-   drawdown;
-   exposure concentration;
-   circuit-breaker frequency;
-   loss prevented by rejected signals.

## Harvesting

-   total harvested;
-   total retained;
-   active capital growth;
-   treasury growth;
-   percentage of lifetime realized gains secured.

------------------------------------------------------------------------

# 60. The Most Important Product Metric

The primary strategy metric should be:

> **Net Copyable Expectancy**

Conceptually:

``` text
Net Copyable Expectancy =
P(win) × AverageRealizedWin
-
P(loss) × AverageRealizedLoss
-
AverageExecutionCosts
```

measured using the system's actual/simulated fills.

A trader can have a 70% source win rate and still produce negative net
copyable expectancy.

------------------------------------------------------------------------

# 61. Example End-to-End Trade

Trader `A`:

``` text
Score: 94
Confidence: 91
30d ROI: +63%
90d ROI: +141%
Max drawdown: 12%
Historical copyability: 76%
```

Trader buys token `XYZ`.

System detects transaction `2.4 seconds` later.

At detection:

``` text
Source entry:       $0.01000
Current price:      $0.01018
Price movement:     +1.8%
Liquidity:          acceptable
Expected slippage:  0.7%
Token risk:         18/100
Portfolio exposure: acceptable
```

Risk engine approves:

``` text
Portfolio:          $1,000
Base risk:          1%
Confidence factor:  0.9
Final allocation:   $9
```

Transaction executes at:

`$0.01027`

The source later partially exits.

Follower exit logic combines source behavior and configured profit
rules.

The position closes net +18%.

System records:

``` text
Source return:       +24%
Follower gross:      +19.2%
Follower net:        +18%
Replication ratio:    75%
```

Trader's replication score is updated.

------------------------------------------------------------------------

# 62. Example Rejected Trade

Trader score:

`96`

Source buys token.

By detection:

``` text
Price has moved +17%
Liquidity is declining
Expected slippage = 5%
```

System rejects.

Even an excellent trader cannot override poor copyability.

Reason:

``` text
PRICE_CHASE_LIMIT_EXCEEDED
SLIPPAGE_LIMIT_EXCEEDED
```

If token later rises another 200%, rejection is still considered correct
according to the configured risk policy; decisions must not be judged
solely by hindsight.

------------------------------------------------------------------------

# 63. Example Profit Harvest

Starting capital:

`$1,000`

Portfolio reaches:

`$1,250`

Configured +25% threshold says:

`Harvest 35% of eligible realized profit.`

Eligible realized profit:

`$250`

Harvest:

`$87.50`

Retained:

`$162.50`

The strategy continues with the configured active capital while `$87.50`
is moved to the treasury.

Later gains are calculated using high-water-mark/harvest accounting so
the same profit is not harvested twice.

------------------------------------------------------------------------

# 64. Risk Presets

The interface may provide presets while exposing advanced configuration.

## Conservative

-   smaller per-trade allocation;
-   stricter token filters;
-   lower maximum chase;
-   stronger harvesting;
-   lower drawdown tolerance.

## Balanced

-   moderate sizing;
-   moderate harvesting;
-   moderate filters.

## Aggressive

-   larger allocations;
-   wider token universe;
-   higher active capital retention.

"Aggressive" must never disable absolute safety controls.

------------------------------------------------------------------------

# 65. Operator Control Center

The operator should be able to answer immediately:

``` text
What is the bot doing?
Why did it do it?
How much is currently at risk?
Who are we following?
Who is making us money?
Who is losing us money?
How much have we secured?
What is our current drawdown?
What would happen if trading stopped now?
```

------------------------------------------------------------------------

# 66. Research Mode

The platform should support a no-execution intelligence mode.

Possible queries:

-   "Show traders with positive 90-day returns and \<15% drawdown."
-   "Which wallets remain profitable with a simulated five-second copy
    delay?"
-   "Which traders made us money over the last 30 days?"
-   "Which source wallets look amazing but become unprofitable after
    slippage?"
-   "Which tokens produced most losses?"
-   "Which traders' performance is deteriorating?"

AI can translate natural-language questions into safe analytical queries
over internal data.

------------------------------------------------------------------------

# 67. Daily System Report

Generate:

``` text
Starting Equity
Ending Equity
Net P&L
Realized P&L
Unrealized P&L
Profit Harvested

Trades Detected
Trades Copied
Trades Rejected

Best Trader
Worst Trader
Best Trade
Worst Trade

Average Entry Latency
Average Slippage
Transaction Failure Rate

Current Drawdown
Risk Events
Trader Promotions/Demotions
```

------------------------------------------------------------------------

# 68. Weekly Strategy Review

Automatically analyze:

-   strategy performance;
-   source versus follower gap;
-   trader decay;
-   score calibration;
-   rejection quality;
-   risk events;
-   profit harvesting;
-   execution deterioration.

The system may recommend parameter changes but should not automatically
alter hard risk limits without authorization.

------------------------------------------------------------------------

# 69. Data Retention

Retain sufficient historical data to reproduce decisions.

Critical records should be durable:

-   source trades;
-   market snapshots used in decisions;
-   score snapshots;
-   decision inputs;
-   orders;
-   fills;
-   risk events;
-   portfolio snapshots;
-   harvest events;
-   strategy versions.

------------------------------------------------------------------------

# 70. Testing

## Unit Tests

Cover:

-   score calculations;
-   position sizing;
-   price-chase logic;
-   loss limits;
-   drawdown calculations;
-   profit harvesting;
-   high-water marks;
-   P&L;
-   slippage;
-   reason codes.

## Integration Tests

Cover:

-   GMGN adapter;
-   Solana RPC;
-   transaction parsing;
-   execution provider;
-   database;
-   event processing.

## Simulation Tests

Simulate:

-   50% token crash;
-   rug/liquidity disappearance;
-   GMGN outage;
-   RPC outage;
-   duplicate event;
-   delayed source transaction;
-   source sells before follower entry;
-   portfolio loss limit;
-   profit threshold crossing;
-   failed treasury transfer;
-   extreme slippage.

------------------------------------------------------------------------

# 71. Acceptance Criteria for Full Product

The complete product is considered functionally implemented when it can:

1.  continuously discover candidate Solana traders;
2.  maintain multi-window performance histories;
3.  rank traders by more than raw ROI;
4.  calculate confidence;
5.  calculate historical and observed copyability;
6.  detect followed-wallet transactions in near real time;
7.  reject stale/chased trades;
8.  evaluate liquidity and slippage;
9.  apply token risk checks;
10. detect selected suspicious wallet behaviors;
11. enforce trader and portfolio exposure limits;
12. size positions dynamically;
13. paper trade using realistic follower fills;
14. shadow trade;
15. execute live trades securely;
16. manage partial and full exits;
17. mirror source exits where configured;
18. apply independent stop/take-profit rules;
19. calculate source-vs-follower replication;
20. re-score traders continuously;
21. automatically reduce or stop allocations to deteriorating traders;
22. enforce daily loss limits;
23. enforce portfolio drawdown limits;
24. maintain high-water marks;
25. automatically harvest eligible profits according to configured
    rules;
26. preserve minimum active capital;
27. transfer harvested funds only to approved destinations;
28. maintain complete audit logs;
29. provide emergency stop controls;
30. expose full portfolio/trader/trade dashboards;
31. attribute P&L to traders and strategies;
32. backtest with latency/slippage assumptions;
33. compare strategy versions;
34. produce daily and weekly reports;
35. recover safely from API/RPC failures.

------------------------------------------------------------------------

# 72. Development Phases

Although this PRD describes the **full product**, implementation should
be staged to avoid deploying unvalidated financial logic all at once.

## Phase A --- Intelligence Foundation

-   GMGN adapter;
-   Solana ingestion;
-   trader database;
-   leaderboard discovery;
-   wallet metrics;
-   score engine;
-   historical analysis.

## Phase B --- Copyability Research

-   historical replay;
-   latency models;
-   slippage models;
-   replication metrics;
-   trader ranking based on copyable alpha.

## Phase C --- Real-Time Paper System

-   real-time detection;
-   copyability gate;
-   risk engine;
-   simulated execution;
-   exits;
-   P&L.

## Phase D --- Shadow Execution

-   construct exact live transactions;
-   measure routing;
-   estimate fills;
-   compare against paper assumptions.

## Phase E --- Controlled Live Execution

-   dedicated wallet;
-   minimal capital;
-   strict limits;
-   live attribution;
-   automatic degradation.

## Phase F --- Capital Management

-   treasury wallet;
-   high-water marks;
-   harvesting;
-   profit locks;
-   capital buckets.

## Phase G --- Adaptive Intelligence

-   wallet graph;
-   cluster detection;
-   strategy decay;
-   dynamic allocations;
-   AI research interface;
-   advanced portfolio optimization.

------------------------------------------------------------------------

# 73. Key Hypotheses to Validate

The product is based on hypotheses, not assumptions.

## H1

Some GMGN-discovered profitable traders remain profitable after
realistic follower latency.

## H2

Copyability varies significantly across traders and can be predicted.

## H3

Longer-term consistency improves future follower performance.

## H4

Rejecting trades after excessive price movement improves net expectancy.

## H5

Dynamic trader re-scoring reduces drawdown.

## H6

Independent risk-managed exits can outperform blindly mirroring source
exits.

## H7

Multi-trader confirmation improves signal quality after removing
correlated wallets.

## H8

Profit harvesting improves capital preservation without destroying
long-run strategy growth.

Each hypothesis must be tested against data.

------------------------------------------------------------------------

# 74. Product Moat

Basic copy trading is already available.

The defensible product is the intelligence and feedback system around
it.

Potential moat:

### 1. Copyability Dataset

A proprietary history of:

``` text
source action
→ detection
→ market movement
→ simulated/live follower fill
→ exit
→ actual follower return
```

### 2. Trader Replication Scores

Ranking traders based on how profitable they are **for followers**, not
themselves.

### 3. Strategy Decay Detection

Knowing when to stop copying before historical leaderboard statistics
fully deteriorate.

### 4. Wallet Relationship Graph

Detecting correlated, insider, and manipulative wallet clusters.

### 5. Execution Dataset

Understanding which signals survive real Solana execution.

### 6. Capital Protection

Portfolio-level harvesting, drawdown controls, and profit locks.

------------------------------------------------------------------------

# 75. Product Evolution

The system can eventually evolve from:

``` text
Copy profitable wallets
```

to:

``` text
Discover repeatable on-chain behaviors
```

and ultimately:

``` text
Construct independent strategies from patterns learned across many successful traders.
```

At that point the system is no longer dependent on mirroring one wallet.
It can identify patterns such as:

-   what types of tokens consistently attract successful traders;
-   how early profitable traders enter;
-   when clusters begin accumulating;
-   what liquidity conditions correlate with successful trades;
-   what exit patterns preserve gains.

Any such evolution must still be validated empirically and constrained
by deterministic risk controls.

------------------------------------------------------------------------

# 76. Final Product Definition

This product is **not**:

> "A bot that copies the highest-return wallet on GMGN."

It is:

> **An adaptive Solana smart-money execution and capital-management
> system that discovers profitable traders, measures whether their edge
> is realistically copyable, selectively follows qualifying trades,
> controls portfolio risk, manages exits, secures a configurable portion
> of realized gains, and continuously learns which traders and
> strategies actually produce net returns for the follower.**

The product's core loop is:

``` text
DISCOVER
   ↓
EVALUATE
   ↓
OBSERVE
   ↓
CHECK COPYABILITY
   ↓
CHECK RISK
   ↓
SIZE
   ↓
EXECUTE
   ↓
MANAGE
   ↓
HARVEST
   ↓
MEASURE
   ↓
RE-SCORE
   ↓
REPEAT
```

The core question at every stage remains:

> **Not "Did this trader make money?" but "Can we safely and repeatedly
> capture enough of this trader's edge after real-world execution costs
> and delays to produce positive net expectancy?"**

------------------------------------------------------------------------

# 77. Safety / Financial Disclaimer

This specification describes software for speculative cryptocurrency
trading. It does not establish that any strategy will be profitable, and
no return---including unusually high leaderboard returns---should be
treated as expected or repeatable. Backtests and paper trading can also
differ materially from live execution.

Live deployment should use only capital the operator is prepared to
lose, secure private-key infrastructure, conservative initial limits,
independent review of execution/security code, and compliance with
applicable laws, exchange/provider terms, tax requirements, and
financial regulations in the jurisdictions where the product is operated
or offered.
