#!/usr/bin/env bash
# parity_check.sh — S08 golden parity gate (design §8, label `parity`).
#
# Runs the SAME command script against the Python cidx (via its launcher)
# and the C++ cidx binary, each with its own INDEXER_CACHE, over the
# READ-ONLY fixture libclang-lab/manifests/project/compile_commands.json,
# then diffs:
#   1. every command's stdout + stderr + exit code (cache dirs normalized
#      to {CACHE}, the volatile `indexed at` value to {TS}), and
#   2. `sqlite3 .dump` of both index.db files with the volatile
#      file.mtime / file.indexed_at values excluded (NULLed in a throwaway
#      copy — story S08: "excluding indexed_at/mtime").
# Zero diffs = pass (exit 0). Any missing prerequisite fails loudly.
#
# A1 (Amendment A1 — spec/02-design.md §12, 2026-06-12):
#   cidx-cpp now LINKS libclang at build time.  CIDX_LIBCLANG is a CMake
#   configure-time hint; at runtime the env var is IGNORED by the C++
#   binary (one-shot WARNING logged).  The parity gate therefore:
#     - Does NOT pass CIDX_LIBCLANG to cidx-cpp invocations (unset from
#       env so a stale user export cannot trigger spurious warning lines
#       that break the transcript diff).
#     - Reads the linked-libclang path from the C++ build's CMakeCache.txt
#       (CIDX_LIBCLANG_LIB) and exports it as CIDX_LIBCLANG for the Python
#       side, so both tools index against the SAME library.
#
# Note (D5 / R6): compile_options is stored as a JSON array; Python writes
# '["a", "b"]' and cidx-cpp writes '["a","b"]' — read-compatible but not
# byte-identical for MULTI-element arrays. The dump_db function normalises
# the compile_options column in a throwaway copy (round-trips each value
# through python3 json.loads/dumps with no spaces) before diffing, so the
# diff is byte-strict even when multi-element arrays are present.  A
# synthetic multi-element-args entry (see MULTI_ARGS_DB below) is imported
# alongside the main fixture to exercise this normalisation.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CIDX_CPP_ROOT="$(dirname "$SCRIPT_DIR")"
LAB_ROOT="$(dirname "$CIDX_CPP_ROOT")"
PY_CIDX="$LAB_ROOT/project/cidx"
CPP_BIN="${CIDX_CPP_BIN:-$CIDX_CPP_ROOT/build/cidx}"
FIXTURE_DB="$LAB_ROOT/manifests/project/compile_commands.json"
PROJECT_DIR="$LAB_ROOT/manifests/project"

fail() { echo "parity_check: FAIL: $*" >&2; exit 1; }

# --- prerequisites (fail loudly, never skip silently) ------------------------
[ -x "$CPP_BIN" ] || fail "C++ binary not found/executable: $CPP_BIN (build cidx-cpp first, or set CIDX_CPP_BIN)"
[ -x "$PY_CIDX" ] || fail "Python launcher not found: $PY_CIDX"
[ -f "$FIXTURE_DB" ] || fail "fixture missing: $FIXTURE_DB"
command -v uv >/dev/null 2>&1 || fail "uv not on PATH (the Python launcher needs it)"
command -v sqlite3 >/dev/null 2>&1 || fail "sqlite3 not on PATH"

# --- A1: derive the linked-libclang path from the C++ build ------------------
# The C++ binary links libclang at build time; the path is recorded in the
# build's CMakeCache.txt as CIDX_LIBCLANG_LIB.  We read it back so both
# tools (Python and C++) index against the exact same library.
#
# Resolution order:
#   1. CIDX_LIBCLANG_LIB env var (explicit override for unusual builds).
#   2. CMakeCache.txt adjacent to CPP_BIN (standard cmake build layout).
#   3. Glob: pip-wheel dylib / .so paths (fallback for dev boxes where the
#      cmake build uses the wheel dylib and no cache is available).
_cmake_cache="$(dirname "$(dirname "$CPP_BIN")")/CMakeCache.txt"
if [ -z "${CIDX_LIBCLANG_LIB:-}" ] && [ -f "$_cmake_cache" ]; then
  CIDX_LIBCLANG_LIB="$(grep '^CIDX_LIBCLANG_LIB:FILEPATH=' "$_cmake_cache" \
    | sed 's/^CIDX_LIBCLANG_LIB:FILEPATH=//')"
fi
if [ -z "${CIDX_LIBCLANG_LIB:-}" ]; then
  for cand in /opt/homebrew/lib/python3*/site-packages/clang/native/libclang.dylib \
              /usr/lib/python3*/site-packages/clang/native/libclang.so \
              /usr/local/lib/python3*/site-packages/clang/native/libclang.so; do
    if [ -e "$cand" ]; then
      CIDX_LIBCLANG_LIB="$cand"
      break
    fi
  done
