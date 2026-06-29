#!/usr/bin/env bash
# query.sh — answer the three canonical questions over the cidx Soufflé layer, per symbol.
# Wraps the engine (cidx.dl) with EXACT per-symbol seeding so you ask about one function /
# method / class at a time. Reads/writes the index IN PLACE via a build/ symlink (same as
# run.sh) — it only adds the views + result/seed tables, never copies the DB.
#
#   ./query.sh [-d index.db] reachable <symbol>            # methods reachable FROM symbol (calls+)
#   ./query.sh [-d index.db] callgraph <symbol> [out|in|both]   # DOT callgraph (default: out)
#   ./query.sh [-d index.db] classes   <symbol>            # ancestors + descendants (hierarchy)
#
# Symbols use the ANNOTATED names the layer keys on (overloads/instances stay distinct), e.g.
#   ./query.sh callgraph 'app::exercise_cache()'
#   ./query.sh callgraph 'rd_kafka_produceva' both | dot -Tpng -o cg.png
#   ./query.sh reachable 'main() @main.cpp:23'
#   ./query.sh classes   'geo::Shape'
# Tip: list candidate names with    sqlite3 index.db "SELECT name FROM symdisp WHERE name LIKE '%foo%';"
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/build"
DB="$HOME/.cache/cidx/index.db"

while [ "${1:-}" = "-d" ]; do DB="$2"; shift 2; done
CMD="${1:-}"; SYM="${2:-}"; DIR="${3:-out}"
[ -n "$CMD" ] && [ -n "$SYM" ] || { sed -n '2,20p' "$0"; exit 2; }
command -v souffle >/dev/null || { echo "error: souffle not on PATH" >&2; exit 1; }
[ -f "$DB" ] || { echo "error: no index at $DB" >&2; exit 1; }
DB_ABS="$(cd "$(dirname "$DB")" && pwd)/$(basename "$DB")"
ESC="${SYM//\'/\'\'}"   # SQL-escape single quotes

reset_outputs() {  # type-agnostic drop of output + seed objects (may be table OR view)
  local drops
  drops="$(sqlite3 "$DB_ABS" "SELECT 'DROP '||type||' IF EXISTS \"'||name||'\";' FROM sqlite_master WHERE name IN ('subtype','edep','reach','cg_out','cg_in','seed');")"
  [ -n "$drops" ] && sqlite3 "$DB_ABS" "$drops"
  sqlite3 "$DB_ABS" "CREATE TABLE seed(x TEXT);"
}

run_engine() {  # build views, seed EXACTLY this symbol, run the full engine in place
  mkdir -p "$BUILD"; ln -sf "$DB_ABS" "$BUILD/index.db"
  sqlite3 "$DB_ABS" ".read $HERE/cidx_views.sql"
  reset_outputs
  sqlite3 "$DB_ABS" "INSERT INTO seed SELECT name FROM symdisp WHERE name='$ESC';"
  local n; n="$(sqlite3 "$DB_ABS" 'SELECT count(*) FROM seed;')"
  if [ "$n" = 0 ]; then
    echo "error: no symbol named exactly: $SYM" >&2
    echo "did you mean (LIKE match):" >&2
    sqlite3 "$DB_ABS" "SELECT '  '||name FROM symdisp WHERE name LIKE '%$ESC%' LIMIT 15;" >&2
    exit 3
  fi
  ( cd "$BUILD" && souffle "$HERE/cidx.dl" ) >/dev/null
}

emit_dot() {  # $1 = source table (cg_out|cg_in); reads a|b edges, writes Graphviz DOT
  echo "digraph callgraph {"
  echo "  rankdir=LR; node [shape=box, fontname=\"monospace\", fontsize=10];"
  echo "  label=\"$CMD $SYM ($DIR)\"; labelloc=t;"
  sqlite3 -separator $'\t' "$DB_ABS" "SELECT caller,callee FROM $1;" | awk -F'\t' '
    { gsub(/"/,"\\\"",$1); gsub(/"/,"\\\"",$2); printf "  \"%s\" -> \"%s\";\n",$1,$2 }'
  # highlight the seed node
  s="$SYM"; s="${s//\"/\\\"}"; echo "  \"$s\" [style=filled, fillcolor=\"#ffe08a\"];"
  echo "}"
}

case "$CMD" in
  reachable)
    run_engine
    echo "# methods reachable from: $SYM  (transitive calls)" >&2
    sqlite3 "$DB_ABS" "SELECT DISTINCT b FROM reach ORDER BY b;"
    ;;
  callgraph)
    run_engine
    case "$DIR" in
      out)  emit_dot cg_out ;;
      in)   emit_dot cg_in ;;
      both)
        echo "digraph callgraph {"
        echo "  rankdir=LR; node [shape=box, fontname=\"monospace\", fontsize=10];"
        echo "  label=\"callgraph $SYM (both)\"; labelloc=t;"
        sqlite3 -separator $'\t' "$DB_ABS" \
          "SELECT caller,callee FROM cg_out UNION SELECT caller,callee FROM cg_in;" | awk -F'\t' '
          { gsub(/"/,"\\\"",$1); gsub(/"/,"\\\"",$2); printf "  \"%s\" -> \"%s\";\n",$1,$2 }'
        s="$SYM"; s="${s//\"/\\\"}"; echo "  \"$s\" [style=filled, fillcolor=\"#ffe08a\"];"
        echo "}"
        ;;
      *) echo "error: direction must be out|in|both" >&2; exit 2 ;;
    esac
    ;;
  classes)
    run_engine
    echo "# ancestors (super-classes) of: $SYM"
    sqlite3 "$DB_ABS" "SELECT '  -> '||super FROM subtype WHERE sub='$ESC' ORDER BY super;"
    echo "# descendants (sub-classes) of: $SYM"
    sqlite3 "$DB_ABS" "SELECT '  <- '||sub FROM subtype WHERE super='$ESC' ORDER BY sub;"
    ;;
  *) echo "error: unknown command '$CMD' (use reachable|callgraph|classes)" >&2; exit 2 ;;
esac
