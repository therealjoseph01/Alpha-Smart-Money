#!/usr/bin/env bash
#
# Start the whole system: Redis, Postgres, the database, and all four services.
#
#   ./start.sh              start everything on port 8000
#   PORT=8099 ./start.sh    start everything on a different port
#   ./stop.sh               stop everything
#
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
PG_FORMULA="${PG_FORMULA:-postgresql@14}"
mkdir -p logs
: > logs/pids

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$1" >&2; exit 1; }

wait_for() {  # wait_for <seconds> <command...>
  local timeout=$1; shift
  for _ in $(seq 1 "$timeout"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}

# ---------------------------------------------------------------- 1. infra
bold "1/4  infrastructure"

if redis-cli ping >/dev/null 2>&1; then
  ok "redis already running"
else
  if command -v brew >/dev/null 2>&1 && brew list redis >/dev/null 2>&1; then
    brew services start redis >/dev/null 2>&1
  else
    command -v redis-server >/dev/null 2>&1 || die "redis is not installed  →  brew install redis"
    redis-server --daemonize yes >/dev/null 2>&1
  fi
  wait_for 20 redis-cli ping || die "redis would not start"
  ok "redis started"
fi

if pg_isready -q 2>/dev/null; then
  ok "postgres already running"
else
  command -v pg_isready >/dev/null 2>&1 || die "postgres is not installed  →  brew install $PG_FORMULA"
  brew services start "$PG_FORMULA" >/dev/null 2>&1
  wait_for 30 pg_isready -q || die "postgres would not start  →  brew services list"
  ok "postgres started"
fi

# ------------------------------------------------------------- 2. database
bold "2/4  database"

if ! psql -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='asm'" 2>/dev/null | grep -q 1; then
  psql -d postgres -c "CREATE ROLE asm LOGIN PASSWORD 'asm' SUPERUSER;" >/dev/null 2>&1 \
    && ok "role 'asm' created" || die "could not create the asm role"
else
  ok "role 'asm' exists"
fi

if ! psql -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='asm'" 2>/dev/null | grep -q 1; then
  psql -d postgres -c "CREATE DATABASE asm OWNER asm;" >/dev/null 2>&1 \
    && ok "database 'asm' created" || die "could not create the asm database"
else
  ok "database 'asm' exists"
fi

[ -f .env ] || { cp .env.example .env; ok ".env created from .env.example"; }

if uv run alembic upgrade head >logs/migrate.log 2>&1; then
  ok "schema up to date"
else
  die "migrations failed  →  see logs/migrate.log"
fi

# ------------------------------------------------------------- 3. preflight
bold "3/4  preflight"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  holder=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -Fc 2>/dev/null | grep '^c' | head -1 | cut -c2-)
  free_port=$PORT
  while lsof -nP -iTCP:"$free_port" -sTCP:LISTEN >/dev/null 2>&1; do
    free_port=$((free_port + 1))
  done
  printf "  \033[31m✗\033[0m port %s is in use by '%s'\n" "$PORT" "${holder:-unknown}" >&2
  echo "      if it is a stale ASM process:  ./stop.sh" >&2
  echo "      otherwise use another port:    PORT=$free_port ./start.sh" >&2
  exit 1
fi
ok "port $PORT is free"

if grep -qE '^HELIUS_API_KEY=.+' .env; then
  ok "HELIUS_API_KEY is set"
else
  warn "HELIUS_API_KEY is not set in .env"
  echo "      Services will run and the dashboard will load, but no wallet"
  echo "      history can be fetched — so nothing gets scored, promoted or traded."
  echo "      Get a free key at https://dashboard.helius.dev"
fi

# grep -c exits 1 when the count is zero, so capture then default - never "||  echo 0",
# which would append a second value to the output.
WALLETS=$(grep -cvE '^[[:space:]]*(#|$)' seeds/wallets.txt 2>/dev/null)
WALLETS=${WALLETS:-0}
if [ "$WALLETS" -gt 0 ]; then
  ok "$WALLETS wallet(s) in seeds/wallets.txt"
else
  warn "seeds/wallets.txt is empty — add wallet addresses, then run: make seed"
fi

# -------------------------------------------------------------- 4. services
bold "4/4  services"

PIDS=()
NAMES=()

# Ctrl+C (or kill) tears the whole stack down. Without this the services would be
# orphaned in the background and the next start would hit "port already in use".
shutdown() {
  trap - INT TERM EXIT
  echo
  bold "shutting down"
  for i in "${!PIDS[@]}"; do
    if kill "${PIDS[$i]}" 2>/dev/null; then
      printf "  stopped %-10s pid %s\n" "${NAMES[$i]}" "${PIDS[$i]}"
    fi
  done
  # Give them a moment to close sockets and DB connections cleanly.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    still_running=0
    for pid in "${PIDS[@]}"; do kill -0 "$pid" 2>/dev/null && still_running=1; done
    [ "$still_running" -eq 0 ] && break
    sleep 0.3
  done
  for pid in "${PIDS[@]}"; do kill -9 "$pid" 2>/dev/null; done
  rm -f logs/pids
  ok "all services stopped"
  exit 0
}
trap shutdown INT TERM EXIT

start() {  # start <name> <command...>
  local name=$1; shift
  "$@" > "logs/$name.log" 2>&1 &
  local pid=$!
  PIDS+=("$pid"); NAMES+=("$name")
  echo "$pid $name" >> logs/pids
  sleep 0.6
  if kill -0 "$pid" 2>/dev/null; then
    printf "  \033[32m✓\033[0m %-10s pid %-7s logs/%s.log\n" "$name" "$pid" "$name"
  else
    printf "  \033[31m✗\033[0m %-10s failed — last lines:\n" "$name"
    tail -6 "logs/$name.log" | sed 's/^/        /'
  fi
}

export PORT
start api       uv run uvicorn asm.services.api.main:app --port "$PORT"
start worker    uv run arq asm.services.worker.WorkerSettings
start decision  uv run python -m asm.services.decision_service
start positions uv run python -m asm.services.position_service

echo
bold "running"
echo "  dashboard   http://localhost:$PORT"
echo "  api docs    http://localhost:$PORT/docs"
echo "  logs        tail -f logs/*.log   (in another terminal)"
echo
printf "  \033[1mPress Ctrl+C to stop everything\033[0m\n"
echo

# Stay in the foreground so Ctrl+C reaches the trap. If any service dies on its own,
# report it and tear the rest down rather than limping along half-running.
while true; do
  for i in "${!PIDS[@]}"; do
    if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
      printf "\n  \033[31m✗\033[0m %s exited unexpectedly — see logs/%s.log\n" \
        "${NAMES[$i]}" "${NAMES[$i]}"
      tail -8 "logs/${NAMES[$i]}.log" | sed 's/^/        /'
      shutdown
    fi
  done
  sleep 2
done