fi
[ -n "${CIDX_LIBCLANG_LIB:-}" ] || fail "cannot determine linked libclang; set CIDX_LIBCLANG_LIB"
[ -e "$CIDX_LIBCLANG_LIB" ]     || fail "CIDX_LIBCLANG_LIB does not exist: $CIDX_LIBCLANG_LIB"
echo "parity_check: C++ binary linked against: $CIDX_LIBCLANG_LIB"

# Python side: CIDX_LIBCLANG must be the same library as C++ links.
# The C++ side: CIDX_LIBCLANG must NOT be set (A1 — runtime env ignored
# with warning, which would add a log line and break the transcript diff).
PY_LIBCLANG="$CIDX_LIBCLANG_LIB"
echo "parity_check: Python will use:            $PY_LIBCLANG"

WORK="$(mktemp -d /tmp/cidx_parity_XXXXXX)" || fail "mktemp failed"
PY_CACHE="$WORK/py-cache"
CPP_CACHE="$WORK/cpp-cache"
mkdir -p "$PY_CACHE" "$CPP_CACHE"

# Synthetic CDB with a multi-element-args entry (R6): exercises compile_options
# arrays with 2+ elements, where Python writes ["a", "b"] (space after comma)
# and C++ writes ["a","b"] (no space).  dump_db normalises both before diff.
MULTI_ARGS_SRC="$WORK/multi_args"
mkdir -p "$MULTI_ARGS_SRC"
cat >"$MULTI_ARGS_SRC/stub.c" <<'EOF'
int stub(void) { return 0; }
EOF
MULTI_ARGS_DB="$WORK/multi_args_cdb.json"
cat >"$MULTI_ARGS_DB" <<EOF
[
  {
    "directory": "$MULTI_ARGS_SRC",
    "command": "cc -I. -DPARITY_R6=1 -DSTUB=1 -c stub.c -o stub.o",
    "file": "stub.c"
  }
]
EOF

# Pre-warm the uv environment so resolver/sync noise never lands in the
# transcript (first `uv run` of a session may print to stderr).
INDEXER_CACHE="$WORK/warm-cache" CIDX_LIBCLANG="$PY_LIBCLANG" \
  "$PY_CIDX" list components >/dev/null 2>&1 || true
rm -rf "$WORK/warm-cache"

# --- transcript runner --------------------------------------------------------
# run_one <transcript> <cache> <is_py:0|1> <tool...> -- <cidx args...>
#   is_py=1 → set CIDX_LIBCLANG=$PY_LIBCLANG for the invocation (Python
#             ctypes needs it to load the library at runtime).
#   is_py=0 → unset CIDX_LIBCLANG so no stale user export reaches cidx-cpp
#             (A1: runtime env ignored with warning → would break diff).
run_one() {
  local transcript=$1 cache=$2 is_py=$3; shift 3
  local -a tool=() args=()
  while [ "$1" != "--" ]; do tool+=("$1"); shift; done
  shift
  args=("$@")
  {
    echo "\$ cidx ${args[*]}"
    local out err rc
    out="$WORK/cmd.out"; err="$WORK/cmd.err"
    if [ "$is_py" = "1" ]; then
      INDEXER_CACHE="$cache" CIDX_LIBCLANG="$PY_LIBCLANG" \
        "${tool[@]}" "${args[@]}" >"$out" 2>"$err"
    else
      INDEXER_CACHE="$cache" \
        env -u CIDX_LIBCLANG "${tool[@]}" "${args[@]}" >"$out" 2>"$err"
    fi
    rc=$?
    # Normalize: per-tool cache dir -> {CACHE}; volatile indexed-at -> {TS}.
    sed -e "s|$cache|{CACHE}|g" \
        -e 's|^indexed at   .* UTC$|indexed at   {TS} UTC|' "$out"
    echo "--- stderr ---"
    sed -e "s|$cache|{CACHE}|g" "$err"
    echo "exit: $rc"
    echo
  } >>"$transcript"
}

# The S08 command script: import, index, second-run skip, FILE-arg skip,
# unknown FILE, search, show symbol (id + USR), show file (path + id), every
# list variant (+ ls alias). --name pins the component name so transcripts
# do not depend on the machine's git remote naming.
#
# is_py: 1 for the Python launcher (CIDX_LIBCLANG injected), 0 for cidx-cpp
# (CIDX_LIBCLANG unset per A1 so no spurious warning line appears).
run_script() {
  local transcript=$1 cache=$2 is_py=$3; shift 3 # remaining: the tool argv prefix
  local -a T=("$@")
  # init: blank DB (fresh), already-exists refusal, --force recreate — all
  # three output strings + exit codes are golden-locked across both tools.
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- init
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- init
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- init --force
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- import --db "$FIXTURE_DB" --name parityproj
  # R6: import a multi-element-args entry so compile_options has 2+ elements.
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- import --db "$MULTI_ARGS_DB" --name multiargscomp
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- index
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- index
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- index "$PROJECT_DIR/app.c"
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- index "$WORK/not-in-db.c"
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- search multiply
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- search a --limit 2
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- search zz
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- search square --kind function
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- show symbol 1
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- show symbol 'c:@F@multiply'
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- show file "$PROJECT_DIR/mathlib.c"
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- show file 1
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- show file 99
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list components
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ls components
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list components --kind external
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list dirs
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list dirs -c parityproj
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list files
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list files --indexed
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list files --pending
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list files app
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list symbols
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list symbols --limit 2
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list symbols sq
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list symbols --kind function
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list symbols -f "$PROJECT_DIR/mathlib.h"
}

