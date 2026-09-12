# Adaptive Smart-Money Copy Trading

Solana copy-trading system built to the PRD in
[adaptive-smart-money-copy-trading-prd.md](adaptive-smart-money-copy-trading-prd.md).

The thesis, in one line:

> A trader being profitable does not mean that trader is profitable to copy.

So this is not a wallet mirror. Every detected trade is re-evaluated against what *we*
could actually capture right now — after detection latency, price drift, liquidity,
slippage, fees, and portfolio risk — and most of them are rejected.

---

## The one architectural decision that matters

The system is two planes, and they never mix:

| | Hot path | Cold path |
|---|---|---|
| Work | detect → classify → gates → reserve → size → execute | backfill, scoring, attribution, decay, harvest, reports |
| Budget | **sub-second** | seconds to minutes |
| Runtime | one `asyncio` process, in-memory queue | **ARQ** workers + cron |
| State | Redis (working memory) | Postgres (ledger of record) |

**ARQ never sits between a detected trade and a submitted order.** A broker round-trip
plus serialization plus worker prefetch costs 50–500ms, and against a gate that rejects
above a 1% price move, that is the entire edge. ARQ does the work that is *allowed* to
be slow — which is most of the intelligence, just none of the trading.

```
Helius logsSubscribe ─→ asyncio.Queue(2048) ─→ 8 workers
                                                   │
              ┌────────────────────────────────────┤
              │  dedupe (Redis SETNX)              │
              │  parse tx from balance deltas      │
              │  gates: trader, price-chase,       │
              │    latency, liquidity, slippage,   │
              │    mcap, age, safety, confirmation │
              │  ATOMIC reserve (Redis Lua)        │
              │  size → execute → persist          │
              └────────────────────────────────────┘
```

---

## Quick start

```bash
make install      # uv sync + .env
make db-init      # start postgres/redis, create schema
```

