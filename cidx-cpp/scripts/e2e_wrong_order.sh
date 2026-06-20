#!/usr/bin/env bash
# e2e_wrong_order.sh — cross-TU wrong-order indexing e2e (v0.14.2 regression).
#
# Proves, end-to-end through the CLI, that a dependent member-template call
# whose DEFINING TU is indexed AFTER the consuming TU still resolves — and that
# the Python (uv) and C++ binaries produce byte-identical `graph callees`
# output. This is the real-codebase complement to the in-process ctest/pytest
# cases (ast_test.cpp / test_member_template_edges.py); e2e_librdkafka.sh does
# NOT exercise this path because librdkafka is C.
#
# Default corpus: manifests/graphlab's Cache (member templates set<T>/get<T>) +
# UseCache (cache_roundtrip<T>), copied into a hermetic workspace and SPLIT into
# two component directories so the cache header is UNOWNED when the consumer is
# indexed first:
#
#   WS/use/   UseCache.hpp (cache_roundtrip<T>), UseCache.cpp   -> component `use`
#   WS/lib/   cache.hpp (Cache::set/get), cache.cpp             -> component `lib`
#
# Flow (per tool, own INDEXER_CACHE):
#   init -> import use -> index            # cache.hpp unowned -> set/get become
#                                          #   USR-keyed stubs (the v0.14.2 fix)
#        -> import lib -> index -> resolve # cache TU backfills the same USRs
#        -> graph callees cache_roundtrip  # must list Cache::set + Cache::get,
#                                          #   is_stub=false
#
# Assertions:
#   1. BEFORE the cache TU: callees already include Cache::set/get (as stubs) —
#      order-independence depends on the consumer minting them.
#   2. AFTER cache TU + resolve: callees are Cache::set/get with is_stub=false.
#   3. Python and C++ `graph callees --json` are byte-identical (both BEFORE and
#      AFTER), over the SAME workspace paths.
#
# Hermetic: each tool gets its own INDEXER_CACHE; the workspace is a mktemp dir;
# manifests/ is read-only (sources are COPIED). Exit 0 = pass; any failure is
# loud and keeps the work dir.
#
# Parameterization (optional, for a larger real repo):
#   e2e_wrong_order.sh CONSUMER_CDB DEFINING_CDB SYMBOL [KIND]
#     CONSUMER_CDB  compile_commands.json whose TU(s) CALL the templates but do
#                   NOT own the defining header (it must be unowned at index).
#     DEFINING_CDB  compile_commands.json whose TU(s) own the defining header.
#     SYMBOL        --name argument for `graph callees` (e.g. app::cache_roundtrip)
#     KIND          optional --kind filter (default: function-template)
#   The committed default (no args) is graphlab, so CI is self-contained.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CIDX_CPP_ROOT="$(dirname "$SCRIPT_DIR")"
LAB_ROOT="$(dirname "$CIDX_CPP_ROOT")"
PY_CIDX="$LAB_ROOT/project/cidx"
CPP_BIN="${CIDX_CPP_BIN:-$CIDX_CPP_ROOT/build/cidx}"
GRAPHLAB="$LAB_ROOT/manifests/graphlab"

fail() { echo "e2e_wrong_order: FAIL: $*" >&2; exit 1; }

# --- prerequisites (fail loudly, never skip silently) ------------------------
[ -x "$CPP_BIN" ] || fail "C++ binary not found/executable: $CPP_BIN (build cidx-cpp first, or set CIDX_CPP_BIN)"
[ -x "$PY_CIDX" ] || fail "Python launcher not found: $PY_CIDX"
command -v uv >/dev/null 2>&1 || fail "uv not on PATH (the Python launcher needs it)"
command -v jq >/dev/null 2>&1 || JQ=""  # jq optional; pure-grep fallback below
command -v jq >/dev/null 2>&1 && JQ="jq"

# --- A1: derive the linked-libclang path so both tools use the SAME library --
# Mirrors parity_check.sh: the C++ binary links libclang at build time; the
# Python side needs CIDX_LIBCLANG pointed at the SAME dylib for byte parity.
_cmake_cache="$(dirname "$(dirname "$CPP_BIN")")/CMakeCache.txt"
if [ -z "${CIDX_LIBCLANG_LIB:-}" ] && [ -f "$_cmake_cache" ]; then
  CIDX_LIBCLANG_LIB="$(grep '^CIDX_LIBCLANG_LIB:FILEPATH=' "$_cmake_cache" \
    | sed 's/^CIDX_LIBCLANG_LIB:FILEPATH=//')"
fi
if [ -z "${CIDX_LIBCLANG_LIB:-}" ]; then
  for cand in /opt/homebrew/lib/python3*/site-packages/clang/native/libclang.dylib \
              /usr/lib/python3*/site-packages/clang/native/libclang.so \
              /usr/local/lib/python3*/site-packages/clang/native/libclang.so; do
    [ -e "$cand" ] && { CIDX_LIBCLANG_LIB="$cand"; break; }
  done