echo "parity_check: running Python cidx (cache: $PY_CACHE)"
run_script "$WORK/py.transcript" "$PY_CACHE" "1" "$PY_CIDX"
echo "parity_check: running cidx-cpp  (cache: $CPP_CACHE)"
run_script "$WORK/cpp.transcript" "$CPP_CACHE" "0" "$CPP_BIN"

# --- diff 1: CLI outputs + exit codes ----------------------------------------
if ! diff -u "$WORK/py.transcript" "$WORK/cpp.transcript" >"$WORK/transcript.diff"; then
  echo "parity_check: TRANSCRIPT DIFF (python vs cpp):" >&2
  cat "$WORK/transcript.diff" >&2
  fail "CLI outputs/exit codes differ (work dir kept: $WORK)"
fi
echo "parity_check: transcripts identical ($(grep -c '^\$ cidx' "$WORK/py.transcript") commands)"

# --- diff 2: DB dumps, mtime/indexed_at excluded -------------------------------
# The dump has two parts:
#   schema — every sqlite_master entry, NORMALIZED (SQL comments stripped,
#            whitespace collapsed): Python's _SCHEMA carries inline comments
#            that sqlite stores verbatim in fresh DBs, a cosmetic-text-only
#            delta documented in design §4; the normalized form asserts the
#            full structural contract (columns, CHECKs, FKs, indexes).
#   data   — every INSERT from `sqlite3 .dump`, byte-diffed, after NULLing
#            the volatile file.mtime / file.indexed_at values in a throwaway
#            copy (S08: "excluding indexed_at/mtime").
dump_db() {
  local src=$1 out=$2
  local tmp="$WORK/dump-tmp.db"
  rm -f "$tmp"
  cp "$src" "$tmp"
  # NULL out volatile columns; normalise compile_options JSON arrays to compact
  # form ["a","b"] (no spaces after commas) so the D5 Python-vs-C++ encoding
  # delta does not cause a spurious diff on multi-element arrays (R6).
  sqlite3 "$tmp" "UPDATE file SET mtime = NULL, indexed_at = NULL;" \
    || fail "sqlite3 UPDATE failed on $src"
  # Normalise compile_options JSON arrays: round-trip through python3 with
  # compact separators so Python's ["a", "b"] and C++'s ["a","b"] both become
  # ["a","b"] before the diff.  The python3 snippet is written to a temp file
  # to avoid shell quoting complications with nested quotes/parens.
  local py_norm="$WORK/norm_opts.py"
  cat >"$py_norm" <<'PYEOF'
import json, sys
a = json.load(sys.stdin)
print(json.dumps(a, separators=(",", ":")))
PYEOF
  sqlite3 "$tmp" \
    "SELECT id, compile_options FROM file WHERE compile_options IS NOT NULL;" \
    | while IFS='|' read -r fid opts; do
        norm=$(printf '%s' "$opts" | python3 "$py_norm")
        # Escape single quotes in norm for safe SQL embedding.
        norm_escaped="${norm//\'/\'\'}"
        sqlite3 "$tmp" "UPDATE file SET compile_options = '$norm_escaped' WHERE id = $fid;"
      done
  {
    echo "-- schema (normalized) --"
    # One normalized line per sqlite_master row: ';;' row separator survives
    # the whitespace fold (multi-line CREATE bodies fold flat).
    sqlite3 "$tmp" \
      "SELECT type || '|' || name || '|' || COALESCE(sql, '') || ' ;;' FROM sqlite_master ORDER BY type, name;" \
      | sed -e 's/--.*$//' | tr -s ' \n\t' ' ' | tr ';' '\n'
    echo "-- data --"
    sqlite3 "$tmp" .dump | grep '^INSERT'
  } >"$out" || fail "sqlite3 dump failed on $src"
  rm -f "$tmp"
}
dump_db "$PY_CACHE/index.db" "$WORK/py.dump"
dump_db "$CPP_CACHE/index.db" "$WORK/cpp.dump"

if ! diff -u "$WORK/py.dump" "$WORK/cpp.dump" >"$WORK/dump.diff"; then
  echo "parity_check: DB DUMP DIFF (python vs cpp):" >&2
  cat "$WORK/dump.diff" >&2
  fail "index.db dumps differ (work dir kept: $WORK)"
fi
echo "parity_check: DB dumps identical (mtime/indexed_at excluded)"

rm -rf "$WORK"
echo "parity_check: PASS"
