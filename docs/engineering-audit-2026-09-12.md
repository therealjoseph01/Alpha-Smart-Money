# Engineering audit — 12 September 2026

**Verdict: the full PRD is not implemented, and this code is not ready for unattended live trading.** Passing tests do not establish zero defects or profitable execution. This review found and repaired concrete bugs, but also found architectural gaps that need implementation and independent live-path validation.

## Scope and evidence

Reviewed the PRD, all substantive modules under `src/asm` (77 Python files including package files), the dashboard source, configuration, schema/migration, CLI, worker schedules, and the existing test coverage. Traced entry, exit, emergency, treasury, and attribution flows across service boundaries. No real trades, treasury transfers, notifications, or provider-account changes were performed.

Baseline: 182 tests passed with local Redis access. The sandbox initially prevented Redis connections; those were environment errors, not 48 application failures. Initial lint found one unused import; the initial configured type check reported 29 errors.

Final checks are recorded in `audit-validation.txt`. Added regression coverage for risk concurrency, persistence, parser units, paper exits, ambiguous broadcasts, controls, configuration, and treasury eligibility. Postgres tests run the actual initial Alembic migration in a unique schema inside a transaction and roll back the schema and all test records. API smoke coverage exercises 19 read endpoints using isolated database state. Redis tests explicitly select database 15; that database is cleared by the suite.

This is static review plus deterministic local integration testing. It is not a mainnet/devnet integration certification, a load/soak test, a browser interaction test, or a dependency vulnerability scan. The existing “end-to-end” tests stub providers and do not run the complete production service topology.

## Repairs made

| Area | Defect and resulting behavior |
|---|---|
| Position limit | Reservations counted only committed positions. Pending entries now count toward the maximum, preventing simultaneous approvals from overbooking the cap. |
| Reservation lifetime | The hold hash expired after 120 seconds without reversing cash/exposure. Held reservations now persist until explicit release/commit. This prevents lost accounting metadata; automatic reconciliation is still required. |
| Uncertain broadcasts | Send/confirmation timeouts were marked failed and entry capital released. They now remain submitted with the signed transaction identity, preserve the hold, and record a reconciliation-required risk event. Signed identities no longer expire after a day. |
| Fill reconciliation | Missing transaction data was treated as a quoted successful fill; sell output units were confused with purchased tokens. Unavailable/mismatched fills now require reconciliation; parsed sell proceeds and token units determine price. The total chain fee is not double-counted as base plus priority. |
| Simulation | A missing simulation response looked like success. It now rejects execution. |
| Protected routing | The builder did not attach a tip, while submission claimed protected routing. Such plans now explicitly reject with `protected_route_requires_tip_instruction`. This is a containment measure, not an implementation of protected routing. Shared mutable plan state was removed. |
| Paper exits | Exit orders had no quote, and paper pricing treated sells as buys. Missing sell quotes are fetched, adverse sell slippage lowers proceeds, and output is denominated in SOL raw units. |
| Shadow sells | Expected exit price used output SOL with the input token's decimals. It now derives SOL proceeds and divides by sold token units. |
| Take-profit persistence | Filled ladder levels were never saved. The level is now persisted with the reduction and survives reload. |
| Marking | Falling prices did not trigger mark persistence. Price changes now do. Position objects are updated after exits before the tick totals unrealized P&L. |
| Source-exit signals | Signals were consumed even after failed exits or unrelated take-profit actions. They remain pending unless the mirror exit succeeds. Parsed partial-sale fractions replace the fixed 50% assumption on the live ingestion path. |
| Parser | Closing token accounts lost their decimals; stablecoin trades could be misclassified from small SOL rent changes; LP payments could appear as buys. These cases are corrected and tested. Zero-decimal metadata no longer falls back to nine decimals. |
| Price chase | Stablecoin source prices were compared to SOL prices. Comparisons now use matching quote currencies; unknown source prices reject. Gate latency includes enrichment time. |
| Token safety | Missing mint fields or holder evidence could be treated as clean. Those cases now fail closed; collected flags are retained. |
| Kill switch | Pause/resume could overwrite the system-state string while ignoring the kill flag. The flag now remains authoritative in controls, reservations, and the local signer. Clearing it leaves the system hard-paused. Invalid system state fails closed. |
| Signer allowlist | Unresolved program indices were skipped. They now reject pending proper lookup-table validation. This does not implement complete transaction-policy enforcement. |
| Emergency response | API responses claimed liquidation was queued even when enqueueing failed. `exit_queued` now reflects whether enqueueing returned a job. |
| Database identities | Duplicate source insertion returned an ID absent from the database. It now returns the persisted ID. Retried order insertion likewise preserves the actual persisted order ID for execution foreign keys. |
| Trader allocation | A zero size multiplier became one through truthiness fallback. Zero remains zero. Followed-wallet membership refreshes even when only trader eligibility changes. |
| Configuration | Nested `RISK_*` and `COPY_*` settings ignored `.env`. Both now load it. |
| Treasury | Explicit destinations must match the configured treasury, and amounts must be finite and positive. Unrealized gains alone no longer trigger harvesting. |
| API authentication | The documented default development token no longer authorizes mutations. Bearer syntax is required, comparison is constant-time, and dashboard control failures become visible. |
| Types/test isolation | Corrected type errors rather than suppressing the checker; the stronger `--check-untyped-defs` check passes. Tests no longer inherit an arbitrary production `REDIS_URL` before calling `FLUSHDB`. |