fi
[ -n "${CIDX_LIBCLANG_LIB:-}" ] || fail "cannot determine linked libclang; set CIDX_LIBCLANG_LIB"
[ -e "$CIDX_LIBCLANG_LIB" ]     || fail "CIDX_LIBCLANG_LIB does not exist: $CIDX_LIBCLANG_LIB"
PY_LIBCLANG="$CIDX_LIBCLANG_LIB"
echo "e2e_wrong_order: libclang (both tools): $PY_LIBCLANG"

WORK="$(mktemp -d /tmp/cidx_wrong_order_XXXXXX)" || fail "mktemp failed"

# --- build the corpus --------------------------------------------------------
SYMBOL="${3:-app::cache_roundtrip}"
KIND="${4:-function-template}"
if [ -n "${1:-}" ] && [ -n "${2:-}" ]; then
  CONSUMER_CDB="$1"
  DEFINING_CDB="$2"
  [ -f "$CONSUMER_CDB" ] || fail "consumer CDB not found: $CONSUMER_CDB"
  [ -f "$DEFINING_CDB" ] || fail "defining CDB not found: $DEFINING_CDB"
  echo "e2e_wrong_order: corpus = caller=$CONSUMER_CDB definer=$DEFINING_CDB symbol=$SYMBOL"
else
  # Default: hermetic graphlab split.
  [ -d "$GRAPHLAB" ] || fail "graphlab corpus missing: $GRAPHLAB"
  USE_DIR="$WORK/use"
  LIB_DIR="$WORK/lib"
  mkdir -p "$USE_DIR" "$LIB_DIR"
  cp "$GRAPHLAB/UseCache.hpp" "$GRAPHLAB/UseCache.cpp" "$USE_DIR/" \
    || fail "cannot copy UseCache sources"
  cp "$GRAPHLAB/cache.hpp" "$LIB_DIR/" || fail "cannot copy cache.hpp"
  # cache.cpp: a TU owned by the lib component whose only job is to pull
  # cache.hpp into an owned TU so Cache::set/get get real (resolved) symbols.
  printf '#include "cache.hpp"\n' > "$LIB_DIR/cache.cpp"
  # UseCache.hpp does #include "cache.hpp"; -Ilib resolves it to the UNOWNED
  # header. -Iuse resolves UseCache.cpp's #include "UseCache.hpp".
  cat > "$USE_DIR/compile_commands.json" <<EOF
[
  {
    "directory": "$USE_DIR",
    "command": "c++ -I$USE_DIR -I$LIB_DIR -std=c++17 -c UseCache.cpp -o UseCache.o",
    "file": "UseCache.cpp"
  }
]
EOF
  cat > "$LIB_DIR/compile_commands.json" <<EOF
[
  {
    "directory": "$LIB_DIR",
    "command": "c++ -I$LIB_DIR -std=c++17 -c cache.cpp -o cache.o",
    "file": "cache.cpp"
  }
]
EOF
  CONSUMER_CDB="$USE_DIR/compile_commands.json"
  DEFINING_CDB="$LIB_DIR/compile_commands.json"
  echo "e2e_wrong_order: corpus = graphlab split (use/ + lib/), symbol=$SYMBOL"
fi

# --- per-tool runners --------------------------------------------------------
# run_py / run_cpp <cache> <args...>: invoke the tool with the right libclang
# env (Python needs CIDX_LIBCLANG; C++ must NOT see it — A1).
run_py()  { INDEXER_CACHE="$1" CIDX_LIBCLANG="$PY_LIBCLANG" "$PY_CIDX" "${@:2}"; }
run_cpp() { INDEXER_CACHE="$1" env -u CIDX_LIBCLANG "$CPP_BIN" "${@:2}"; }

# callees_args: the `graph callees` argv (with optional --kind).
callees_args=(graph callees --name "$SYMBOL" --json)
[ -n "$KIND" ] && callees_args+=(--kind "$KIND")

# drive_consumer_first <run-fn> <cache> <before.json> <after.json>
# Runs the full wrong-order flow, capturing callees BEFORE and AFTER the cache TU.
drive_consumer_first() {
  local runfn=$1 cache=$2 before=$3 after=$4
  mkdir -p "$cache"
  "$runfn" "$cache" init                 >/dev/null 2>"$WORK/err" || fail "$runfn init failed: $(cat "$WORK/err")"
  "$runfn" "$cache" import --db "$CONSUMER_CDB" --name use >/dev/null 2>"$WORK/err" || fail "$runfn import use failed: $(cat "$WORK/err")"
  "$runfn" "$cache" index                >/dev/null 2>"$WORK/err" || fail "$runfn index(use) failed: $(cat "$WORK/err")"
  "$runfn" "$cache" "${callees_args[@]}" >"$before" 2>"$WORK/err" || fail "$runfn callees(before) failed: $(cat "$WORK/err")"
  "$runfn" "$cache" import --db "$DEFINING_CDB" --name lib >/dev/null 2>"$WORK/err" || fail "$runfn import lib failed: $(cat "$WORK/err")"
  "$runfn" "$cache" index                >/dev/null 2>"$WORK/err" || fail "$runfn index(lib) failed: $(cat "$WORK/err")"
  "$runfn" "$cache" resolve              >/dev/null 2>"$WORK/err" || fail "$runfn resolve failed: $(cat "$WORK/err")"
  "$runfn" "$cache" "${callees_args[@]}" >"$after"  2>"$WORK/err" || fail "$runfn callees(after) failed: $(cat "$WORK/err")"
}

