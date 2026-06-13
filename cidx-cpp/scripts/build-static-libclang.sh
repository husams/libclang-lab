#!/usr/bin/env bash
# build-static-libclang.sh — produce a static libclang.a (the C API) so cidx can
# be linked with -DCIDX_STATIC=ON and carry NO libclang.so dependency.
#
# WHY THIS EXISTS
#   No common libclang distribution ships a *linkable* static C-API archive:
#     * macOS pip wheel  -> libclang.dylib only.
#     * official LLVM release tarballs (/opt/llvm-*) -> libclang.so only, and
#       their *.a component libs are LLVM-IR *bitcode* (LTO build) that GNU ld
#       cannot link.
#     * distro -dev packages -> libclang.so only (no libclang.a).
#   But a distro's clang/LLVM *component* static libs (libclangAST.a, libLLVM*.a)
#   ARE regular ELF and built with libstdc++ — ABI-compatible with a g++ build of
#   cidx. The only missing piece is the thin C-API wrapper (clang/tools/libclang).
#   This script compiles exactly those wrapper sources against the installed
#   headers and archives them into libclang.a. CMake's CIDX_STATIC path then links
#   that wrapper + the component archives in one --start-group.
#
# REQUIREMENTS (validated on Ubuntu 24.04 with apt llvm-18 = 18.1.3):
#   - <prefix>/bin/llvm-config and <prefix>/include/clang-c (apt llvm-18-dev +
#     libclang-18-dev -> /usr/lib/llvm-18).
#   - The component static libs present and NON-LTO (regular ELF) — the script
#     checks and aborts on bitcode.
#   - A C++ compiler matching the components' runtime (libstdc++ for apt llvm).
#   - curl + tar + network (downloads the matching clang source tarball).
#
# USAGE
#   scripts/build-static-libclang.sh [LLVM_PREFIX] [OUTPUT_A]
#     LLVM_PREFIX  default: prefix of llvm-config-18, else /usr/lib/llvm-18,
#                  else prefix of llvm-config.
#     OUTPUT_A     default: <libdir>/libclang.a, falling back to ./libclang.a
#                  when <libdir> is not writable.
#
# Then build cidx with a fully-static libclang:
#   cmake -S . -B build-static -DCIDX_STATIC=ON \
#         -DCIDX_LIBCLANG=<libdir>/libclang.so \
#         -DCIDX_LIBCLANG_STATIC=<OUTPUT_A>
#   cmake --build build-static
set -euo pipefail

INVOKE_CWD="$PWD"

PREFIX="${1:-}"
if [ -z "$PREFIX" ]; then
  if command -v llvm-config-18 >/dev/null 2>&1; then
    PREFIX="$(dirname "$(dirname "$(command -v llvm-config-18)")")"
  elif [ -d /usr/lib/llvm-18 ]; then
    PREFIX=/usr/lib/llvm-18
  elif command -v llvm-config >/dev/null 2>&1; then
    PREFIX="$(dirname "$(dirname "$(command -v llvm-config)")")"
  else
    echo "error: no LLVM prefix given and none found (pass it as arg 1)" >&2
    exit 1
  fi
fi

LC="$PREFIX/bin/llvm-config"
[ -x "$LC" ] || { echo "error: $LC not found/executable" >&2; exit 1; }
AR="$PREFIX/bin/llvm-ar"
command -v "$AR" >/dev/null 2>&1 || AR="ar"
VER="$("$LC" --version)"                 # e.g. 18.1.3
INCDIR="$("$LC" --includedir)"
LIBDIR="$("$LC" --libdir)"
OUT="${2:-$LIBDIR/libclang.a}"
CXX="${CXX:-g++}"

echo "build-static-libclang: prefix=$PREFIX version=$VER CXX=$CXX"

# Sanity: components must be regular ELF (not LTO bitcode), or the final
# CIDX_STATIC link fails with 'file format not recognized'.
probe="$LIBDIR/libclangAST.a"
if [ -f "$probe" ]; then
  m="$("$AR" t "$probe" 2>/dev/null | head -1 || true)"
  if [ -n "$m" ] && "$AR" p "$probe" "$m" 2>/dev/null | head -c4 | grep -q $'BC\xc0\xde'; then
    echo "error: $probe is LLVM-IR bitcode (LTO build); ld cannot link it." >&2
    echo "       Use a non-LTO LLVM (e.g. distro apt llvm-*-dev)." >&2
    exit 1
  fi
else
  echo "warning: $probe not found — component static libs may be missing for the link" >&2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

TARBALL="clang-${VER}.src.tar.xz"
URL="https://github.com/llvm/llvm-project/releases/download/llvmorg-${VER}/${TARBALL}"
echo "build-static-libclang: fetching $URL"
curl -fsSL -o "$TARBALL" "$URL"
tar xf "$TARBALL"
SRC="$WORK/clang-${VER}.src/tools/libclang"
[ -d "$SRC" ] || { echo "error: $SRC missing in tarball" >&2; exit 1; }

cd "$SRC"
CXXFLAGS="$("$LC" --cxxflags) -I. -I$INCDIR -D_CINDEX_LIB_ -fPIC"
mkdir -p "$WORK/obj"
ok=0; skipped=""
for f in *.cpp; do
  if $CXX $CXXFLAGS -c "$f" -o "$WORK/obj/${f%.cpp}.o" 2>"$WORK/err"; then
    ok=$((ok + 1))
  else
    # Optional units (e.g. CXExtractAPI) may need tablegen headers a release
    # install omits; skip them — cidx does not use those APIs.
    skipped="$skipped ${f}"
    echo "  skip ${f}: $(grep -m1 'fatal error' "$WORK/err" | sed 's#.*fatal error: ##' || echo 'compile failed')"
  fi
done
echo "build-static-libclang: compiled $ok object(s);${skipped:+ skipped:$skipped}"
[ "$ok" -gt 0 ] || { echo "error: no libclang sources compiled" >&2; exit 1; }

# Resolve OUT to an absolute path and fall back to the invoking CWD when the
# chosen directory is not writable.
case "$OUT" in /*) ;; *) OUT="$INVOKE_CWD/$OUT" ;; esac
outdir="$(dirname "$OUT")"
if [ ! -w "$outdir" ]; then
  echo "warning: $outdir not writable; writing $INVOKE_CWD/libclang.a instead" >&2
  OUT="$INVOKE_CWD/libclang.a"
fi
rm -f "$OUT"
"$AR" rcs "$OUT" "$WORK"/obj/*.o
sym="$(nm "$OUT" 2>/dev/null | grep -cE ' T clang_parseTranslationUnit2$' || true)"
echo "build-static-libclang: wrote $OUT ($(du -h "$OUT" | cut -f1)); defines C API: $sym"
[ "$sym" = "1" ] || { echo "error: $OUT does not define the libclang C API" >&2; exit 1; }
echo "build-static-libclang: OK — configure cidx with:"
echo "  -DCIDX_STATIC=ON -DCIDX_LIBCLANG=$LIBDIR/libclang.so -DCIDX_LIBCLANG_STATIC=$OUT"
