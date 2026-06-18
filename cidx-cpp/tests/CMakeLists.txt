# One doctest executable per module (design §8, D21). All S01 suites are
# hermetic: label "default" — no libclang, no network, no Python at runtime.
# env_logger_test also hosts the subprocess coverage (design §3: no separate
# exe).
set(CIDX_DEFAULT_TESTS
  pathutil_test
  json_min_test
  hashing_test
  env_logger_test
  storage_smoke_test
  storage_migration_test
  graph_storage_test
  fuzzy_match_test
  repo_test
)

foreach(test_name IN LISTS CIDX_DEFAULT_TESTS)
  add_executable(${test_name} ${test_name}.cpp)
  target_compile_options(${test_name} PRIVATE -Wall -Wextra)
  target_link_libraries(${test_name} PRIVATE cidx_core)
  add_test(NAME ${test_name} COMMAND ${test_name})
  set_tests_properties(${test_name} PROPERTIES LABELS "default")
endforeach()

# S02: the migration suite opens committed Python-generated fixture DBs
# (copied to a temp dir first — the fixtures themselves stay read-only).
target_compile_definitions(storage_migration_test PRIVATE
  CIDX_FIXTURES_DIR="${CMAKE_CURRENT_SOURCE_DIR}/fixtures")

# CIDX_MANIFESTS_DIR can be overridden at cmake configure time to simulate an
# environment where the lab fixtures are absent (e.g. the e2e box that rsyncs
# only cidx-cpp/).  Default: the sibling manifests/ tree.
if(NOT DEFINED CIDX_MANIFESTS_DIR)
  get_filename_component(_cidx_manifests_dir
    "${CMAKE_CURRENT_SOURCE_DIR}/../../manifests" ABSOLUTE)
  set(CIDX_MANIFESTS_DIR "${_cidx_manifests_dir}")
endif()

# --- S03: compiledb_test — one exe, two ctest registrations -----------------
# Hermetic strip/sanitize/driver/major-regex cases run under the "default"
# label; the CompileDb::load cases live in doctest suite "clang" and are
# registered separately under the "clang" label.
# A1 (Amendment A1): libclang is linked at build time — SKIP-77 is kept here
# only for the fixture-gap case (CIDX_MANIFESTS_DIR absent), not for "no dylib".
add_executable(compiledb_test compiledb_test.cpp)
target_compile_options(compiledb_test PRIVATE -Wall -Wextra)
target_link_libraries(compiledb_test PRIVATE cidx_core)
target_compile_definitions(compiledb_test PRIVATE
  CIDX_MANIFESTS_DIR="${CIDX_MANIFESTS_DIR}")

add_test(NAME compiledb_test COMMAND compiledb_test --test-suite-exclude=clang)
set_tests_properties(compiledb_test PROPERTIES LABELS "default")

add_test(NAME compiledb_clang_test COMMAND compiledb_test --test-suite=clang)
set_tests_properties(compiledb_clang_test PROPERTIES
  LABELS "clang"
  SKIP_RETURN_CODE 77)

# --- S04: toolchain_test — one exe, two ctest registrations ------------------
# Hermetic cases (fake-driver fixture + env injection + Toolchain test seams)
# run under "default"; the real-LibClang::major() gnuc-cap smoke lives in
# doctest suite "clang".
# A1: SKIP-77 kept for fixture-gap only (no "no dylib" path anymore).
add_executable(toolchain_test toolchain_test.cpp)
target_compile_options(toolchain_test PRIVATE -Wall -Wextra)
target_link_libraries(toolchain_test PRIVATE cidx_core)
target_compile_definitions(toolchain_test PRIVATE
  CIDX_FIXTURES_DIR="${CMAKE_CURRENT_SOURCE_DIR}/fixtures")

add_test(NAME toolchain_test COMMAND toolchain_test --test-suite-exclude=clang)
set_tests_properties(toolchain_test PROPERTIES LABELS "default")

add_test(NAME toolchain_clang_test COMMAND toolchain_test --test-suite=clang)
set_tests_properties(toolchain_clang_test PROPERTIES
  LABELS "clang"
  SKIP_RETURN_CODE 77)

