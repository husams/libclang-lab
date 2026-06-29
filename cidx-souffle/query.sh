#!/usr/bin/env bash
# query.sh — answer the three canonical questions over the cidx graph, per symbol, with
# plain READ-ONLY SQL. The recursion is a SQLite recursive CTE over the data ALREADY in
# `edge`/`entity_edge`, anchored on the one symbol you ask about — so it stays bounded
# without any "seed" table and without invoking Soufflé. Nothing is written back: the only
# thing added to the DB is the read-only name VIEWs (symdisp + edge views) from
# cidx_views.sql, created once if missing.
#
#   ./query.sh [-d index.db] reachable <symbol>                  # methods reachable FROM symbol (calls+)
#   ./query.sh [-d index.db] callgraph <symbol> [out|in|both]    # DOT callgraph (default: out)
#   ./query.sh [-d index.db] classes   <symbol>                  # ancestors + descendants (hierarchy)
#
# Symbols use the ANNOTATED names the layer keys on (overloads/instances stay distinct), e.g.
#   ./query.sh callgraph 'app::exercise_cache()'
#   ./query.sh callgraph 'main()' both | dot -Tpng -o cg.png
#   ./query.sh classes   'geo::Shape'
# Tip: list candidate names with    sqlite3 index.db "SELECT name FROM symdisp WHERE name LIKE '%foo%';"
#
# Edge kinds used: calls = edge.kind 1; hierarchy = edge.kind 2 (inherits) ∪
# entity_edge.kind 1 (generalizes), 2 (implements). (Soufflé's run.sh/cidx.dl remain the
# engine for GLOBAL materialization + the future DSL; this script needs none of it.)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DB="$HOME/.cache/cidx/index.db"

while [ "${1:-}" = "-d" ]; do DB="$2"; shift 2; done
CMD="${1:-}"; SYM="${2:-}"; DIR="${3:-out}"
[ -n "$CMD" ] && [ -n "$SYM" ] || { sed -n '2,18p' "$0"; exit 2; }
[ -f "$DB" ] || { echo "error: no index at $DB" >&2; exit 1; }
DB_ABS="$(cd "$(dirname "$DB")" && pwd)/$(basename "$DB")"
ESC="${SYM//\'/\'\'}"   # SQL-escape single quotes

# read-only name views (symdisp etc.); created once if absent, never on every call
if [ "$(sqlite3 "$DB_ABS" "SELECT count(*) FROM sqlite_master WHERE name='symdisp';")" = 0 ]; then
  sqlite3 "$DB_ABS" ".read $HERE/cidx_views.sql"
fi
# validate the symbol exists (exact annotated name); else suggest LIKE candidates
if [ "$(sqlite3 "$DB_ABS" "SELECT count(*) FROM symdisp WHERE name='$ESC';")" = 0 ]; then
  echo "error: no symbol named exactly: $SYM" >&2
  echo "did you mean (LIKE match):" >&2
  sqlite3 "$DB_ABS" "SELECT '  '||name FROM symdisp WHERE name LIKE '%$ESC%' LIMIT 15;" >&2
  exit 3
fi

# recursive CTE that returns caller<TAB>callee edges of a call cone; $1 = out|in
cg_edges() {
  local anchor recurse
  if [ "$1" = in ]; then            # reverse cone: edges that transitively CALL the seed
    anchor="dst_id IN (SELECT id FROM seeds)"; recurse="e.dst_id = c.caller"
  else                              # forward cone: edges the seed transitively CALLS
    anchor="src_id IN (SELECT id FROM seeds)"; recurse="e.src_id = c.callee"
  fi
  sqlite3 -separator $'\t' "$DB_ABS" "
    WITH RECURSIVE
      seeds(id) AS (SELECT id FROM symdisp WHERE name='$ESC'),
      c(caller,callee) AS (
        SELECT src_id,dst_id FROM edge WHERE kind=1 AND $anchor
        UNION
        SELECT e.src_id,e.dst_id FROM edge e JOIN c ON $recurse WHERE e.kind=1)
    SELECT s1.name, s2.name
    FROM c JOIN symdisp s1 ON s1.id=c.caller JOIN symdisp s2 ON s2.id=c.callee;"
}

dot_header() { echo "digraph callgraph {"; echo "  rankdir=LR; node [shape=box, fontname=\"monospace\", fontsize=10];"; echo "  label=\"callgraph $SYM ($1)\"; labelloc=t;"; }
dot_edges()  { awk -F'\t' '{ gsub(/"/,"\\\"",$1); gsub(/"/,"\\\"",$2); printf "  \"%s\" -> \"%s\";\n",$1,$2 }'; }
dot_footer() { local s="${SYM//\"/\\\"}"; echo "  \"$s\" [style=filled, fillcolor=\"#ffe08a\"];"; echo "}"; }

case "$CMD" in
  reachable)
    echo "# methods reachable from: $SYM  (transitive calls)" >&2
    sqlite3 "$DB_ABS" "
      WITH RECURSIVE
        seeds(id) AS (SELECT id FROM symdisp WHERE name='$ESC'),
        r(id) AS (
          SELECT dst_id FROM edge WHERE kind=1 AND src_id IN (SELECT id FROM seeds)
          UNION SELECT e.dst_id FROM edge e JOIN r ON e.src_id=r.id WHERE e.kind=1)
      SELECT DISTINCT sd.name FROM r JOIN symdisp sd ON sd.id=r.id ORDER BY sd.name;"
    ;;
  callgraph)
    case "$DIR" in
      out|in) dot_header "$DIR"; cg_edges "$DIR" | dot_edges; dot_footer ;;
      both)   dot_header both; { cg_edges out; cg_edges in; } | sort -u | dot_edges; dot_footer ;;
      *) echo "error: direction must be out|in|both" >&2; exit 2 ;;
    esac
    ;;
  classes)
    # hierarchy edges = inherits (edge.kind 2) ∪ generalizes/implements (entity_edge.kind 1,2)
    echo "# ancestors (super-classes) of: $SYM"
    sqlite3 "$DB_ABS" "
      WITH RECURSIVE
        seeds(id) AS (SELECT id FROM symdisp WHERE name='$ESC'),
        h(sub,super) AS (
          SELECT src_id,dst_id FROM edge WHERE kind=2
          UNION ALL SELECT src_id,dst_id FROM entity_edge WHERE kind IN (1,2)),
        anc(id) AS (
          SELECT super FROM h WHERE sub IN (SELECT id FROM seeds)
          UNION SELECT h.super FROM h JOIN anc ON h.sub=anc.id)
      SELECT DISTINCT '  -> '||sd.name FROM anc JOIN symdisp sd ON sd.id=anc.id ORDER BY sd.name;"
    echo "# descendants (sub-classes) of: $SYM"
    sqlite3 "$DB_ABS" "
      WITH RECURSIVE
        seeds(id) AS (SELECT id FROM symdisp WHERE name='$ESC'),
        h(sub,super) AS (
          SELECT src_id,dst_id FROM edge WHERE kind=2
          UNION ALL SELECT src_id,dst_id FROM entity_edge WHERE kind IN (1,2)),
        des(id) AS (
          SELECT sub FROM h WHERE super IN (SELECT id FROM seeds)
          UNION SELECT h.sub FROM h JOIN des ON h.super=des.id)
      SELECT DISTINCT '  <- '||sd.name FROM des JOIN symdisp sd ON sd.id=des.id ORDER BY sd.name;"
    ;;
  *) echo "error: unknown command '$CMD' (use reachable|callgraph|classes)" >&2; exit 2 ;;
esac
