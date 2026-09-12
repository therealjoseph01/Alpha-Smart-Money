#!/usr/bin/env bash
#
# Stop all ASM services. Leaves Redis and Postgres running (other things may use them).
# Pass --all to stop those too.
#
set -uo pipefail
cd "$(dirname "$0")"

ok() { printf "  \033[32m✓\033[0m %s\n" "$1"; }

if [ -f logs/pids ]; then
  while read -r pid name; do
    kill "$pid" 2>/dev/null && ok "stopped $name (pid $pid)"
  done < logs/pids
  rm -f logs/pids
fi

for pattern in "uvicorn asm.services.api.main" "arq asm.services.worker" \
               "asm.services.decision_service" "asm.services.position_service"; do
  pkill -f "$pattern" 2>/dev/null && ok "stopped stray: $pattern"
done

# An orphan may not match the patterns above (different cmdline shape), so also
# free the port itself.
PORT="${PORT:-8000}"
for p in "$PORT" 8000 8099; do
  for pid in $(lsof -nP -iTCP:"$p" -sTCP:LISTEN -t 2>/dev/null); do
    if ps -p "$pid" -o command= 2>/dev/null | grep -q "asm.services"; then
      kill -9 "$pid" 2>/dev/null && ok "freed port $p (pid $pid)"
    fi
  done
done

sleep 0.5
if ps aux | grep -E "uvicorn asm|asm\.services" | grep -v grep >/dev/null; then
  printf "  \033[33m!\033[0m still alive:\n"
  ps aux | grep -E "uvicorn asm|asm\.services" | grep -v grep | awk '{print "      "$2"  "$11" "$12}'
else
  ok "all services stopped"
fi

if [ "${1:-}" = "--all" ]; then
  brew services stop redis >/dev/null 2>&1 && ok "redis stopped"
fi
