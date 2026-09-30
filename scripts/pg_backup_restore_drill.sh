#!/usr/bin/env bash
# PostgreSQL-only logical backup + disposable restore verification.
# This script never restores over the source database. CR_PG_DSN may contain a
# password; Python moves it to PGPASSWORD for pg_* children and never prints it.
set -euo pipefail

: "${CR_PG_DSN:?set CR_PG_DSN to the PostgreSQL source DSN}"
: "${CR_PG_SCHEMA:=runtime_v2}"
: "${CR_PG_BACKUP_DIR:=./backups/postgres}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="${CR_PG_BACKUP_DIR}/companion-${stamp}.dump"
manifest="${archive}.manifest.json"
mkdir -p -- "${CR_PG_BACKUP_DIR}"

python -m companion_runtime.postgres_ops backup \
  --dsn "${CR_PG_DSN}" \
  --schema "${CR_PG_SCHEMA}" \
  --archive "${archive}" \
  --manifest "${manifest}"

python -m companion_runtime.postgres_ops drill \
  --dsn "${CR_PG_DSN}" \
  --archive "${archive}" \
  --manifest "${manifest}"

printf 'backup and disposable restore drill succeeded: %s\n' "${manifest}"