Add a Helius key to `.env` (free tier is fine — <https://dashboard.helius.dev>):

```
HELIUS_API_KEY=your-key
```

Then seed the wallets you want watched, and run it:

```bash
#  paste 50-100 addresses into seeds/wallets.txt
make seed         # import + queue scoring
make backfill     # reconstruct history, score every wallet

make api          # :8000/docs
make worker       # ARQ cold path
make decision     # hot path
make positions    # exit manager
```

`make status` prints the portfolio; `make test` runs the suite.

### Seeding: why it is manual

GMGN publishes no official public API — the endpoints that circulate are
reverse-engineered and sit behind bot protection, so building on them is building on
sand. The adapter exists (`adapters/gmgn.py`, `GMGN_ENABLED=false`) but the
supported path is simpler and more robust:

**Curate 50–100 wallets by hand from GMGN's UI once. After that the system is
self-sufficient** — it streams those wallets on-chain, reconstructs their trade history
through Helius, scores them on copyable alpha, and promotes or degrades them on its own
evidence.

A seeded wallet enters as `discovered`, **not** `active`. Seeding grants attention,
never capital. It has to earn its way to being copied:

```
discovered → candidate → probation → active
                  ↑            ↓
              (re-earn)   degraded → suspended
```

---

## What the code does that a naive copy bot does not

**Check-and-reserve is atomic.** Two followed traders buying the same token 40ms apart
both pass a read-only risk check and the portfolio ends up over its cap. So the whole
risk check *and* the exposure write happen inside one Lua script
([`state/scripts.py`](src/asm/state/scripts.py)). The test proves it: 20 simultaneous
reservations against a 5-slot cap grant exactly 5.

**Idempotency is claimed before signing.** Geyser and websocket streams replay on
reconnect, and a send timeout is not proof of failure. Source transactions dedupe on a
hash of `(chain, wallet, signature, mint, action)`; orders claim an idempotency key
*before* the signer is called, so a retry can never double-buy.

**Unknown data fails closed.** A token whose mint authority we could not read scores as
dangerous, not as clean. Missing validation data rejects the trade.

**Paper mode is pessimistic on purpose.** A paper engine that fills at the quoted mid is
a lie generator. This one applies the router's real price impact at our real size, an
adverse drift proportional to decision latency, network + priority fees, and a failure
rate that rises as the book thins.

**Shadow is live minus two lines.** `ShadowExecutor` and `LiveExecutor` share
`_prepare()` — same re-quote, same priority-fee estimate, same build, same simulation.
Shadow just stops before `sign()` and `send()`. That makes the shadow-to-live gap a
measurement instead of a hope.

**Rejections are the valuable half of the dataset.** Every rejected trade is stored with
its full input snapshot and reason codes. That is the counterfactual the copyability
model learns from — `GET /trades/stats/rejections` shows which gate is actually doing
the work, so thresholds get tuned from data rather than intuition.

**Exits run in their own process.** Capital preservation does not get to depend on the
service that spends capital. An exit still fires when the decision service is paused,
restarting, or saturated.

**Keys never touch the API or decision processes.** The signer is its own process behind
a unix socket, with a program allowlist and its own kill-switch check — so a pause holds
even if everything upstream is wedged.

---

## Layout

```
src/asm/
  config.py            every tunable (copyability gates, risk limits, harvesting)
  domain/              enums, reason codes, pydantic models, Decimal helpers
  db/                  SQLAlchemy schema (PRD 48) + repository
  state/               Redis: atomic Lua risk ledger, dedupe, event bus
  adapters/            helius, jupiter, gmgn — all behind a circuit breaker
  ingest/parser.py     tx → SourceTrade, classified from balance deltas
  engine/
    gates.py           PRD 17-20, pure functions
    decision.py        PRD 41, the orchestrator
    sizing.py          PRD 21, five modes incl. fractional Kelly
    exits.py           PRD 28-29, pure rules
    harvest.py         PRD 30-33, high-water-mark based
    scoring.py         PRD 13-15, copyability-weighted composite
  execution/           paper | shadow | live behind one Executor protocol
  services/
    decision_service.py   THE HOT PATH
    position_service.py   exits, marks, harvest checks (1Hz)
    worker.py             ARQ functions + cron
    api/                  FastAPI (PRD 49)
```

### Money

Token amounts are `NUMERIC(78,0)` raw integer units. Prices are `NUMERIC(38,18)`.
Python monetary calculations primarily use `Decimal`; Redis Lua and `HINCRBYFLOAT`
use floating arithmetic. Exact accounting and fee reconciliation remain audit findings.

### Positions are projections

`position_events` is append-only and is the source of truth; the `positions` table is a
materialized current state. That gives attribution and audit for free, and means a
position's history can never silently disagree with its balance.

---

## Build order

The PRD's phases A–G, collapsed to what actually gates what:

1. ✅ Skeleton, domain, schema, Helius parse
2. ✅ Cold path: seed → backfill → score → ARQ cron
3. **Backtest/replay — validate H1–H3 before writing a live order**
4. ✅ Hot path in paper mode
5. Shadow, then live with a hard cap
6. ✅ Harvesting + treasury, adaptive re-scoring

Step 3 is the gate. Do not start step 5 until the numbers say the alpha survives your
measured latency — `GET /trades/stats/latency` tells you what that latency actually is.

---

## Status

**Engineering audit: full PRD acceptance and unattended live trading remain blocked.**
See [the audit and acceptance checklist](docs/engineering-audit-2026-09-12.md) and
[recorded validation](docs/audit-validation.txt) for repaired defects, test results, and outstanding work.

Verified running against local Postgres 14 + Redis: schema applied (25 tables), all
endpoints served, both long-running services boot, seed → backfill → decision loop
exercised end to end.

### What each subsystem does

| PRD | Subsystem | Where |
|---|---|---|
| 8, 26 | Four execution modes behind one `Executor` protocol | `execution/` |
| 13–15 | Copyability-weighted scoring, lifecycle, promotion | `engine/scoring.py` |
| 16 | Venue-agnostic tx classification from balance deltas | `ingest/parser.py` |
| 17–20 | The gates, as pure replayable functions | `engine/gates.py` |
| 21–25 | Sizing + the atomic Lua risk ledger | `engine/sizing.py`, `state/` |
| 27 | MEV protection, tips sized against the trade | `execution/mev.py` |
| 28–29 | Independent exit rules | `engine/exits.py` |
| 30–33 | Harvesting, real treasury transfers, profit lock, buckets | `engine/treasury.py` |
| 34–38 | Attribution, re-scoring, decay detection | `services/tasks/` |
| 39 | Wallet graph and cluster detection | `engine/graph.py` |
| 40 | AI layer — advisory, structurally isolated | `ai.py` |
| 42 | Operator control center | `services/api/static/` |
| 43 | Alert delivery: Telegram, Discord, webhook | `alerts.py` |
| 44 | **Backtest engine** — gates live trading | `backtest/` |
| 46, 57 | Experiments + strategy versioning | `engine/experiments.py` |
| 55 | Kill switch + emergency liquidation | `services/tasks/emergency.py` |
| 66 | Research mode | `services/tasks/research.py` |
| 69 | Asymmetric data retention | `services/tasks/retention.py` |

### Three guarantees enforced by tests, not by policy

**The risk ledger cannot be raced.** Twenty simultaneous buys of one token against a
5-slot cap grant exactly five — check and reserve happen inside one Lua script.

**The AI layer cannot touch a trade.** `test_ai_layer_is_not_reachable_from_the_hot_path`
walks the AST of every decision, risk and execution module and fails if any of them
imports `asm.ai`.

**An experiment cannot loosen a hard limit.** Arms that try to override `stop_loss_pct`,
`max_daily_loss_pct` or `max_drawdown_pct` raise at construction.

### Bugs the tests caught during the build

- `Position.age_seconds` read the wall clock, so every replayed position time-exited
  instantly. Exits now use the supplied clock.
- `/system/emergency-stop` enqueued `emergency_exit_all`, which **was never defined** —
  the kill switch stopped new signing but silently no-opped the liquidation.
- Confidence scoring let 3 trades reach 53, above the promotion floor of 50. Sample size
  is now a multiplier, so confidence cannot exceed the evidence.
- A failed treasury transfer used to advance the harvest baseline, making the system
  believe profit was safe while it was still at risk.

### Known limits

- **Detection uses `logsSubscribe`.** Helius LaserStream/Geyser gRPC is 1–3s faster and
  drops in behind `WalletSubscription`. On a 1%-price-move gate that gap matters.
- **Liquidity is derived** by inverting the router's price impact on a $100 probe.
  Venue-agnostic and measured on the path we'd actually trade, but a pool reserve read
  would be better.
- **Backtest price history is sparse** — reconstructed from observed swaps, so it only
  has prints where someone traded. The provider marks stale points and the gates reject
  them rather than interpolating.
- **Trader scores in a backtest are in-sample.** They were computed over the whole
  history, so trader *selection* is mildly optimistic. Execution and risk are honest.
  Walk-forward scoring is the fix.
- Impact and drift constants are deliberately pessimistic guesses. Calibrate them from
  shadow-mode fills before trusting absolute backtest numbers.

## Safety

This trades speculative crypto assets and can lose money. Live mode requires explicit
enablement, a dedicated wallet, configured limits, and a kill switch. Start in `paper`,
graduate to `shadow`, and let live run on capital you would not mind losing entirely.

`python -m asm.cli kill` activates the kill switch. It is checked by the signer itself, so it holds
regardless of what else is broken.
