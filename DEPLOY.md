# Deploying with Dokploy

## 1. Create the service

**Create Service → Compose.** Not Application (that runs one process; this needs four)
and not Database (that is Redis alone — the compose file already includes it).

- Repository: `git@github.com:therealjoseph01/Alpha-Smart-Money.git`
- Branch: `main`
- Compose path: `docker-compose.yml`

## 2. Environment

Paste into Dokploy's environment editor. These are the only values it needs; everything
else has a default, and the risk and gate settings live in the database where the
dashboard can change them.

```
ENV=prod
LOG_JSON=true

HELIUS_API_KEY=<rotate this before deploying>
GMGN_API_KEY=<rotate this too>
GMGN_ENABLED=true

API_TOKEN=<32+ random characters — this is the dashboard password>
```

Generate the token with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Do not paste these

Your local `.env` has more in it. The rest is either wrong for a container or does
nothing, and pasting it causes real problems rather than clutter:

| Leave out | Why |
|---|---|
| `DATABASE_URL` | Compose sets it to `/app/data/asm.db` on the shared volume. A local path here makes each container create its own empty database. |
| `REDIS_URL` | Compose points it at the redis container. `localhost` inside a container is that container. |
| `MODE` | Not read any more. A fresh database starts in paper and the mode is changed from the dashboard, where the promotion gates are enforced. |
| `LOG_LEVEL` | `INFO` is the default. |
| `HELIUS_RPC_URL`, `HELIUS_WS_URL` | Derived from the API key when blank. |
| `RPC_FALLBACK_URL` | Already the default public endpoint. |
| `GMGN_PLAN_WEIGHT` | `5` is the default, correct for the free plan. |
| `TRADING_WALLET_PUBKEY`, `TREASURY_WALLET_PUBKEY` | Blank until you go live. Add them then. |
| `SOLANA_KEYPAIR` | Never. Production signs through the isolated signer process. |

## 3. Deploy

Dokploy builds the image and starts five containers:

| Container | Role |
|---|---|
| `migrate` | runs once, applies the schema, exits |
| `redis` | risk ledger, dedupe keys, sessions |
| `api` | dashboard and API on port 8000 |
| `worker` | discovery, scoring, attribution, cron |
| `decision` | the hot path — detect, gate, execute |
| `positions` | exits, marks, harvesting |

Everything waits for `migrate` to finish, so no service ever starts against a schema it
does not understand.

## 4. Reaching the dashboard

Map a domain to the `api` service on port 8000 in Dokploy. It will terminate TLS and
set `X-Forwarded-Proto`, which the session cookie reads — so the cookie is marked
Secure automatically, with nothing to configure.

The dashboard is password-protected and login is rate limited to eight attempts per IP
per fifteen minutes. Still, prefer not exposing it publicly at all:

```bash
ssh -L 8000:localhost:8000 you@server
```

## 5. Bringing your local database up

Optional. A fresh deploy starts with an empty database and you can discover wallets
there instead.

```bash
./stop.sh
make db-push HOST=you@server DIR=/path/to/dokploy/compose/dir
```

`db-push` snapshots consistently and backs up the remote copy first. Restart the
containers afterwards so they reopen the file.

**Run one instance.** Two bots on the same wallets both trade — double positions,
double credits, and two risk ledgers that disagree. Once deployed, the server is the
real one.

## 6. After it is up

1. Open the dashboard, sign in
2. **Traders → Score all** — reconstructs history, measures copyability
3. `make backtest` — the verdict that gates live trading
4. **Start trading** — nothing is copied until this is pressed

## Notes

**SQLite on a shared volume.** Five containers, one file, same host. WAL means readers
never block the writer and a 30-second busy timeout means the writer waits rather than
failing. Fine at this write volume. If it ever is not, point `DATABASE_URL` at Postgres —
the schema is portable both ways.

**Redis persists.** `appendonly yes`, because the risk ledger holds deployed capital
and open position counts. Losing it mid-session would reset those to zero while
positions are still open.

**Backups.** The database is the copyability dataset, the one thing that cannot be
regenerated:

```bash
docker compose exec api sqlite3 /app/data/asm.db ".backup '/app/data/backup.db'"
```
