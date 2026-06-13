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

# Core library: util + storage + clangx + compiledb + cli + vendored MD5.
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
  src/clangx/ast.cpp
  src/compiledb/compiledb.cpp
  src/cli/args.cpp
  src/cli/format.cpp
  src/cli/commands.cpp
  third_party/md5/md5.c
)

# A1.1: link libclang directly.
target_link_libraries(cidx_core PUBLIC "${CIDX_LIBCLANG_LIB}")
# S02: storage layer links the system SQLite3.
target_link_libraries(cidx_core PUBLIC SQLite::SQLite3)
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

# A2.1: the stdc++fs workaround for GNU < 9 is removed — minimum is now gcc 13
# where <filesystem> is in libstdc++ proper with no extra link needed.

add_executable(cidx src/main.cpp)
target_compile_options(cidx PRIVATE -Wall -Wextra)
target_link_libraries(cidx PRIVATE cidx_core)
# A1.1: RPATH so the binary runs without LD_LIBRARY_PATH.
set_target_properties(cidx PROPERTIES
  BUILD_RPATH "${_cidx_libclang_dir}"
  INSTALL_RPATH "${_cidx_libclang_dir}"
  INSTALL_RPATH_USE_LINK_PATH ON
)

enable_testing()
add_subdirectory(tests)
