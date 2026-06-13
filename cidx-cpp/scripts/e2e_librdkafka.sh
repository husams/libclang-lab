#!/usr/bin/env bash
# e2e_librdkafka.sh — S08 release gate (design §8, label `e2e`, manual).
#
# Validation target (analysis §9.1.11): librdkafka under the /opt/gcc8
# conda-forge g++ 8.5.0 cross toolchain × libclang 21.1.1 must index
# 93/93 TUs on the gcc-index-test box (192.168.1.115, ssh as husam — memory
# note `gcc-index-test-box`).
#
# The script is self-contained: it pre-flights the box's resources, ensures
# libsqlite3-dev (passwordless sudo; fails loudly if sudo needs a password),
# rsyncs THIS cidx-cpp tree, builds it remotely with the system g++ (the
# C++17 / g++-floor check), then imports + indexes the box's
# ~/librdkafka/build-gcc8 compile DB into a throwaway cache and asserts the
# final summary line reports 93 indexed / 0 failed. Nothing outside the
# remote ~/cidx-cpp-e2e dir and a mktemp cache is written on the box.
#
# A1 (Amendment A1 — spec/02-design.md §12, 2026-06-12):
#   cidx-cpp links libclang at build time.  The cmake configure step now
#   receives -DCIDX_LIBCLANG=$REMOTE_LIBCLANG so the binary is built
#   against libclang 21.1.1 and RPATH is set to that directory.  The
#   runtime CIDX_LIBCLANG env export is removed — the env var is ignored
#   by the linked binary (one-shot WARNING), and RPATH makes the dylib
#   visible without LD_LIBRARY_PATH.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CIDX_CPP_ROOT="$(dirname "$SCRIPT_DIR")"

BOX="${CIDX_E2E_HOST:-husam@192.168.1.115}"
REMOTE_DIR="cidx-cpp-e2e"
REMOTE_LIBCLANG="/opt/llvm-21.1.1/lib/libclang.so"
REMOTE_COMPILE_DB="\$HOME/librdkafka/build-gcc8/compile_commands.json"
EXPECT_INDEXED=93

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)

fail() { echo "e2e_librdkafka: FAIL: $*" >&2; exit 1; }
run_remote() { ssh "${SSH_OPTS[@]}" "$BOX" "$@"; }

command -v rsync >/dev/null 2>&1 || fail "rsync not on PATH"

# --- 1. reachability + pre-flight resources ----------------------------------
run_remote 'true' || fail "test box unreachable: $BOX (story is BLOCKED, not done)"
echo "e2e_librdkafka: box reachable ($BOX)"
run_remote 'free -m | sed -n 2p; nproc; df -h "$HOME" | tail -1'

run_remote "[ -e $REMOTE_LIBCLANG ]" \
  || fail "missing $REMOTE_LIBCLANG on the box"
run_remote "[ -f $REMOTE_COMPILE_DB ]" \
  || fail "missing librdkafka gcc8 compile DB on the box"

# --- 2. build dependency: libsqlite3-dev --------------------------------------
if ! run_remote 'dpkg -s libsqlite3-dev >/dev/null 2>&1'; then
  echo "e2e_librdkafka: installing libsqlite3-dev (passwordless sudo)"
  run_remote 'sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libsqlite3-dev >/dev/null 2>&1' \
    || fail "cannot install libsqlite3-dev (sudo needs a password?) — install it manually"
fi

# --- 3. sync THIS tree + remote build -----------------------------------------
echo "e2e_librdkafka: rsyncing cidx-cpp -> $BOX:~/$REMOTE_DIR/src"
run_remote "mkdir -p $REMOTE_DIR/src" || fail "cannot create remote dir"
rsync -a --delete -e "ssh ${SSH_OPTS[*]}" \
  --exclude 'build*/' --exclude '.git/' \
  "$CIDX_CPP_ROOT/" "$BOX:$REMOTE_DIR/src/" || fail "rsync failed"

echo "e2e_librdkafka: building on the box (A1: -DCIDX_LIBCLANG baked at configure time)"
run_remote "cd $REMOTE_DIR && cmake -B build -S src -DCMAKE_BUILD_TYPE=Release -DCIDX_LIBCLANG=$REMOTE_LIBCLANG >cmake.log 2>&1 && cmake --build build -j\$(nproc) >>cmake.log 2>&1" \
  || { run_remote "tail -40 $REMOTE_DIR/cmake.log" >&2; fail "remote build failed"; }

# --- 4. import + index librdkafka ----------------------------------------------
# A1: CIDX_LIBCLANG is NOT exported at runtime — the binary links libclang
# at build time and RPATH covers the load path.  Only INDEXER_CACHE is set.
echo "e2e_librdkafka: import + index (gcc8 cross toolchain, libclang 21.1.1)"
RESULT="$(run_remote "
  set -u
  unset CIDX_LIBCLANG  # A1: ensure no stale box env triggers the ignored warning
  CACHE=\$(mktemp -d /tmp/cidx_e2e_XXXXXX)
  export INDEXER_CACHE=\$CACHE
  CIDX=\$HOME/$REMOTE_DIR/build/cidx
  \$CIDX import --db $REMOTE_COMPILE_DB > \$CACHE/import.out 2>&1 \
    || { echo IMPORT-FAILED; cat \$CACHE/import.out; exit 0; }
  tail -1 \$CACHE/import.out
  \$CIDX index > \$CACHE/index.out 2> \$CACHE/index.err
  echo \"index-exit: \$?\"
  grep '^index: ' \$CACHE/index.out || echo NO-SUMMARY-LINE
  if [ -s \$CACHE/index.err ]; then echo '--- index stderr ---'; cat \$CACHE/index.err; fi
  if [ -f \$CACHE/cidx.log ]; then echo \"cidx.log: \$(wc -l < \$CACHE/cidx.log) line(s)\"; fi
  echo \"cache: \$CACHE\"
")" || fail "remote run failed"

echo "$RESULT"
echo "$RESULT" | grep -q 'IMPORT-FAILED' && fail "import failed on the box"

SUMMARY="$(echo "$RESULT" | grep '^index: ')" || fail "no index summary line"
echo "$SUMMARY" | grep -q "^index: $EXPECT_INDEXED indexed, 0 failed, " \
  || fail "expected 'index: $EXPECT_INDEXED indexed, 0 failed, ...' — got: $SUMMARY"
echo "$RESULT" | grep -q '^index-exit: 0$' || fail "index exited non-zero"

echo "e2e_librdkafka: PASS — $EXPECT_INDEXED/$EXPECT_INDEXED TUs indexed"