## Remaining release blockers

### 1. Signing security is incomplete — critical

[`execution/signer.py`](../src/asm/execution/signer.py) accepts `max_lamports` but does not enforce the amount. Allowlisting the System and Token programs alone does not restrict transfer recipients, amounts, account ownership, or the meaning of instructions. No isolated signer server implementation is present; `RemoteSigner` is a client. `get_signer()` still selects an in-process local key when an environment key exists. `MODE=live` does not validate the PRD's full readiness conditions.

Required: an isolated signer service with authenticated requests, decoded transaction-policy checks, resolved lookup tables, recipient/spend restrictions, independent controls, and real transaction fixtures.

### 2. Recovery is not transactional — critical

[`services/decision_service.py`](../src/asm/services/decision_service.py), [`state/ledger.py`](../src/asm/state/ledger.py), and [`db/repo.py`](../src/asm/db/repo.py) change Postgres and Redis separately. Redis commit can succeed before a database commit fails. A crash after signing/sending and before durable persistence is not recovered automatically. There is no worker to settle the newly retained ambiguous submissions. Entry failures before execution can leave reservations held. Redis loss bootstraps configured starting cash rather than rebuilding real positions and balances.

Required: a durable order state machine, idempotent ledger projection, restart reconciliation, signature/expiry handling, and failure-injection tests at every persistence boundary. Retained holds must not be manually released merely because a timeout elapsed.

### 3. Emergency liquidation cannot work through the current live signer — critical

[`services/tasks/emergency.py`](../src/asm/services/tasks/emergency.py) is queued after the system is killed, but the signer refuses all signing in that state. Hard pause similarly permits exits at the service level but rejects them at the signer. The regular and emergency exit processes also lack shared per-position ownership, so they can race. Protective exits encounter entry-oriented slippage/protection ceilings.

Required: explicit, independently validated protective-exit authorization and a common durable exit state machine. Do not simply bypass the kill flag for arbitrary caller-labeled sells.

### 4. Treasury settlement is not exactly once — critical

[`engine/harvest.py`](../src/asm/engine/harvest.py) snapshots cash, transfers, then debits the ledger without a shared reservation/lock against other harvest callers and entries. A transfer timeout may be retried with a new signature even if the first transfer landed. Failed attempts already have harvest-event amounts and can appear as successful harvested money in the dashboard. Withdrawal-adjusted high-water marks and baseline accounting remain incomplete.