# --- S05: ast_test — one exe, two ctest registrations -------------------------
# The final-argv assembly case is hermetic ("default" label); the parse +
# diagnostic-policy suite performs real parses, lives in doctest suite "clang".
# S06 extends this same exe with the walk/extraction cases.
# A1: SKIP-77 kept for fixture-gap (CIDX_MANIFESTS_DIR absent); not for dylib.
add_executable(ast_test ast_test.cpp)
target_compile_options(ast_test PRIVATE -Wall -Wextra)
target_link_libraries(ast_test PRIVATE cidx_core)
# S06: the walk/extraction suites parse the lab manifests READ-ONLY.
target_compile_definitions(ast_test PRIVATE
  CIDX_MANIFESTS_DIR="${CIDX_MANIFESTS_DIR}")

add_test(NAME ast_test COMMAND ast_test --test-suite-exclude=clang)
set_tests_properties(ast_test PROPERTIES LABELS "default")

add_test(NAME ast_clang_test COMMAND ast_test --test-suite=clang)
set_tests_properties(ast_clang_test PROPERTIES
  LABELS "clang"
  SKIP_RETURN_CODE 77)

# --- S07: cli_test — one exe, two ctest registrations -------------------------
# Args-grammar, format, add-source, and query-golden cases are hermetic
# ("default" label); the cmd_import cases need CompileDb::load
# (CXCompilationDatabase), live in doctest suite "clang".
# A1: SKIP-77 kept for fixture-gap only.
add_executable(cli_test cli_test.cpp)
target_compile_options(cli_test PRIVATE -Wall -Wextra)
target_link_libraries(cli_test PRIVATE cidx_core)
target_compile_definitions(cli_test PRIVATE
  CIDX_MANIFESTS_DIR="${CIDX_MANIFESTS_DIR}")

add_test(NAME cli_test COMMAND cli_test --test-suite-exclude=clang)
set_tests_properties(cli_test PROPERTIES LABELS "default")

add_test(NAME cli_clang_test COMMAND cli_test --test-suite=clang)
set_tests_properties(cli_clang_test PROPERTIES
  LABELS "clang"
  SKIP_RETURN_CODE 77)

# --- S09: ast_query_test — M5 AST command group unit tests (ADR-006 §8.1) -----
# "default" suite: json_out, kind_names, group_thousands, argparse (hermetic).
# "clang" suite: real parse + golden comparison over CIDX_MANIFESTS_DIR.
# CIDX_FIXTURES_DIR must cover tests/fixtures/m5/ goldens.
add_executable(ast_query_test ast_query_test.cpp)
target_compile_options(ast_query_test PRIVATE -Wall -Wextra)
target_link_libraries(ast_query_test PRIVATE cidx_core)
target_compile_definitions(ast_query_test PRIVATE
  CIDX_MANIFESTS_DIR="${CIDX_MANIFESTS_DIR}"
  CIDX_FIXTURES_DIR="${CMAKE_CURRENT_SOURCE_DIR}/fixtures")

add_test(NAME ast_query_test COMMAND ast_query_test --test-suite-exclude=clang)
set_tests_properties(ast_query_test PROPERTIES LABELS "default")

add_test(NAME ast_query_clang_test COMMAND ast_query_test --test-suite=clang)
set_tests_properties(ast_query_clang_test PROPERTIES
  LABELS "clang"
  SKIP_RETURN_CODE 77)

# --- S10: astcache_test — M5 cache tests (ADR-006 §8.1, mirrors test_astcache.py)
# All tests need real libclang parses ("clang" suite only).
add_executable(astcache_test astcache_test.cpp)
target_compile_options(astcache_test PRIVATE -Wall -Wextra)
target_link_libraries(astcache_test PRIVATE cidx_core)
target_compile_definitions(astcache_test PRIVATE
  CIDX_MANIFESTS_DIR="${CIDX_MANIFESTS_DIR}")

add_test(NAME astcache_test COMMAND astcache_test --test-suite=clang)
set_tests_properties(astcache_test PROPERTIES
  LABELS "clang"
  SKIP_RETURN_CODE 77)

# --- S08: parity gate (label "parity"; dev box only — needs uv + the Python
# cidx launcher + sqlite3). The script self-checks its prerequisites and
# fails loudly rather than skipping. scripts/e2e_librdkafka.sh (label "e2e")
# is deliberately NOT registered: it drives the remote test box
# 192.168.1.115 and is a manual release gate (design §8).
add_test(NAME parity_check
  COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/../scripts/parity_check.sh)
set_tests_properties(parity_check PROPERTIES
  LABELS "parity"
  ENVIRONMENT "CIDX_CPP_BIN=$<TARGET_FILE:cidx>")
