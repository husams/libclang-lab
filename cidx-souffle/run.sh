#!/usr/bin/env bash
# run.sh — run the cidx Soufflé reasoning layer IN PLACE on the index. No copy, no
# separate database: integer VIEWs are created directly in index.db and Soufflé writes
# its result tables (subtype, edep, reach) back into the SAME index.db.
#
#   ./run.sh [path/to/index.db] [seed_name_substring]
#
# The index is used as-is. Soufflé's sqlite directive needs the file named `index.db`
# in its working dir, so we run it through a symlink in build/ that points at the real
# file — the file itself is never moved or copied. Only the `edge`/`entity_edge` tables
# are read (via the views), never the millions of `symbol` rows.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${1:-$HOME/.cache/cidx/index.db}"
SEED="${2:-}"
BUILD="$HERE/build"

command -v souffle >/dev/null || { echo "error: souffle not on PATH"; exit 1; }
[ -f "$SRC" ] || { echo "error: no index at $SRC"; exit 1; }
SRC_ABS="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"

mkdir -p "$BUILD"
ln -sf "$SRC_ABS" "$BUILD/index.db"               # symlink — Soufflé reads/writes the REAL file

echo "── creating views + seed in $SRC_ABS (in place) ──"
SEED_SQL=""
[ -n "$SEED" ] && SEED_SQL="INSERT INTO seed SELECT name FROM symdisp WHERE name LIKE '%$SEED%';"
sqlite3 "$SRC_ABS" <<SQL
.read $HERE/cidx_views.sql
$SEED_SQL
SQL

echo "── running soufflé (writes results into the same index.db) ──"
time ( cd "$BUILD" && souffle "$HERE/cidx.dl" )

echo "── result tables now live in $SRC_ABS (names, not ids) ──"
sqlite3 "$SRC_ABS" \
  "SELECT 'subtype', count(*) FROM subtype
   UNION ALL SELECT 'edep', count(*) FROM edep
   UNION ALL SELECT 'reach(seeded)', count(*) FROM reach;"
echo "results already carry names, e.g.:"
echo "  sqlite3 $SRC_ABS \"SELECT b FROM reach WHERE a='X::func' LIMIT 20;\""
