#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE"
WRITE=0
SEED=""
PROFILE=""
JOBS="auto"
while [ $# -gt 0 ]; do
  case "$1" in
    --write) WRITE=1; shift ;;
    --seed) SEED="${2:?--seed requires an exact annotated symbol name}"; shift 2 ;;
    --profile) PROFILE="${2:?--profile requires an output path}"; shift 2 ;;
    --jobs) JOBS="${2:?--jobs requires a worker count or auto}"; shift 2 ;;
    --) shift; break ;;
    -*) echo "error: unknown option: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
RULE="${1:-}"
DB="${2:-$HOME/.cache/cidx/index.db}"

[ -n "$RULE" ] || {
  echo "usage: $0 [--seed SYMBOL] [--profile FILE] [--jobs N] [--write] EXAMPLE.dl [index.db]" >&2
  exit 2
}
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

case "$(basename "$RULE")" in
  02_callgraph.dl|03_references.dl|04_hierarchy.dl|05_architecture.dl|06_impact.dl|07_metrics.dl|08_templates.dl|10_writeback.dl)
    [ -n "$SEED" ] || {
      echo "error: $(basename "$RULE") requires --seed with an exact annotated symbol name" >&2
      exit 2
    }
    ;;
esac

if [ -n "$SEED" ]; then
  ESCAPED_SEED="${SEED//\'/\'\'}"
  sqlite3 "$DB_ABS" "DROP VIEW query_seed; CREATE VIEW query_seed AS SELECT name FROM symdisp WHERE name='$ESCAPED_SEED';"
  if [ "$(sqlite3 "$DB_ABS" 'SELECT count(*) FROM query_seed;')" -eq 0 ]; then
    echo "error: no exact annotated symbol named: $SEED" >&2
    echo "candidates:" >&2
    sqlite3 "$DB_ABS" "SELECT '  ' || name FROM symdisp WHERE name LIKE '%$ESCAPED_SEED%' LIMIT 10;" >&2
    exit 3
  fi
fi

ARGS=(-I "$ROOT" -j "$JOBS")
if [ -n "$PROFILE" ]; then
  [[ "$PROFILE" = /* ]] || PROFILE="$PWD/$PROFILE"
  ARGS+=(-p "$PROFILE")
fi
(cd "$(dirname "$DB_ABS")" && souffle "${ARGS[@]}" "$RULE")
