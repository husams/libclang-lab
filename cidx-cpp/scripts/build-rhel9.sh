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
#   SQLITE_FROM             where to get SQLite: 'amalgamation' (zip from
#                           sqlite.org), 'git' (GitHub source), or 'auto'
#                           (default: try the zip, fall back to GitHub if the
#                           download is blocked).
#   SQLITE_AMALGAMATION_URL amalgamation zip URL (default 3.45.1).
#   SQLITE_GIT_URL          SQLite git mirror (default github.com/sqlite/sqlite).
#   SQLITE_GIT_TAG          tag to build (default version-3.45.1). cidx needs
#                           SQLite >= 3.35.
#   FORCE_SQLITE=1          rebuild /usr/lib64/libsqlite3.a even if present.
#   BUILD_DIR               cmake build dir (default <repo>/build-static).
#   JOBS                    parallel build jobs (default: nproc).
#   SKIP_DEPS=1             skip dnf installs (deps already present).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CIDX_ROOT="$(dirname "$SCRIPT_DIR")"

GCC_TOOLSET="${GCC_TOOLSET:-13}"
SQLITE_FROM="${SQLITE_FROM:-auto}"
SQLITE_AMALGAMATION_URL="${SQLITE_AMALGAMATION_URL:-https://www.sqlite.org/2024/sqlite-amalgamation-3450100.zip}"
SQLITE_GIT_URL="${SQLITE_GIT_URL:-https://github.com/sqlite/sqlite.git}"
SQLITE_GIT_TAG="${SQLITE_GIT_TAG:-version-3.45.1}"
BUILD_DIR="${BUILD_DIR:-$CIDX_ROOT/build-static}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

# dnf refreshes the metadata index of EVERY enabled repo before installing even
# one package, so a single broken third-party repo (e.g. a devcontainer base
# image's packages-microsoft-com-prod with a rotated/mismatched GPG key) aborts
# the whole run. cidx needs nothing from those repos — its packages come from
# baseos/appstream/crb — so skip any repo that fails to load and drop the known
# Microsoft one outright. This does NOT update/upgrade anything; it only scopes
# which repos each 'dnf install' is allowed to consult.
dnf_install() {
  $SUDO dnf -y \
    --setopt='*.skip_if_unavailable=1' \
    --disablerepo='*microsoft*' \
    install "$@"
}

# --- dependencies ------------------------------------------------------------
if [ "${SKIP_DEPS:-0}" != "1" ]; then
  echo "==> installing build dependencies (dnf)"
  dnf_install dnf-plugins-core
  # CRB / CodeReady Builder (some deps live there). Repo id differs by distro;
  # try each, never fail the run if it cannot be toggled.
  $SUDO dnf config-manager --set-enabled crb 2>/dev/null \
    || $SUDO dnf config-manager --set-enabled "codeready-builder-for-rhel-9-$(arch)-rpms" 2>/dev/null \
    || $SUDO subscription-manager repos --enable "codeready-builder-for-rhel-9-$(arch)-rpms" 2>/dev/null \
    || $SUDO crb enable 2>/dev/null \
    || echo "   (could not enable CRB automatically — continuing)"
  dnf_install \
    "gcc-toolset-${GCC_TOOLSET}" "gcc-toolset-${GCC_TOOLSET}-libstdc++-devel" \
    cmake make git tar xz unzip which \
    clang-devel llvm-devel
fi

TOOLSET_ENABLE="/opt/rh/gcc-toolset-${GCC_TOOLSET}/enable"
[ -f "$TOOLSET_ENABLE" ] || { echo "error: $TOOLSET_ENABLE missing (install gcc-toolset-${GCC_TOOLSET})" >&2; exit 1; }

# --- static SQLite (RHEL ships no libsqlite3.a) ------------------------------
# Fetch the SQLite sources into $1 (leaving sqlite3.c + sqlite3.h there) from the
# amalgamation zip or the GitHub mirror. 'auto' tries the zip and falls back to
# GitHub when the download is blocked (e.g. a proxy returns 403).
fetch_sqlite_src() {
  local out="$1" got_zip=0
  if [ "$SQLITE_FROM" = amalgamation ] || [ "$SQLITE_FROM" = auto ]; then
    if curl -fsSL -o "$out/sqlite.zip" "$SQLITE_AMALGAMATION_URL"; then
      ( cd "$out" && unzip -q sqlite.zip \
        && cp sqlite-amalgamation-*/sqlite3.c sqlite-amalgamation-*/sqlite3.h . )
      got_zip=1
    elif [ "$SQLITE_FROM" = amalgamation ]; then
      echo "error: could not download $SQLITE_AMALGAMATION_URL" >&2; return 1
    else
      echo "   amalgamation download blocked — generating from GitHub source"
    fi
  fi
  if [ "$got_zip" -eq 0 ]; then
    # GitHub mirror: build the amalgamation from the tag (needs tcl to run
    # tool/mksqlite3c.tcl via ./configure && make sqlite3.c).
    [ "${SKIP_DEPS:-0}" = "1" ] || dnf_install git tcl file >/dev/null
    git clone --depth 1 -b "$SQLITE_GIT_TAG" "$SQLITE_GIT_URL" "$out/src"
    ( cd "$out/src" && ./configure >/dev/null && make sqlite3.c >/dev/null )
    cp "$out/src/sqlite3.c" "$out/src/sqlite3.h" "$out/"
  fi
}

if [ ! -f /usr/lib64/libsqlite3.a ] || [ "${FORCE_SQLITE:-0}" = "1" ]; then
  echo "==> building static libsqlite3.a (SQLITE_FROM=$SQLITE_FROM)"
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  fetch_sqlite_src "$tmp"
  gcc -O2 -fPIC -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_JSON1 -DSQLITE_ENABLE_RTREE \
      -c "$tmp/sqlite3.c" -o "$tmp/sqlite3.o"
  $SUDO ar rcs /usr/lib64/libsqlite3.a "$tmp/sqlite3.o"
  $SUDO install -m 0644 "$tmp/sqlite3.h" /usr/include/
  [ -f "$tmp/sqlite-amalgamation-"*/sqlite3ext.h ] 2>/dev/null \
    && $SUDO install -m 0644 "$tmp"/sqlite-amalgamation-*/sqlite3ext.h /usr/include/ || true
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