Required: reserve cash atomically, persist transfer identity before submission, reconcile ambiguous transfers, and project only settled harvests into reports. Test concurrent harvesting/entries and crash recovery.

### 5. Net P&L and risk protection remain incomplete — high

Paper/live service accounting records fees but does not consistently debit them into portfolio cash, position basis, and realized P&L. The backtester subtracts some fees in reported P&L without consistently subtracting them from ledger equity, and entry/failed-attempt fees are incomplete. Consequently reported net expectancy and equity can disagree.

Daily loss uses realized losses divided by current equity, not the PRD's opening-day net equity loss. Weekly loss limits are absent. The profit-lock and bucket helpers are not wired into entry authorization. Per-trader configured budgets and SOL gas reserves are not comprehensively enforced before entries. Lua/HINCRBYFLOAT uses floating arithmetic despite the README's former “no float” claim.

### 6. Entry validation still has fail-open paths — high

[`engine/gates.py`](../src/asm/engine/gates.py) permits unknown market cap and age and assumes 15% expected alpha when evidence is missing. Final sizing can differ from the size quoted and liquidity-tested, particularly after confirmation bonuses. Price chase, total latency, balance, and portfolio constraints are not comprehensively rechecked immediately before broadcast. Token-2022 transfer restrictions, sellability, deployer relationships, and pool-account holder classification are incomplete.

### 7. Live learning loop is not connected — high

[`services/tasks/ops.py`](../src/asm/services/tasks/ops.py) attributes some closed-position P&L but does not populate the realized outcomes used by scoring. Backfill rebuilds source-based scores without feeding observed replication into them. `next_status()` receives the default zero live-trade count, so automatic probation-to-active promotion does not occur through that path. Multi-window performance and per-signal source/follower comparisons are incomplete.

### 8. Backtests do not establish live readiness — high

[`backtest/loader.py`](../src/asm/backtest/loader.py) uses current trader scores, sparse prints, assumed liquidity, and a fixed SOL price. Enhanced records do not populate source USD size, often leaving reconstructed liquidity at the minimum assumption. Risk/age defaults can misrepresent what was known historically. Source exit delay, analysis-time price movement, daily resets, duplicate signals, custom risk propagation, and end-of-run liquidation need more rigorous modeling. Fee/equity inconsistencies above invalidate net-return certification.

Required: recorded historical market/risk data, walk-forward selection, chronological event scheduling, complete fee accounting, and calibrated latency/fill models.

### 9. Detection and independence are partial — high

Transient transaction fetch failure occurs after claiming signal dedupe, so a signal can be lost. Source exits arriving before follower entry are not reconciled; multiple follower lots can exist while source-exit lookup selects only one. Confirmation bonuses count wallets without proving independent qualification. Cluster sizing is a multiplier, not an atomic aggregate cluster budget; stale cluster relationships/allocations need removal.

### 10. Production operations and auditability are incomplete — high/medium

No continuous discovery job calls the GMGN discovery adapter. Strategy checksum/experiment helpers are not integrated into decisions, which still identify the strategy as constant `v1` without the complete immutable configuration. Health reports reflect process-local adapter state rather than end-to-end provider and worker freshness. Most trade/risk events are not wired to notification delivery. Stream consumers do not reclaim abandoned pending messages. API pagination/IDs lack consistent validation and detail routes lack consistent mode scoping. The dashboard omits several PRD views and can hide failures/stale state. A real browser pass is still required.

## PRD §71 acceptance map

“Partial” means some implementation exists; it does not mean release acceptance passed.

