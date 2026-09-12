#!/usr/bin/env bash
#
# Move the SQLite database between this machine and a server.
#
#   ./scripts/db.sh backup                  snapshot locally
#   ./scripts/db.sh push  user@host [dir]   send this database to the server
#   ./scripts/db.sh pull  user@host [dir]   fetch the server's database to here
#
# SQLite is a single file, so "deploying the database" is copying it. The rule that
# matters: never copy a file that is being written. These use sqlite3 .backup, which
# takes a consistent snapshot even while something is running - cp can capture a
# half-written page.
set -euo pipefail
cd "$(dirname "$0")/.."

DB="data/asm.db"
DEFAULT_DIR="Alpha-Smart-Money"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$1" >&2; exit 1; }

snapshot() { sqlite3 "$1" ".backup '$2'" || die "could not snapshot $1"; }

cmd="${1:-}"; host="${2:-}"; dir="${3:-$DEFAULT_DIR}"

case "$cmd" in
  backup)
    [ -f "$DB" ] || die "$DB does not exist"
    out="data/backup-$(date +%Y%m%d-%H%M%S).db"
    snapshot "$DB" "$out"
    ok "$out  ($(du -h "$out" | cut -f1))"
    ;;

  push)
    [ -n "$host" ] || die "usage: ./scripts/db.sh push user@host [remote-dir]"
    [ -f "$DB" ] || die "$DB does not exist"
    pgrep -f "asm.services" >/dev/null 2>&1 && \
      warn "services are running here; the snapshot is consistent but stops at now"

    tmp="data/.push.db"
    snapshot "$DB" "$tmp"
    ok "snapshot taken ($(du -h "$tmp" | cut -f1))"

    ssh "$host" "cd '$dir' && mkdir -p data && \
      if [ -f data/asm.db ]; then cp data/asm.db data/asm.db.replaced-\$(date +%Y%m%d-%H%M%S); fi" \
      || die "could not reach $host:$dir"
    ok "remote database backed up before overwrite"

    scp -q "$tmp" "$host:$dir/data/asm.db" || die "copy failed"
    rm -f "$tmp"
    ok "pushed to $host:$dir/data/asm.db"
    warn "restart the remote services so they reopen the new file"
    ;;

  pull)
    [ -n "$host" ] || die "usage: ./scripts/db.sh pull user@host [remote-dir]"
    ssh "$host" "cd '$dir' && sqlite3 data/asm.db \".backup 'data/.pull.db'\"" \
      || die "could not snapshot the remote database"
    if [ -f "$DB" ]; then
      cp "$DB" "data/asm.db.replaced-$(date +%Y%m%d-%H%M%S)"
      ok "local database backed up before overwrite"
    fi
    scp -q "$host:$dir/data/.pull.db" "$DB" || die "copy failed"
    ssh "$host" "rm -f '$dir/data/.pull.db'"
    ok "pulled from $host into $DB"
    ;;

  *)
    sed -n '3,7p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
