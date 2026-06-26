#!/usr/bin/env bash
# run.sh — apply the cidx Soufflé reasoning layer WITHOUT copying the index.
#
#   ./run.sh [path/to/index.db] [seed_name_substring]
#
# Reads the (possibly multi-GiB / 12M-symbol) index READ-ONLY, projects only the small
# integer edge tables into build/graph.db, runs Soufflé over integer ids, then joins names
# back onto the small result rows. The canonical index is NEVER opened read-write.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${1:-$HOME/.cache/cidx/index.db}"
SEED="${2:-}"                                    # optional: seed reach() from symbols whose name matches
BUILD="$HERE/build"

command -v souffle >/dev/null || { echo "error: souffle not on PATH"; exit 1; }
[ -f "$SRC" ] || { echo "error: no index at $SRC"; exit 1; }
SRC_ABS="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"

mkdir -p "$BUILD"
rm -f "$BUILD/graph.db"

echo "── projecting edges (read-only) from $SRC_ABS ──"
SEED_SQL=""
[ -n "$SEED" ] && SEED_SQL="INSERT INTO seed SELECT id FROM src.symbol WHERE qual_name LIKE '%$SEED%' OR spelling LIKE '%$SEED%';"
time sqlite3 "$BUILD/graph.db" <<SQL
ATTACH 'file:$SRC_ABS?immutable=1' AS src;
.read $HERE/project.sql
CREATE TABLE seed(x INTEGER);
$SEED_SQL
SQL

echo "── running soufflé (integer ids) ──"
time ( cd "$BUILD" && souffle "$HERE/cidx.dl" )

echo "── resolving names on result rows (read-only join) ──"
sqlite3 "$BUILD/graph.db" \
  "ATTACH 'file:$SRC_ABS?immutable=1' AS src;
   SELECT 'subtype', count(*) FROM subtype
   UNION ALL SELECT 'edep', count(*) FROM edep
   UNION ALL SELECT 'reach(seeded)', count(*) FROM reach;"
echo "name a result e.g.:  sqlite3 $BUILD/graph.db \"ATTACH 'file:$SRC_ABS?immutable=1' AS src; SELECT sa.qual_name, sb.qual_name FROM reach r JOIN src.symbol sa ON sa.id=r.a JOIN src.symbol sb ON sb.id=r.b LIMIT 20;\""
