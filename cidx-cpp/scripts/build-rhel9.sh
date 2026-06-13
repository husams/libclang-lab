#!/usr/bin/env bash
# build-rhel9.sh — install dependencies (incl. a static SQLite) and build cidx on
# RHEL 9.x / AlmaLinux 9 / Rocky 9. Run it from anywhere; it locates the repo.
#
#   ./scripts/build-rhel9.sh
#
# Produces: <repo>/build-static/cidx — SQLite3 + the C++ runtime linked
# STATICALLY, libclang linked DYNAMICALLY (RHEL ships no static clang libs; a
# static libclang would require building clang from source). To RUN the binary,
# the host needs libclang.so:   dnf install -y clang-libs
#
# Knobs (env vars):
#   GCC_TOOLSET             gcc-toolset major for C++23 (default 13; RHEL 9's
#                           system gcc is 11, too old).
#   SQLITE_AMALGAMATION_URL SQLite amalgamation zip (default 3.45.1). cidx needs
#                           SQLite >= 3.35.
#   FORCE_SQLITE=1          rebuild /usr/lib64/libsqlite3.a even if present.
#   BUILD_DIR               cmake build dir (default <repo>/build-static).
#   JOBS                    parallel build jobs (default: nproc).
#   SKIP_DEPS=1             skip dnf installs (deps already present).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CIDX_ROOT="$(dirname "$SCRIPT_DIR")"

GCC_TOOLSET="${GCC_TOOLSET:-13}"
SQLITE_AMALGAMATION_URL="${SQLITE_AMALGAMATION_URL:-https://www.sqlite.org/2024/sqlite-amalgamation-3450100.zip}"
BUILD_DIR="${BUILD_DIR:-$CIDX_ROOT/build-static}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

# --- dependencies ------------------------------------------------------------
if [ "${SKIP_DEPS:-0}" != "1" ]; then
  echo "==> installing build dependencies (dnf)"
  $SUDO dnf -y install dnf-plugins-core
  # CRB / CodeReady Builder (some deps live there). Repo id differs by distro;
  # try each, never fail the run if it cannot be toggled.
  $SUDO dnf config-manager --set-enabled crb 2>/dev/null \
    || $SUDO dnf config-manager --set-enabled "codeready-builder-for-rhel-9-$(arch)-rpms" 2>/dev/null \
    || $SUDO subscription-manager repos --enable "codeready-builder-for-rhel-9-$(arch)-rpms" 2>/dev/null \
    || $SUDO crb enable 2>/dev/null \
    || echo "   (could not enable CRB automatically — continuing)"
  $SUDO dnf -y install \
    "gcc-toolset-${GCC_TOOLSET}" "gcc-toolset-${GCC_TOOLSET}-libstdc++-devel" \
    cmake make git tar xz unzip which \
    clang-devel llvm-devel
fi

TOOLSET_ENABLE="/opt/rh/gcc-toolset-${GCC_TOOLSET}/enable"
[ -f "$TOOLSET_ENABLE" ] || { echo "error: $TOOLSET_ENABLE missing (install gcc-toolset-${GCC_TOOLSET})" >&2; exit 1; }

# --- static SQLite (RHEL ships no libsqlite3.a) ------------------------------
if [ ! -f /usr/lib64/libsqlite3.a ] || [ "${FORCE_SQLITE:-0}" = "1" ]; then
  echo "==> building static libsqlite3.a from the amalgamation"
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/sqlite.zip" "$SQLITE_AMALGAMATION_URL"
  ( cd "$tmp" && unzip -q sqlite.zip && cd sqlite-amalgamation-* \
    && gcc -O2 -fPIC -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_JSON1 -DSQLITE_ENABLE_RTREE \
           -c sqlite3.c -o sqlite3.o \
    && $SUDO ar rcs /usr/lib64/libsqlite3.a sqlite3.o \
    && $SUDO install -m 0644 sqlite3.h sqlite3ext.h /usr/include/ )
  echo "   wrote /usr/lib64/libsqlite3.a"
else
  echo "==> /usr/lib64/libsqlite3.a already present (FORCE_SQLITE=1 to rebuild)"
fi

# --- build cidx --------------------------------------------------------------
echo "==> building cidx (gcc-toolset-${GCC_TOOLSET}; static SQLite + libstdc++, dynamic libclang)"
# shellcheck disable=SC1090
source "$TOOLSET_ENABLE"
LLVM_LIBDIR="$(llvm-config --libdir)"
cmake -S "$CIDX_ROOT" -B "$BUILD_DIR" -DCIDX_STATIC=ON \
  -DCIDX_LIBCLANG="$LLVM_LIBDIR/libclang.so"
cmake --build "$BUILD_DIR" -j"$JOBS" --target cidx

echo
echo "==> built: $BUILD_DIR/cidx"
"$BUILD_DIR/cidx" --help >/dev/null 2>&1 && echo "    cidx runs OK"
echo "    to run on another RHEL 9.6 host: copy the binary + 'dnf install -y clang-libs'"
