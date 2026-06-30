# M5 C++ `ast` port — review log (senior-developer, review mode)

Branch: feat/cidx-cpp-ast  (commits 871852b, 852a3ac, 8ce9216 + e111745)
Reference: project/indexer/{astcmd.py, astcache.py}, cli.py:1699-1819, clang/ast.py.
Verdict: CHANGES-REQUESTED.

## Verified byte-identical (empirically, rebuilt binary vs goldens + live Python on same libclang 18.1.1)
- dump_leaf_a.json / locals_badly.json / conditions_shape_area.json: all 3 MATCH.
- kind_names.cpp: 249 kinds, 0 diffs vs live clang.cindex CursorKind.
- SHA-1 cache_key / flags_hash: hand-derived + live Python identical (130a8b8…, ed6e034…).
- json_out::dumps_indent2: faithful to json.dumps(indent=2) (escaping, empty containers, int-arrays-per-line, no trailing ws/newline).
- ParsedTu RAII: dtor disposes TU then Index; move nulls source; copy deleted. subtree_rec recurses OUTSIDE the libclang visitor (correct). No leaks.
- --cache/--no-cache mutex; `ast dump -- -std=c11` REMAINDER both behave identically.
- Scope: 22 files, all in ADR-006 §1 set; no mass reformat.

## BLOCKERS (rubric: unmet parity AC — byte-identical output contract)
- B1 ast_query.cpp:557 — ambiguous --name list omits `format::ljust(s.kind,14)`. C++ `struct ParseFail`, Py `struct         ParseFail`. CONFIRMED.
- B2 commands.cpp:1510-1513 — not-a-function error uses clang_getCursorKindSpelling (StructDecl) not kind_name (STRUCT_DECL). CONFIRMED `'Point' is a StructDecl`. The exact divergence ADR §5.4 warns about.
- B3 ast_query.cpp:437-447 — COMPONENT:// rel missing lstrip('/') before pathutil::join (posixpath join resets on leading '/'). `lab:///manifests/x` → C++ `/manifests/x`, Py full path. CONFIRMED.
- B4 commands.cpp:1849-1850,1899 (cache status) + 2021-2022 (clear) — target detection uses selectors (ast_usr/ast_id/name); Python keys ONLY on args.target. Also dir-not-exist check must run FIRST (Py astcache.py:291). `cache status/clear --name X` → C++ resolves symbol, Py runs bulk/clear-all. CONFIRMED both.
- B5 args.cpp:1690,1694 — cache required/invalid-choice error says `what`; Python dest is `cache_action`. CONFIRMED.
- B6 args.cpp:1703 — cache leaf help hardcodes kAstCacheBuildHelp; `ast cache status -h` prints build usage. CONFIRMED.
- B7 is_function_kind (ast_query.cpp:36) includes CXCursor_ConversionFunction — NOT in Python _FUNCTION_KINDS (5 kinds). A conversion operator would be accepted by locals/conditions in C++, rejected (not-a-function) in Python.

## SHOULD-FIX
- S1 cache status/build JSON is hand-built (commands.cpp:1868-,1882-,1968-) — bypasses json_out, so abspath strings are NOT escaped. Route through dumps_indent2 for guaranteed parity (non-ASCII / backslash path would diverge).
- S2 args.cpp kTopUsage/kTopHelp (24-26) not updated to list `ast`; kCommands (597) does include it → internal inconsistency (`cidx bogus` lists ast, `cidx -h` doesn't).
- S3 cache status/clear help-text wrapping differs from Python (`[-h]` line break) — transcription, lower priority than S2.

## NITS
- N1 not-a-function + no-symbol-USR errors use raw `'name'` not format::py_repr (Python !r). Edge case (names with quotes/non-print).
- N2 gen_kind_names.py (ADR §1/§5.4 deliverable) MISSING; no drift-guard doctest. Table is currently correct but unprotected.
- N3 parity_check.sh `ast` block not added (ADR §8) — qa5 lane.

dev5 self-reported deviations: handlers in commands.cpp (sound — avoids cycle), `Toolchain tc; Parser parser(tc)` per reparse (sound), json_out aliases (sound), delete-else→else-if (sound). None are defects.