# Pre-warm uv so resolver noise never lands in captured output.
run_py "$WORK/warm" list components >/dev/null 2>&1 || true
rm -rf "$WORK/warm"

echo "e2e_wrong_order: driving Python (uv) consumer-first..."
drive_consumer_first run_py  "$WORK/py"  "$WORK/py.before.json"  "$WORK/py.after.json"
echo "e2e_wrong_order: driving C++ consumer-first..."
drive_consumer_first run_cpp "$WORK/cpp" "$WORK/cpp.before.json" "$WORK/cpp.after.json"

# --- assertion helpers -------------------------------------------------------
# has_callee <json-file> <qual_name>: 1 if a result row has that qual_name.
has_callee() {
  if [ -n "$JQ" ]; then
    [ "$($JQ --arg q "$2" '[.[] | select(.qual_name == $q)] | length' "$1")" -gt 0 ]
  else
    grep -q "\"qual_name\": \"$2\"" "$1"
  fi
}
# callee_is_stub <json-file> <qual_name>: 1 if that row has is_stub == true.
callee_is_stub() {
  if [ -n "$JQ" ]; then
    [ "$($JQ --arg q "$2" '[.[] | select(.qual_name == $q and .is_stub == true)] | length' "$1")" -gt 0 ]
  else
    # grep fallback: crude proximity check on the flat pretty-printed JSON.
    awk -v q="\"qual_name\": \"$2\"" '
      $0 ~ q {seen=1}
      seen && /"is_stub": true/ {found=1}
      seen && /^  }/ {seen=0}
      END {exit found?0:1}
    ' "$1"
  fi
}

DEFINER_SET="${SET_QUAL:-app::Cache::set}"
DEFINER_GET="${GET_QUAL:-app::Cache::get}"

# --- 1. BEFORE the cache TU: callees already present (as stubs) ---------------
for f in "$WORK/py.before.json" "$WORK/cpp.before.json"; do
  has_callee "$f" "$DEFINER_SET" || fail "BEFORE: $DEFINER_SET missing in $(basename "$f") — consumer did not mint the stub (order-dependence regression)"
  has_callee "$f" "$DEFINER_GET" || fail "BEFORE: $DEFINER_GET missing in $(basename "$f")"
done
# Default corpus: assert they are genuinely UNRESOLVED stubs before the cache TU.
if [ -z "${1:-}" ]; then
  callee_is_stub "$WORK/cpp.before.json" "$DEFINER_SET" || fail "BEFORE: $DEFINER_SET should be an unresolved stub (C++)"
  callee_is_stub "$WORK/py.before.json"  "$DEFINER_SET" || fail "BEFORE: $DEFINER_SET should be an unresolved stub (Python)"
fi
echo "e2e_wrong_order: BEFORE — callees include $DEFINER_SET / $DEFINER_GET as stubs (order-independence)"

# --- 2. AFTER cache TU + resolve: present and NOT stubs -----------------------
for f in "$WORK/py.after.json" "$WORK/cpp.after.json"; do
  has_callee "$f" "$DEFINER_SET" || fail "AFTER: $DEFINER_SET missing in $(basename "$f")"
  has_callee "$f" "$DEFINER_GET" || fail "AFTER: $DEFINER_GET missing in $(basename "$f")"
  if [ -z "${1:-}" ]; then
    callee_is_stub "$f" "$DEFINER_SET" && fail "AFTER: $DEFINER_SET still a stub in $(basename "$f") — backfill failed"
    callee_is_stub "$f" "$DEFINER_GET" && fail "AFTER: $DEFINER_GET still a stub in $(basename "$f") — backfill failed"
  fi
done
echo "e2e_wrong_order: AFTER  — callees resolved to $DEFINER_SET / $DEFINER_GET (backfill OK)"

# --- 3. Py <-> C++ byte-identical (both phases) ------------------------------
if ! diff -u "$WORK/py.before.json" "$WORK/cpp.before.json" >"$WORK/before.diff"; then
  echo "e2e_wrong_order: BEFORE Py-vs-C++ DIFF:" >&2; cat "$WORK/before.diff" >&2
  fail "callees(before) differ between Python and C++ (work dir kept: $WORK)"
fi
if ! diff -u "$WORK/py.after.json" "$WORK/cpp.after.json" >"$WORK/after.diff"; then
  echo "e2e_wrong_order: AFTER Py-vs-C++ DIFF:" >&2; cat "$WORK/after.diff" >&2
  fail "callees(after) differ between Python and C++ (work dir kept: $WORK)"
fi
echo "e2e_wrong_order: Python and C++ \`graph callees\` output byte-identical"

rm -rf "$WORK"
echo "e2e_wrong_order: PASS"
