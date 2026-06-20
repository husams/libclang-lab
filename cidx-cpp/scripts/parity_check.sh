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
# Single unified compile DB at manifests/ (sub-project DBs were consolidated).
# All scenarios import the same DB under different component names; the
# geometry/graphlab edge + call_arg assertions stay exercised because those
# TUs live in the unified DB.
FIXTURE_DB="$LAB_ROOT/manifests/compile_commands.json"
PROJECT_DIR="$LAB_ROOT/manifests/project"
GEOMETRY_DB="$LAB_ROOT/manifests"
GRAPHLAB_DB="$LAB_ROOT/manifests"

fail() { echo "parity_check: FAIL: $*" >&2; exit 1; }

# --- prerequisites (fail loudly, never skip silently) ------------------------
[ -x "$CPP_BIN" ] || fail "C++ binary not found/executable: $CPP_BIN (build cidx-cpp first, or set CIDX_CPP_BIN)"
[ -x "$PY_CIDX" ] || fail "Python launcher not found: $PY_CIDX"
[ -f "$FIXTURE_DB" ] || fail "fixture missing: $FIXTURE_DB"
[ -f "$GEOMETRY_DB/compile_commands.json" ] || fail "geometry fixture missing: $GEOMETRY_DB/compile_commands.json"
[ -f "$GRAPHLAB_DB/compile_commands.json" ] || fail "graphlab fixture missing: $GRAPHLAB_DB/compile_commands.json"
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

