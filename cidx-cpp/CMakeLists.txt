# cidx-cpp — C++ port of the cidx semantic symbol indexer (spec/02-design.md).
# A2 (Amendment A2 — spec §13): C++23; build hosts are AppleClang 15+ / gcc 13+.
cmake_minimum_required(VERSION 3.16)
project(cidx LANGUAGES C CXX)

# Default to Release when the user has not specified a build type.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE "Release" CACHE STRING
      "Build type (Release, Debug, RelWithDebInfo, MinSizeRel)" FORCE)
  message(STATUS "cidx: CMAKE_BUILD_TYPE defaulted to Release")
endif()

# CIDX_STATIC: prefer static archives for the third-party dependencies so the
# binary carries fewer runtime requirements. Platform reality differs:
#   * Linux: SQLite3 from its static archive + -static-libstdc++ -static-libgcc
#     so the binary needs neither libstdc++ nor libsqlite3 at runtime. glibc
#     stays dynamic (a fully static glibc binary is discouraged and breaks
#     NSS/dlopen). libclang is ALSO linked statically when a C-API wrapper
#     archive is supplied via -DCIDX_LIBCLANG_STATIC=<libclang.a> (build it once
#     with scripts/build-static-libclang.sh against a NON-LTO distro LLVM, e.g.
#     apt llvm-18 — validated 2026-06-13 on Ubuntu 24.04 -> the binary has no
#     libclang.so dependency). Without it, libclang falls back to dynamic: no
#     distribution ships a linkable static libclang C API (release tarballs are
#     LTO bitcode; -dev packages ship only libclang.so), hence the wrapper.
#   * macOS: only SQLite3 has a static archive; libclang ships shared-only and
#     Apple provides no static libSystem, so libclang stays dynamic (RPATH).
# Each dependency falls back to its shared form (with a STATUS/WARNING) when no
# static archive is found, so a static configure never hard-fails.
option(CIDX_STATIC "Statically link dependencies where the platform allows" OFF)

# A2.1: C++23 standard; minimum compilers (A2.1 guard).
# Build hosts: macOS AppleClang (dev) and Ubuntu 24.04 gcc 13 (e2e).
# g++ 8.5 cross toolchains are *indexing targets*, not the cidx build host.
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU" AND
   CMAKE_CXX_COMPILER_VERSION VERSION_LESS 13)
  message(FATAL_ERROR
    "cidx requires gcc >= 13 for C++23 (found gcc ${CMAKE_CXX_COMPILER_VERSION}). "
    "g++ 8.5 cross toolchains are *indexing targets* only; build cidx with gcc 13+.")
endif()
if(CMAKE_CXX_COMPILER_ID STREQUAL "Clang" AND
   CMAKE_CXX_COMPILER_VERSION VERSION_LESS 16)
  message(FATAL_ERROR
    "cidx requires clang >= 16 for C++23 (found clang ${CMAKE_CXX_COMPILER_VERSION}).")
endif()
if(CMAKE_CXX_COMPILER_ID STREQUAL "AppleClang" AND
   CMAKE_CXX_COMPILER_VERSION VERSION_LESS 15)
  message(FATAL_ERROR
    "cidx requires AppleClang >= 15 for C++23 (found AppleClang ${CMAKE_CXX_COMPILER_VERSION}).")
endif()

message(STATUS "cidx: compiler ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}, C++${CMAKE_CXX_STANDARD}")

# S02: storage layer sits on the system SQLite3 (>= 3.35 enforced at runtime).
find_package(SQLite3 REQUIRED)

