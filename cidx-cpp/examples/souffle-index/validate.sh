#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$HERE/run.sh"

if [ -n "${1:-}" ]; then
  DB="$1"
elif [ -n "${CIDX_DB:-}" ]; then
  DB="$CIDX_DB"
else
  DB="$HOME/.cache/cidx/index.db"
fi

[ -f "$DB" ] || { echo "error: no index: $DB" >&2; exit 1; }
DB="$(cd "$(dirname "$DB")" && pwd)/$(basename "$DB")"
[ "$(basename "$DB")" = index.db ] || {
  echo "error: database must be named index.db: $DB" >&2
  exit 2
}

OUT="$(mktemp -d "${TMPDIR:-/tmp}/cidx-souffle-validation.XXXXXX")"
trap 'rm -rf "$OUT"' EXIT

run_case() {
  local number="$1" seed="$2" rule="$3"
  if [ -n "$seed" ]; then
    "$RUN" --seed "$seed" "$rule" "$DB" >"$OUT/$number.out"
  else
    "$RUN" "$rule" "$DB" >"$OUT/$number.out"
  fi
  printf 'PASS %s %-24s rows=%s' "$number" "$rule" "$(wc -l <"$OUT/$number.out" | tr -d ' ')"
  [ -n "$seed" ] && printf ' seed=%s' "$seed"
  printf '\n'
}

# 01 also creates/recreates all adapter views used to select exact seeds.
run_case 01 "" 01_inventory.dl

call_source="$(sqlite3 "$DB" 'SELECT a FROM calls ORDER BY a LIMIT 1;')"
reference_target="$(sqlite3 "$DB" 'SELECT b FROM calls UNION SELECT b FROM uses ORDER BY 1 LIMIT 1;')"
hierarchy_seed="$(sqlite3 "$DB" 'SELECT a FROM inherits UNION SELECT a FROM e_generalizes UNION SELECT a FROM e_implements UNION SELECT a FROM overrides ORDER BY 1 LIMIT 1;')"
architecture_seed="$(sqlite3 "$DB" 'SELECT a FROM e_uses UNION SELECT a FROM e_creates UNION SELECT a FROM e_composes UNION SELECT a FROM e_aggregates UNION SELECT a FROM e_associates ORDER BY 1 LIMIT 1;')"
impact_seed="$(sqlite3 "$DB" 'SELECT b FROM calls ORDER BY b LIMIT 1;')"
metric_seed="$(sqlite3 "$DB" 'SELECT name FROM symdisp ORDER BY name LIMIT 1;')"
template_seed="$(sqlite3 "$DB" 'SELECT a FROM instantiates ORDER BY a LIMIT 1;')"
IFS=$'\t' read -r path_source path_target <<EOF
$(sqlite3 -separator $'\t' "$DB" 'SELECT a,b FROM calls WHERE a IN (SELECT name FROM callable_fact) AND b IN (SELECT name FROM callable_fact) ORDER BY a,b LIMIT 1;')
EOF

for required in call_source reference_target hierarchy_seed architecture_seed impact_seed metric_seed template_seed path_source path_target; do
  [ -n "${!required}" ] || {
    echo "error: index has no suitable seed for $required" >&2
    exit 4
  }
done

"$RUN" --seed "$call_source" --profile "$OUT/02-profile.json" \
  02_callgraph.dl "$DB" >"$OUT/02.out"
test -s "$OUT/02-profile.json"
printf 'PASS 02 %-24s rows=%s seed=%s profile=yes\n' \
  02_callgraph.dl "$(wc -l <"$OUT/02.out" | tr -d ' ')" "$call_source"

run_case 03 "$reference_target" 03_references.dl
run_case 04 "$hierarchy_seed" 04_hierarchy.dl
run_case 05 "$architecture_seed" 05_architecture.dl
run_case 06 "$impact_seed" 06_impact.dl
run_case 07 "$metric_seed" 07_metrics.dl
run_case 08 "$template_seed" 08_templates.dl
run_case 09 "" 09_cross_file.dl

"$RUN" --seed "$path_source" --target "$path_target" \
  11_all_paths.dl "$DB" >"$OUT/11.out"
printf 'PASS 11 %-24s rows=%s source=%s target=%s\n' \
  11_all_paths.dl "$(wc -l <"$OUT/11.out" | tr -d ' ')" \
  "$path_source" "$path_target"

# Write-back is validated only against a disposable copy.
WRITE_DIR="$OUT/writeback"
mkdir -p "$WRITE_DIR"
cp "$DB" "$WRITE_DIR/index.db"
"$RUN" --write --seed "$impact_seed" 10_writeback.dl \
  "$WRITE_DIR/index.db" >"$OUT/10.out"
sqlite3 "$WRITE_DIR/index.db" 'SELECT count(*) FROM souffle_impact;' \
  >"$OUT/10-count"
printf 'PASS 10 %-24s rows=%s seed=%s disposable=yes\n' \
  10_writeback.dl "$(cat "$OUT/10-count")" "$impact_seed"

echo "All Souffle index examples passed: $DB"