# run_one_ast — like run_one but also masks .ast file sizes.
# .ast files produced by Python and C++ clang_saveTranslationUnit differ by a
# small fixed delta (format-header metadata varies by TU origin) even when both
# link the same libclang.  Mask "(<N> bytes)" → "({SZ} bytes)" and size columns
# in `ast cache status` output (the multi-digit size field before "valid").
run_one_ast() {
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
    # Normalize: cache path, timestamp, and .ast byte sizes.
    sed -e "s|$cache|{CACHE}|g" \
        -e 's|^indexed at   .* UTC$|indexed at   {TS} UTC|' \
        -e 's| ([0-9,]* bytes)| ({SZ} bytes)|g' \
        -e 's|  [0-9,]\{5,\}  |  {SZ}  |g' \
        -e 's|  [0-9,]\{5,\},  |  {SZ},  |g' \
        -e 's|[0-9,]* bytes total|{SZ} bytes total|g' \
        -e 's|, [0-9,]* bytes freed|, {SZ} bytes freed|g' \
        "$out"
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
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- resolve
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- resolve --rebuild
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
  # import --force: delete the existing component (its files + indexed symbols)
  # and rebuild from the same DB, then re-index. Both tools must emit the same
  # force/component/counts lines and produce an identical rebuilt index.db.
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- import --force --db "$FIXTURE_DB" --name parityproj
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list files --pending
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- index
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- resolve
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list symbols --limit 2
  # M2: geometry fixture (C++ graph edges: inherits/field_of/method_of/
  # template_param/instantiates/template_arg). Import + index + resolve so the
  # DB dump covers all graph tables and diffs are byte-strict across both tools.
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- import --db "$GEOMETRY_DB" --name geoproject
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- index
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- resolve
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- resolve --rebuild

  # M3: graphlab fixture — includes chain.cpp (value-typed local B passed as
  # argument to top_rank) which exercises call_arg/edge_site provenance with
  # UNEXPOSED_EXPR arguments.  Parity here golden-locks that Python and C++
  # emit identical call_arg rows (src_kind='local', type_usr=chain::B) for
  # the top_rank(b) call, catching any future _peel_expr / classify divergence.
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- import --db "$GRAPHLAB_DB" --name graphlabproj
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- index
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- resolve

  # `file` (per-file compile-flag editor) + dump-compile-commands (schema v8):
  # addressed COMPONENT://RELPATH, the relpath is relative to the component
  # root (parityproj's git root). REL makes the address independent of where
  # that root sits. -set-flag/-unset-flag/-import-args mark the file
  # args_overridden; -dump-args and dump-compile-commands emit JSON. Run before
  # the delete block (which removes app.c + parityproj). Help, malformed
  # target, unknown component, unknown op, and not-in-db all diff byte-strict.
  GROOT="$(cd "$PROJECT_DIR" && git rev-parse --show-toplevel 2>/dev/null || echo "$LAB_ROOT")"
  REL="${PROJECT_DIR#"$GROOT"/}"
  APPADDR="parityproj://$REL/app.c"
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "$APPADDR" -dump-args
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "$APPADDR" -set-flag -DPARITY_FILE=1
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "$APPADDR" -set-flag -DPARITY_FILE=1
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "$APPADDR" -dump-args
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "$APPADDR" -import-args "{\"directory\": \"$PROJECT_DIR\", \"file\": \"app.c\", \"arguments\": [\"cc\", \"-I.\", \"-DPARITY_IMP=2\", \"-c\", \"app.c\", \"-o\", \"app.o\"]}"
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "$APPADDR" -dump-args
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "$APPADDR" -unset-flag -DPARITY_IMP=2
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "$APPADDR"
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "parityproj://does/not/exist.c" -dump-args
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "bogustarget" -dump-args
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "nocomp://x.c" -dump-args
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file "$APPADDR" -bogus-op
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- file -h
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- dump-compile-commands parityproj
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- dump-compile-commands nosuchcomp
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- dump-compile-commands -h

  # delete subcommand: help, nested-choice errors, per-leaf help, the
  # required-mutex / mutex / bad-int / 0-match error paths, dry-run previews,
  # then REAL deletes exercising cascade + orphan-symbol purge. Placed last so
  # the golden assertions above are undisturbed; the final DB dump reflects the
  # post-delete state and must still match byte-for-byte across both tools.
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete bogus
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete component -h
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete dir -h
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete file -h
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete symbol -h
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete symbol
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete symbol --id 1 --name x
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete dir --id 1 --path /x
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete file --id notanint
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete symbol --name nope
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete symbol --name multiply --dry-run
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete symbol --name multiply
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete file --name app.c
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- delete component --name parityproj
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list components
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- list symbols

  # --- M5: ast dump / locals / conditions (text + --json + --tokens + --ast) --
  # All ad-hoc targets (FILE + -- -std=c11) so this block is independent of the
  # indexed fixture built above.  Options come BEFORE the positional; -- FLAGS last.
  # $cache is the per-tool INDEXER_CACHE (PY_CACHE or CPP_CACHE) passed into
  # run_script — ast cache files go there alongside index.db.
  MAN="$LAB_ROOT/manifests"
  # dump: text + JSON + tokens, single-function focus, whole-file
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast dump --name leaf_a --depth 2 --types "$MAN/calls.c" -- -std=c11
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast dump --name leaf_a --depth 2 --types --json "$MAN/calls.c" -- -std=c11
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast dump --tokens "$MAN/calls.c" -- -std=c11
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast dump --depth 1 --json "$MAN/shapes.c" -- -std=c11
  # locals: text + JSON + --params
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast locals --name BadlyNamedFunction --params "$MAN/messy.c" -- -std=c11
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast locals --name BadlyNamedFunction --params --json "$MAN/messy.c" -- -std=c11
  # conditions: text + JSON + --ast (the AST subtree of the condition)
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast conditions --name shape_area "$MAN/shapes.c" -- -std=c11
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast conditions --name shape_area --json "$MAN/shapes.c" -- -std=c11
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast conditions --name shape_area --ast --json "$MAN/shapes.c" -- -std=c11
  # conditions: empty-result case (recurse has no guarded calls)
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast conditions --name recurse --json "$MAN/calls.c" -- -std=c11
  # --- error paths: exit codes + stderr must match byte-for-byte
  # missing focus (--name not found in file)
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast locals --name nope "$MAN/messy.c" -- -std=c11
  # --cache --no-cache mutex error (exit 2)
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast dump --cache --no-cache "$MAN/calls.c" -- -std=c11
  # no subcommand (exit 2)
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast
  # REMAINDER without target (exit 1: target="-std=c11" → file-not-found)
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast dump -- -std=c11
  # bad --kind choice (exit 2)
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast dump --kind notakind "$MAN/calls.c" -- -std=c11
  # help text for dump / cache group (exit 0; exact text pinned by diff)
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast dump -h
  run_one "$transcript" "$cache" "$is_py" "${T[@]}" -- ast cache -h
  # --- M5: ast cache lifecycle (build / status / clear) ----------------------
  # .ast sizes differ between Python and C++ (56-byte delta observed: different
  # sidecar mtime float formatting → different TU save header bytes). Sizes are
  # masked with {SZ} by run_one_ast.  Keys (sha1 of abspath+flags) are identical.
  run_one_ast "$transcript" "$cache" "$is_py" "${T[@]}" -- ast cache status
  run_one_ast "$transcript" "$cache" "$is_py" "${T[@]}" -- ast cache build "$MAN/calls.c" -- -std=c11
  run_one_ast "$transcript" "$cache" "$is_py" "${T[@]}" -- ast cache status "$MAN/calls.c" -- -std=c11
  run_one_ast "$transcript" "$cache" "$is_py" "${T[@]}" -- ast cache clear "$MAN/calls.c" -- -std=c11
  run_one_ast "$transcript" "$cache" "$is_py" "${T[@]}" -- ast cache status
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
  # NULL out the volatile graph_resolved_at timestamp in meta (written by resolve).
  sqlite3 "$tmp" "UPDATE meta SET value = NULL WHERE key = 'graph_resolved_at';" \
    || fail "sqlite3 UPDATE meta failed on $src"
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

# --- diff 3: CIDX_MEM per-TU memory report ------------------------------------
# Both tools call clang_getCXTUResourceUsage on the SAME linked libclang over
# the SAME sources, so the per-TU byte amounts are deterministic and identical.
# Re-index the fixture under CIDX_MEM=1 in a throwaway cache per tool and diff
# the "TU memory" log lines (the leading timestamp is stripped; absolute source
# paths are identical for both tools).
mem_report() {
  local cache=$1 is_py=$2 out=$3; shift 3
  local -a tool=("$@")
  rm -rf "$cache"; mkdir -p "$cache"
  if [ "$is_py" = "1" ]; then
    INDEXER_CACHE="$cache" CIDX_LIBCLANG="$PY_LIBCLANG" \
      "${tool[@]}" import --db "$FIXTURE_DB" --name memproj >/dev/null 2>&1
    INDEXER_CACHE="$cache" CIDX_LIBCLANG="$PY_LIBCLANG" CIDX_MEM=1 \
      "${tool[@]}" index >/dev/null 2>&1
  else
    INDEXER_CACHE="$cache" env -u CIDX_LIBCLANG \
      "${tool[@]}" import --db "$FIXTURE_DB" --name memproj >/dev/null 2>&1
    INDEXER_CACHE="$cache" env -u CIDX_LIBCLANG CIDX_MEM=1 \
      "${tool[@]}" index >/dev/null 2>&1
  fi
  # Drop the "<date> <time>,<ms> INFO " prefix; keep "<src>: TU memory ...".
  grep -h "TU memory" "$cache/cidx.log" | sed -E 's/^[0-9-]+ [0-9:,]+ INFO //' \
    | sort >"$out"
}
mem_report "$WORK/py-mem"  "1" "$WORK/py.mem"  "$PY_CIDX"
mem_report "$WORK/cpp-mem" "0" "$WORK/cpp.mem" "$CPP_BIN"
[ -s "$WORK/py.mem" ] || fail "CIDX_MEM produced no Python memory lines"
if ! diff -u "$WORK/py.mem" "$WORK/cpp.mem" >"$WORK/mem.diff"; then
  echo "parity_check: CIDX_MEM REPORT DIFF (python vs cpp):" >&2
  cat "$WORK/mem.diff" >&2
  fail "CIDX_MEM memory reports differ (work dir kept: $WORK)"
fi
echo "parity_check: CIDX_MEM memory reports identical ($(wc -l <"$WORK/py.mem" | tr -d ' ') TUs)"

rm -rf "$WORK"
echo "parity_check: PASS"
