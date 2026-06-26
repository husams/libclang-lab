#!/usr/bin/env bash
# run.sh — apply the cidx Soufflé query layer to a COPY of the cidx index.
#
#   ./run.sh [path/to/index.db]      (default: ~/.cache/cidx/index.db)
#
# Copies the index to build/graph.db (the canonical DB is NEVER mutated), creates the
# adapter views, runs Soufflé, and reports the materialized derived relations.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${1:-$HOME/.cache/cidx/index.db}"
BUILD="$HERE/build"

command -v souffle >/dev/null || { echo "error: souffle not on PATH"; exit 1; }
[ -f "$SRC" ] || { echo "error: no index at $SRC"; exit 1; }

mkdir -p "$BUILD"
cp "$SRC" "$BUILD/graph.db"                       # work on a copy, never the real index
sqlite3 "$BUILD/graph.db" < "$HERE/cidx_views.sql"
( cd "$BUILD" && souffle "$HERE/cidx.dl" )        # IO=sqlite dbname is relative to cwd

echo "── materialized into $BUILD/graph.db ──"
sqlite3 "$BUILD/graph.db" \
  "SELECT 'reachable',      count(*) FROM reachable
   UNION ALL SELECT 'subtype_named',  count(*) FROM subtype_named
   UNION ALL SELECT 'entity_depends', count(*) FROM entity_depends;"
