#!/usr/bin/env bash
#
# Move the SQLite database between this machine and a server.
#
#   ./scripts/db.sh backup                       snapshot locally
#   ./scripts/db.sh push       user@host [dir]    send this database to the server
#   ./scripts/db.sh pull       user@host [dir]    fetch the server's database to here
#
# On Dokploy the database is inside a Docker volume, not a directory, so copying to
# a path on the host reaches nothing. Use these instead:
#
#   ./scripts/db.sh push-docker user@host [stack] send this database into the volume
#   ./scripts/db.sh pull-docker user@host [stack] fetch it out of the volume
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

# The five containers share one volume mounted at /app/data. Rather than ask for the
# volume's name - Dokploy prefixes it with a generated stack id - find the api
# container and ask Docker where its /app/data comes from.
remote_api() {
  ssh "$host" "docker ps -a --filter name='${stack}' --format '{{.Names}}' \
    | grep -- '-api-1\$' | head -1"
}

# Stop every writer first. A snapshot of a file is only consistent if nothing is
# appending to it, and SQLite's -wal holds committed data that is not yet in the main
# file - which is why both it and -shm have to go when the file is replaced.
remote_services() {
  ssh "$host" "docker ps -a --filter name='${stack}' --format '{{.Names}}' \
    | grep -E -- '-(api|worker|decision|positions)-1\$'"
}

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

  push-docker)
    [ -n "$host" ] || die "usage: ./scripts/db.sh push-docker user@host [stack-name]"
    [ -f "$DB" ] || die "$DB does not exist"
    stack="${3:-}"

    api=$(remote_api) || die "could not reach $host"
    [ -n "$api" ] || die "no api container found on $host (deploy first, or pass the stack name)"
    ok "found $api"

    svcs=$(remote_services)
    [ -n "$svcs" ] || die "no running services found"

    tmp="data/.push.db"
    snapshot "$DB" "$tmp"
    ok "snapshot taken ($(du -h "$tmp" | cut -f1))"

    # shellcheck disable=SC2086
    ssh "$host" "docker stop $svcs >/dev/null" || die "could not stop the services"
    ok "services stopped"

    ssh "$host" "docker cp '$api:/app/data/asm.db' /tmp/asm.db.replaced 2>/dev/null \
      && echo backed-up || true" >/dev/null
    ok "remote database copied to /tmp/asm.db.replaced"

    scp -q "$tmp" "$host:/tmp/asm.push.db" || die "copy failed"

    # docker cp writes through to the volume, and works on a stopped container.
    # The -wal and -shm files must go with it: they describe the OLD database, and
    # SQLite would replay them over the new one.
    ssh "$host" "
      set -e
      docker cp /tmp/asm.push.db '$api:/app/data/asm.db'
      vol=\$(docker inspect -f '{{range .Mounts}}{{if eq .Destination \"/app/data\"}}{{.Name}}{{end}}{{end}}' '$api')
      docker run --rm -v \"\$vol\":/data alpine rm -f /data/asm.db-wal /data/asm.db-shm
      rm -f /tmp/asm.push.db
    " || die "could not write into the volume"
    rm -f "$tmp"
    ok "pushed into the volume, stale -wal and -shm removed"

    # shellcheck disable=SC2086
    ssh "$host" "docker start $svcs >/dev/null" || die "could not restart the services"
    ok "services restarted"
    ;;

  pull-docker)
    [ -n "$host" ] || die "usage: ./scripts/db.sh pull-docker user@host [stack-name]"
    stack="${3:-}"

    api=$(remote_api) || die "could not reach $host"
    [ -n "$api" ] || die "no api container found on $host"

    ssh "$host" "docker exec '$api' sqlite3 /app/data/asm.db \".backup '/tmp/p.db'\" \
      && docker cp '$api:/tmp/p.db' /tmp/asm.pull.db && docker exec '$api' rm -f /tmp/p.db" \
      || die "could not snapshot the remote database"

    if [ -f "$DB" ]; then
      cp "$DB" "data/asm.db.replaced-$(date +%Y%m%d-%H%M%S)"
      ok "local database backed up before overwrite"
    fi
    scp -q "$host:/tmp/asm.pull.db" "$DB" || die "copy failed"
    ssh "$host" "rm -f /tmp/asm.pull.db"
    ok "pulled from $host into $DB"
    ;;

  *)
    sed -n '3,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
