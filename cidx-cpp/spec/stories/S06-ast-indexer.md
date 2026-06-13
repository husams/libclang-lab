# S06 — AST indexer (symbols + headers)

## Goal
Port the cursor walk and symbol extraction: 17-kind map, USR identity, semantic-parent
qualification, subtree/body pruning, decl/def split with resolved-skip + decl-patch, and
header indexing via the including TU with the five skip counters.

## Scope — files to create / modify
```
cidx-cpp/src/clangx/ast.hpp .cpp          # AstIndexer (design §5.8)
cidx-cpp/tests/ast_test.cpp               # EXTEND (S05 created it) with walk/extraction/header cases
```
Modify: `cidx-cpp/tests/CMakeLists.txt` only if test registration changes.

## Spec references
- 01-analysis: §5.6 entire (kind map, `_file_cursors` pruning, `_to_symbol` extraction,
  store policy, main-file matching, `index_headers` semantics + counters), §2.2 (TU freed
  after extraction; one transaction per file), G15, G21, G22, G23, G24, G25, G26;
  env `INDEXER_IGNORE_SYSTEM_HEADERS` falsy set `{0,false,no,off}` (§1.3).
- 02-design: §5.8 (AstIndexer shape, streaming `clang_visitChildren`, frozen kind map +
  function-like set), D13 (linkage/access spelling tables: `no-linkage`,
  `unique-external`, etc.; INVALID → NULL; pure via `clang_CXXMethod_isPureVirtual`),
  D17 (no stale-symbol purge), D19 (`macro` mapped but unreachable), D23 (noexcept
  visitor callbacks, error stashing), §7 (streaming, no cursor collection).

## Acceptance criteria
- `for_file_cursors`: pre-order via `clang_visitChildren`, visitor `noexcept`; child whose
  location file is null or ≠ filename → `CXChildVisit_Continue` (entire subtree pruned,
  G21); function-like kinds (FunctionDecl, CXXMethod, Constructor, Destructor,
  FunctionTemplate) yielded but bodies not walked; everything else recursed. Cursors are
  streamed to a callback — never collected into a vector (design §7).
- `to_symbol`: skip empty USR (G25 anonymous entities WITH a USR are still indexed); kind
  map = the frozen 17 entries exactly; qual_name from semantic parents, empty-spelling
  levels skipped, out-of-line methods qualified by class (G25); displayname; type spelling;
  line/col from expansion location; `is_definition`; pure-virtual; linkage/access via D13
  tables; parent USR unless TU; `resolved = is_definition`; declaration cursors record
  their own site as decl_*, definition cursors leave decl_* null.
- `index_symbols`: one `db.transaction()` wrapping the whole file; store policy = existing
  resolved row → skip (counted) but patch decl_* via `update_symbol` when the cursor
  carries a decl site and the stored row has none (G15); else upsert. Main file matched as
  `tu.spelling` == path exactly as passed (G24). Returns symbol count.
- `index_headers`: iterate `clang_getInclusions` (transitive); dedupe by abspath; skip
  counters exactly `{indexed, symbols, already, system, unowned}`: `system` via
  `clang_getLocation(tu, file, 1, 1)` → `clang_Location_isInSystemHeader`, default-on,
  disabled by the falsy set (G26); `unowned` via `component_for_path`; `already` via file
  row indexed + matching md5. Otherwise: file row created with mtime + md5 and NULL
  options/driver (G20), symbols extracted from THIS TU's AST matched against the include
  **spelling** `inc.include.name`, not the abspath (G23), file marked indexed.
- `macro` kind remains unreachable (options=0 from S05) but stays in the map (G22/D19).
- **Unit tests written and passing** (`ast_test` extensions, label `clang`) — behaviors the
  tests MUST cover, parsing READ-ONLY `libclang-lab/manifests/` samples
  (shapes.c/shapes.h, geometry.cpp/geometry.hpp, project/) plus tmp-dir synthetic sources:
  - kind map: a fixture exercising all 16 reachable kinds (C: struct/union/enum/
    enum-constant/typedef/function/variable; C++: class/method/member/constructor/
    destructor/namespace/class-template/function-template/type-alias) maps to the exact
    stored kind strings; unmapped kinds (e.g. ParmDecl) ignored.
  - decl/def split on shapes.h + shapes.c (project-style): prototype indexed as decl site,
    definition wins file_id/line/col, decl_* preserved; `resolved` sticky.
  - resolved-skip + decl-patch: index .c first then .h — stored definition untouched,
    decl_* patched (G15).
  - subtree pruning: macro/include artifacts from other files absent; function-local
    declarations absent (body pruning).
  - qual_name: `ns::Class::method` from geometry.cpp; anonymous enum/struct levels skipped.
  - linkage spellings: `external`, `internal` (static fn), `no-linkage`; access
    public/protected/private on geometry.hpp members; pure-virtual → `is_pure=1`.
  - header indexing on `manifests/project/`: counters {indexed, symbols, already, system,
    unowned} asserted across two consecutive TU indexes (second sees `already`); system
    headers skipped by default and indexed with `INDEXER_IGNORE_SYSTEM_HEADERS=0`;
    header file row has NULL compile_options/driver.

## Test plan
- `ctest --test-dir build --output-on-failure -L clang -R ast`.
- Fixtures: `libclang-lab/manifests/**` READ-ONLY; component registration done against tmp
  copies' paths is NOT allowed for manifests-based cases — register the manifests dir
  itself as an `external` component in a tmp `:memory:`/tmp-file DB instead (no writes to
  the lab tree). Synthetic multi-kind fixture sources generated in tmp dirs.

## Dependencies
blockedBy: S02, S05. Parallel-safe with S07.