# Default: the shared SQLite3 imported target. Under CIDX_STATIC, swap in the
# static archive when one is on disk (the sqlite amalgamation pulls in
# dl/pthread/m; libSystem covers those on macOS so they are Linux-only).
set(_cidx_sqlite_link SQLite::SQLite3)
if(CIDX_STATIC)
  find_library(CIDX_SQLITE3_STATIC NAMES libsqlite3.a
    HINTS /opt/homebrew/opt/sqlite/lib /usr/local/opt/sqlite/lib
          /usr/lib /usr/lib64 /usr/local/lib
    DOC "Static SQLite3 archive for CIDX_STATIC")
  if(CIDX_SQLITE3_STATIC)
    add_library(cidx_sqlite_static STATIC IMPORTED)
    set_target_properties(cidx_sqlite_static PROPERTIES
      IMPORTED_LOCATION "${CIDX_SQLITE3_STATIC}"
      INTERFACE_INCLUDE_DIRECTORIES "${SQLite3_INCLUDE_DIRS}")
    if(NOT APPLE)
      target_link_libraries(cidx_sqlite_static
        INTERFACE ${CMAKE_DL_LIBS} pthread m)
    endif()
    set(_cidx_sqlite_link cidx_sqlite_static)
    message(STATUS "cidx[static]: SQLite3 (static) = ${CIDX_SQLITE3_STATIC}")
  else()
    message(WARNING "cidx[static]: no libsqlite3.a found — using shared SQLite3")
  endif()
endif()

# ---------------------------------------------------------------------------
# A1 (Amendment A1 — spec §12): link libclang at build time.
# Search order (A1.1):
#   1. CIDX_LIBCLANG cache var (configure-time FILEPATH), if set.
#   2. llvm-config --libdir output directory.
#   3. /opt/llvm*/lib64, /opt/llvm*/lib, /usr/lib/llvm-*/lib,
#      /usr/local/llvm*/lib globs.
#   4. macOS pip-wheel native/ directory (python3 probe, OPTIONAL).
#   5. System default (no PATHS — CMake's normal library search).
# FATAL_ERROR if not resolved.
# ---------------------------------------------------------------------------

set(CIDX_LIBCLANG "" CACHE FILEPATH "Path to libclang shared library (.so/.dylib)")

set(_cidx_libclang_hints "")

# 1. User-provided path (full file → use directly; directory → add as hint).
if(CIDX_LIBCLANG)
  if(EXISTS "${CIDX_LIBCLANG}" AND NOT IS_DIRECTORY "${CIDX_LIBCLANG}")
    set(CIDX_LIBCLANG_LIB "${CIDX_LIBCLANG}")
  else()
    list(APPEND _cidx_libclang_hints "${CIDX_LIBCLANG}")
  endif()
endif()

if(NOT CIDX_LIBCLANG_LIB)
  # 2. llvm-config --libdir (quiet on failure).
  find_program(_llvm_config llvm-config)
  if(_llvm_config)
    execute_process(
      COMMAND "${_llvm_config}" --libdir
      OUTPUT_VARIABLE _llvm_libdir
      OUTPUT_STRIP_TRAILING_WHITESPACE
      ERROR_QUIET
      RESULT_VARIABLE _llvm_config_result
    )
    if(_llvm_config_result EQUAL 0 AND _llvm_libdir)
      list(APPEND _cidx_libclang_hints "${_llvm_libdir}")
    endif()
  endif()

  # 3. Glob-based /opt/llvm* and /usr/lib/llvm-* candidates.
  foreach(_glob_pat
      "/opt/llvm*/lib64" "/opt/llvm*/lib"
      "/usr/lib/llvm-*/lib"
      "/usr/local/llvm*/lib")
    file(GLOB _glob_dirs "${_glob_pat}")
    if(_glob_dirs)
      list(SORT _glob_dirs)
      list(APPEND _cidx_libclang_hints ${_glob_dirs})
    endif()
  endforeach()

  # 4. macOS pip-wheel native/ directory (python3 probe, OPTIONAL).
  find_program(_python3 python3)
  if(_python3)
    execute_process(
      COMMAND "${_python3}" -c
        "import clang.cindex, os; print(os.path.dirname(clang.cindex.__file__)+'/native')"
      OUTPUT_VARIABLE _wheel_native_dir
      OUTPUT_STRIP_TRAILING_WHITESPACE
      ERROR_QUIET
      RESULT_VARIABLE _wheel_probe_result
    )
    if(_wheel_probe_result EQUAL 0 AND _wheel_native_dir AND IS_DIRECTORY "${_wheel_native_dir}")
      list(APPEND _cidx_libclang_hints "${_wheel_native_dir}")
    endif()
  endif()

  # 5. find_library with accumulated hints (+ CMake system default).
  find_library(CIDX_LIBCLANG_LIB
    NAMES clang libclang
    HINTS ${_cidx_libclang_hints}
    DOC "Path to libclang shared library"
  )
endif()