| # | Requirement | Assessment |
|---|---|---|
| 1 | Continuous trader discovery | Missing scheduled discovery; manual seed works |
| 2 | Multi-window performance histories | Partial |
| 3 | Rank beyond ROI | Implemented calculations; historical evidence limited |
| 4 | Confidence | Implemented calculations, tested |
| 5 | Historical and observed copyability | Historical approximation; observed feedback incomplete |
| 6 | Near-real-time detection | Implemented subscription; outage/latency unverified |
| 7 | Reject stale/chased trades | Tested gates; final execution revalidation incomplete |
| 8 | Liquidity/slippage | Partial; proxies and final-size mismatch |
| 9 | Token risk | Partial; missing critical evidence now rejects |
| 10 | Suspicious wallets | Partial heuristics/co-trading graph |
| 11 | Exposure limits | Tested atomic token/trader/position caps; broader budgets incomplete |
| 12 | Dynamic sizing | Implemented formulas; configuration/risk integration incomplete |
| 13 | Realistic paper trading | Exit defects repaired; net fees/calibration incomplete |
| 14 | Shadow mode | Implemented; protected route and external integration blocked |
| 15 | Secure live execution | Blocked by signer and recovery gaps |
| 16 | Partial/full exits | Persistence regression tested; concurrency/recovery incomplete |
| 17 | Mirror source exits | Fractions repaired; multiple lots/pending entries incomplete |
| 18 | Independent stops/profits | Pure rules tested; live execution cannot guarantee exits |
| 19 | Source/follower replication | Incomplete end-to-end attribution |
| 20 | Continuous re-scoring | Scheduled source-based recalculation; live feedback missing |
| 21 | Stop deteriorating allocations | Partial score-based degradation |
| 22 | Daily loss limit | Realized-loss gate only; PRD net-equity/session semantics incomplete |
| 23 | Portfolio drawdown | Basic gate tested; recovery/withdrawal accounting incomplete |
| 24 | High-water marks | Basic ratchet tested; withdrawal adjustment incomplete |
| 25 | Automatic harvesting | Partial; settlement/concurrency blockers |
| 26 | Minimum active capital | Pure rule tested; concurrent transfer protection incomplete |
| 27 | Approved destinations | Configured destination enforced; signer/settlement validation incomplete |
| 28 | Complete audit logs | Partial; some actions/inputs/events not durable or complete |
| 29 | Emergency controls | Kill latching repaired; live emergency liquidation blocked |
| 30 | Full dashboards | Basic views/API smoke-tested; full PRD views incomplete |
| 31 | P&L attribution | Partial trader totals; fees/modes/strategy attribution incomplete |
| 32 | Latency/slippage backtests | Replay exists and tested; material modeling/accounting gaps |
| 33 | Compare strategy versions | Helpers exist; runtime integration missing |
| 34 | Daily/weekly reports | Jobs exist; full metrics/delivery/provider verification incomplete |
| 35 | Safe API/RPC recovery | Partial retries/failover; durable reconciliation missing |

## External contract checks

Jupiter currently recommends migration from the deprecated legacy hostname to `api.jup.ag`; its changelog also says the earlier deprecation deadline was postponed. This review does **not** establish that the configured `lite-api.jup.ag` endpoint is currently down. Authenticate and test the specific Quote/Swap/Price contracts and rate limits before changing environments. [Jupiter platform changelog](https://developers.jup.ag/changelog/developer-platform).

Helius Sender requires a tip; current product documentation describes Sender Max and SWQOS-only tiers. The local code's tip constants/accounts and unsigned swap builder do not establish compliance with those contracts. [Helius Sender](https://www.helius.dev/sender). No provider credential was used to place a transaction during this review.

## Operator implications

Configure a non-default `API_TOKEN` before using mutation endpoints. The dashboard reads the corresponding token from `localStorage['asm_token']`; reload after changing it. Production authentication still needs deployment-level transport protection and secret management.

Use the corrected code for controlled testing. Do not treat the passing suite, a backtest “VIABLE” verdict, or the earlier README completeness claim as approval for unattended live trading. Complete blockers 1–6 before any live-readiness assessment, then validate intelligence/reporting requirements and run representative paper/shadow soak and failure-recovery tests.
