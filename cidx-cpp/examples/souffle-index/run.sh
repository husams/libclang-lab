#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE"
WRITE=0
if [ "${1:-}" = "--write" ]; then WRITE=1; shift; fi
RULE="${1:-}"
DB="${2:-$HOME/.cache/index.db}"

[ -n "$RULE" ] || { echo "usage: $0 [--write] EXAMPLE.dl [index.db]" >&2; exit 2; }
[[ "$RULE" = /* ]] || RULE="$HERE/$RULE"
[ -f "$RULE" ] || { echo "error: no rule file: $RULE" >&2; exit 1; }
[ -f "$DB" ] || { echo "error: no index: $DB" >&2; exit 1; }
command -v souffle >/dev/null || { echo "error: souffle not on PATH" >&2; exit 1; }
command -v sqlite3 >/dev/null || { echo "error: sqlite3 not on PATH" >&2; exit 1; }

if [ "$(basename "$RULE")" = "10_writeback.dl" ] && [ "$WRITE" -ne 1 ]; then
  echo "error: 10_writeback.dl modifies the index; rerun with --write" >&2
  exit 2
fi

DB_ABS="$(cd "$(dirname "$DB")" && pwd)/$(basename "$DB")"
[ "$(basename "$DB_ABS")" = "index.db" ] || {
  echo "error: Souffle rules expect the database to be named index.db: $DB_ABS" >&2
  exit 2
}
sqlite3 "$DB_ABS" ".read $ROOT/cidx_views.sql"

(cd "$(dirname "$DB_ABS")" && souffle -I "$ROOT" "$RULE")
