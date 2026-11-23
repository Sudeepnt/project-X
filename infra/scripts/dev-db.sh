#!/usr/bin/env bash
# Local PostgreSQL 18 (Homebrew) for development: start | stop | status | setup | reset
set -euo pipefail

PG_BIN="${PG_BIN:-/opt/homebrew/opt/postgresql@18/bin}"
PGDATA="${PGDATA:-/opt/homebrew/var/postgresql@18}"
PG_LOG="${PG_LOG:-/opt/homebrew/var/log/postgresql@18.log}"
PSQL="$PG_BIN/psql -X -q -v ON_ERROR_STOP=1"
DATABASES=(projectx projectx_test)

ensure_db() {
  local db="$1"
  if ! $PSQL -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1; then
    "$PG_BIN/createdb" -O projectx "$db"
  fi
  # Extensions need a superuser; migrations then find them already installed.
  $PSQL -d "$db" -c "CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS pgcrypto;
                     CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
}

case "${1:-status}" in
  start)
    mkdir -p "$(dirname "$PG_LOG")"
    if "$PG_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then echo "postgres already running"; else
      LC_ALL=en_US.UTF-8 "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$PG_LOG" -w start; fi ;;
  stop)
    "$PG_BIN/pg_ctl" -D "$PGDATA" -m fast stop ;;
  status)
    "$PG_BIN/pg_ctl" -D "$PGDATA" status ;;
  setup)
    # The app connects as a NON-superuser role: superusers bypass row-level security.
    $PSQL -d postgres <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'projectx') THEN
    CREATE ROLE projectx LOGIN PASSWORD 'projectx' NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS;
  END IF;
END $$;
SQL
    for db in "${DATABASES[@]}"; do ensure_db "$db"; done
    echo "databases ready: ${DATABASES[*]}" ;;
  reset)
    for db in "${DATABASES[@]}"; do "$PG_BIN/dropdb" --if-exists "$db"; done
    "$0" setup ;;
  *)
    echo "usage: $0 start|stop|status|setup|reset" >&2; exit 2 ;;
esac