if(NOT CIDX_LIBCLANG_LIB)
  message(FATAL_ERROR
    "libclang not found. Pass -DCIDX_LIBCLANG=/path/to/libclang.{so,dylib} "
    "or install an LLVM development package.")
endif()

get_filename_component(_cidx_libclang_dir "${CIDX_LIBCLANG_LIB}" DIRECTORY)
message(STATUS "libclang: ${CIDX_LIBCLANG_LIB}")
message(STATUS "libclang dir (RPATH): ${_cidx_libclang_dir}")

# How libclang is linked into cidx_core. Default: the resolved shared library
# (RPATH-located at runtime). Under CIDX_STATIC on a non-Apple host, try to
# build a static link line instead.
set(_cidx_libclang_link "${CIDX_LIBCLANG_LIB}")
set(_cidx_libclang_static OFF)

if(CIDX_STATIC AND APPLE)
  message(STATUS "cidx[static]: macOS — libclang stays dynamic "
                 "(no static archive ships; libSystem has no static form)")
elseif(CIDX_STATIC)
  # Linux: link a static libclang. The libclang C API itself ships only as a
  # shared lib everywhere, so a wrapper libclang.a (the clang/tools/libclang
  # C-API objects) must be built first with scripts/build-static-libclang.sh and
  # pointed at via -DCIDX_LIBCLANG_STATIC. We then add the distro's clang/LLVM
  # *component* static archives (regular ELF, libstdc++ — ABI-matched to a g++
  # build of cidx; an LTO/bitcode LLVM will NOT link) and the llvm-config LLVM +
  # system libs, all in one --start-group so the circular refs resolve.
  #   Validated 2026-06-13 on Ubuntu 24.04 apt llvm-18 (18.1.3): the resulting
  #   cidx has NO libclang.so dependency (only libc/libm/libz/libzstd/libtinfo).
  # CIDX_LIBCLANG_STATIC: path to the C-API wrapper archive. Usually passed via
  # -D (printed by build-static-libclang.sh); else try to find one on disk.
  if(NOT CIDX_LIBCLANG_STATIC)
    find_library(CIDX_LIBCLANG_STATIC NAMES libclang.a
      HINTS "${_cidx_libclang_dir}" ${_cidx_libclang_hints}
      DOC "Static libclang.a (C-API wrapper) for CIDX_STATIC")
  endif()
  # llvm-config MUST come from the SAME prefix as the libclang we are linking
  # (its --libdir/--libs select the component archives). Pin it to the resolved
  # libclang's sibling bin/ rather than trusting PATH (which may point at an
  # unrelated/LTO LLVM, e.g. /opt/llvm-*). -DCIDX_LLVM_CONFIG overrides.
  if(CIDX_LLVM_CONFIG)
    set(_cidx_llvm_config "${CIDX_LLVM_CONFIG}")
  elseif(EXISTS "${_cidx_libclang_dir}/../bin/llvm-config")
    set(_cidx_llvm_config "${_cidx_libclang_dir}/../bin/llvm-config")
  else()
    find_program(_cidx_llvm_config NAMES llvm-config llvm-config-18)
  endif()
  if(CIDX_LIBCLANG_STATIC AND EXISTS "${CIDX_LIBCLANG_STATIC}" AND _cidx_llvm_config)
    execute_process(COMMAND "${_cidx_llvm_config}" --libdir
      OUTPUT_VARIABLE _ll_libdir OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
    execute_process(COMMAND "${_cidx_llvm_config}" --ldflags
      OUTPUT_VARIABLE _ll_ldflags OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
    execute_process(COMMAND "${_cidx_llvm_config}" --link-static --libs
      OUTPUT_VARIABLE _ll_libs OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
    execute_process(COMMAND "${_cidx_llvm_config}" --link-static --system-libs
      OUTPUT_VARIABLE _ll_syslibs OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
    separate_arguments(_ll_ldflags)
    separate_arguments(_ll_libs)
    separate_arguments(_ll_syslibs)
    # Drop components llvm-config lists but the distro ships no static .a for
    # (e.g. Polly on apt LLVM) — they would fail with 'cannot find -lPolly'.
    list(FILTER _ll_libs EXCLUDE REGEX "Polly")
    # Clang component archives (libclangAST.a, ...) live beside the LLVM ones.
    file(GLOB _cidx_clang_static_libs "${_ll_libdir}/libclang*.a")
    list(SORT _cidx_clang_static_libs)
    set(_cidx_libclang_link
      ${_ll_ldflags}
      "-Wl,--start-group"
      "${CIDX_LIBCLANG_STATIC}"          # the C-API wrapper archive
      ${_cidx_clang_static_libs}          # clang component archives
      ${_ll_libs}                         # LLVM component archives
      "-Wl,--end-group"
      ${_ll_syslibs})
    set(_cidx_libclang_static ON)
    message(STATUS "cidx[static]: libclang (static) = ${CIDX_LIBCLANG_STATIC}")
    message(STATUS "cidx[static]: LLVM libdir = ${_ll_libdir}")
  else()
    message(WARNING "cidx[static]: no static libclang -> libclang stays dynamic. "
      "Build the C-API wrapper with scripts/build-static-libclang.sh and pass "
      "-DCIDX_LIBCLANG_STATIC=<libclang.a> (needs a non-LTO LLVM + llvm-config).")
  endif()
endif()

# Core library: util + storage + clangx + compiledb + cli + vendored MD5/SHA1.
add_library(cidx_core STATIC
  src/util/logger.cpp
  src/util/env.cpp
  src/util/subprocess.cpp
  src/util/hashing.cpp
  src/util/json_min.cpp
  src/util/pathutil.cpp
  src/util/repo.cpp
  src/util/files.cpp
  src/storage/sqlite.cpp
  src/storage/storage.cpp
  src/clangx/libclang.cpp
  src/clangx/toolchain.cpp
  src/clangx/parse.cpp
  src/clangx/pch.cpp
  src/clangx/ast.cpp
  src/clangx/ast_query.cpp
  src/astcache/astcache.cpp
  src/compiledb/compiledb.cpp
  src/cli/args.cpp
  src/cli/format.cpp
  src/cli/json_out.cpp
  src/cli/kind_names.cpp
  src/cli/commands.cpp
  src/graph/query.cpp
  src/graph/emit.cpp
  src/astgraph/astgraph.cpp
  third_party/md5/md5.c
  third_party/sha1/sha1.c
)

# A1.1: link libclang (shared by default; a static archive group under
# CIDX_STATIC on Linux — see the resolution block above).
target_link_libraries(cidx_core PUBLIC ${_cidx_libclang_link})
# S02: storage layer links SQLite3 (shared, or the static archive under
# CIDX_STATIC).
target_link_libraries(cidx_core PUBLIC ${_cidx_sqlite_link})
target_include_directories(cidx_core PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party
)
target_compile_options(cidx_core PRIVATE
  $<$<COMPILE_LANGUAGE:CXX>:-Wall;-Wextra>
)
# A1.3: bake the resolved libclang path into the binary so library_path()
# returns it without a dladdr() call. PUBLIC so all linked test exes inherit it.
target_compile_definitions(cidx_core PUBLIC
  "CIDX_LIBCLANG_PATH=\"${CIDX_LIBCLANG_LIB}\"")

# Optional native Souffle support for maintained astgraph analyses.  Each rule
# is generated at build time and embedded as an object, while user-authored
# Datalog remains the standalone `souffle` CLI workflow.
option(CIDX_ASTGRAPH_SOUFFLE "Build native Souffle astgraph analyses" ON)
if(CIDX_ASTGRAPH_SOUFFLE)
  find_program(CIDX_SOUFFLE_EXECUTABLE NAMES souffle)
  # Homebrew ships a nested include root; prefer it to the parent include dir
  # so generated <souffle/...> includes select one consistent header tree.
  find_path(CIDX_SOUFFLE_INCLUDE_DIR NAMES souffle/CompiledSouffle.h
    HINTS /opt/homebrew/opt/souffle/include/souffle)
  if(CIDX_SOUFFLE_EXECUTABLE AND CIDX_SOUFFLE_INCLUDE_DIR)
    set(_cidx_astgraph_rule_dir "${CMAKE_CURRENT_BINARY_DIR}/generated/astgraph")
    set(_cidx_astgraph_callgraph_cpp
      "${_cidx_astgraph_rule_dir}/ast_callgraph.cpp")
    add_custom_command(
      OUTPUT "${_cidx_astgraph_callgraph_cpp}"
      COMMAND "${CMAKE_COMMAND}" -E make_directory "${_cidx_astgraph_rule_dir}"
      COMMAND "${CIDX_SOUFFLE_EXECUTABLE}" -g "${_cidx_astgraph_callgraph_cpp}"
              "${CMAKE_CURRENT_SOURCE_DIR}/src/astgraph/rules/ast_callgraph.dl"
      DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/astgraph/rules/ast_callgraph.dl"
      VERBATIM)
    # Souffle 2.5's generated runtime is C++17-compatible but is not C++23
    # clean on current libc++; keep this narrow bridge out of cidx_core.
    add_library(cidx_astgraph_souffle_rules OBJECT
      src/astgraph/souffle_runner.cpp
      "${_cidx_astgraph_callgraph_cpp}")
    set_target_properties(cidx_astgraph_souffle_rules PROPERTIES
      CXX_STANDARD 17
      CXX_STANDARD_REQUIRED ON
      CXX_EXTENSIONS OFF)
    target_compile_definitions(cidx_astgraph_souffle_rules PRIVATE
      CIDX_HAVE_ASTGRAPH_SOUFFLE=1
      __EMBEDDED_SOUFFLE__)
    target_include_directories(cidx_astgraph_souffle_rules PRIVATE
      "${CIDX_SOUFFLE_INCLUDE_DIR}"
      "${CMAKE_CURRENT_SOURCE_DIR}/src")
    set(CIDX_ASTGRAPH_SOUFFLE_ENABLED ON)
    message(STATUS "cidx: native astgraph Souffle = ${CIDX_SOUFFLE_EXECUTABLE}")
  else()
    message(WARNING "cidx: Souffle not found; native astgraph analyses disabled")
  endif()
endif()
if(NOT CIDX_ASTGRAPH_SOUFFLE_ENABLED)
  target_sources(cidx_core PRIVATE src/astgraph/souffle_stub.cpp)
endif()

# A2.1: the stdc++fs workaround for GNU < 9 is removed — minimum is now gcc 13
# where <filesystem> is in libstdc++ proper with no extra link needed.

add_executable(cidx src/main.cpp)
target_compile_options(cidx PRIVATE -Wall -Wextra)
target_link_libraries(cidx PRIVATE cidx_core)

# Static C++ runtime on Linux (AppleClang/libc++ has no static-libstdc++).
if(CIDX_STATIC AND NOT APPLE)
  target_link_options(cidx PRIVATE -static-libstdc++ -static-libgcc)
endif()

# RPATH is only needed while libclang is dynamic. With a static libclang there
# is no shared dependency to locate, so skip it (a leftover RPATH is harmless
# but misleading).
if(NOT _cidx_libclang_static)
  set_target_properties(cidx PROPERTIES
    BUILD_RPATH "${_cidx_libclang_dir}"
    INSTALL_RPATH "${_cidx_libclang_dir}"
    INSTALL_RPATH_USE_LINK_PATH ON
  )
endif()

# cidx-astgraph: dump one TU's AST into <TU>.db for Soufflé/Datalog reasoning
# (src/astgraph/). Shares cidx configuration: compile args come from the same
# index.db, the parse goes through the same Toolchain/Parser as `cidx index`.
add_executable(cidx-astgraph src/astgraph/main.cpp)
target_compile_options(cidx-astgraph PRIVATE -Wall -Wextra)
target_link_libraries(cidx-astgraph PRIVATE cidx_core)
if(CIDX_ASTGRAPH_SOUFFLE_ENABLED)
  target_sources(cidx-astgraph PRIVATE
    $<TARGET_OBJECTS:cidx_astgraph_souffle_rules>)
endif()
if(CIDX_STATIC AND NOT APPLE)
  target_link_options(cidx-astgraph PRIVATE -static-libstdc++ -static-libgcc)
endif()
if(NOT _cidx_libclang_static)
  set_target_properties(cidx-astgraph PROPERTIES
    BUILD_RPATH "${_cidx_libclang_dir}"
    INSTALL_RPATH "${_cidx_libclang_dir}"
    INSTALL_RPATH_USE_LINK_PATH ON
  )
endif()

enable_testing()
add_subdirectory(tests)
