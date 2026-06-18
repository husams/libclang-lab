# ADR-007: C++ port of the `graph` command group (M6)

Status: accepted
Date: 2026-06-18
Supersedes: none
Relates-to: ADR-006 (C++ `ast` port — structural template), schema v13 (no change)

## Context

The Python `graph` command group is the read-side graph-query layer over
`index.db` — 8 subcommands (`callers`, `callees`, `refs`, `neighbors`, `walk`,
`path`, `hierarchy`, `dispatch`) backed by `project/indexer/query.py`'s
`GraphQuery` engine (1697 lines) and the `cmd_graph_*` handlers in
`project/indexer/cli.py:1094-1273` with the argparse tree at
`cli.py:1576-1697`. It is currently Python-only — exactly the position `ast` was
in before M5.

M6 backports it to the C++ binary (`cidx-cpp/build/cidx`) with **byte-identical
stdout (text AND `--json`) and identical exit codes** vs Python for the same
index + inputs. `graph` reads `index.db` ONLY — it never parses source, so
unlike `ast` it needs no libclang at runtime (no `astcache`, no `resolve_target`,
no TU parse). The hard parts are (a) replicating `GraphQuery`'s SQL **including
every `ORDER BY`**, (b) replicating the `Sym`/`Edge`/`Site` value types and the
file-path reconstruction + stub/external/`loc` logic, and (c) replicating the
two emitters (`_emit_edges`, `_emit_syms`) and the selector/error machinery
byte-for-byte.

### Hard constraints
- No product-version bump (stays `0.4.0`); no schema change (stays v13).
- NO Python changes. Do not open a PR or merge. Stop at a green branch
  (`feat/cidx-cpp-graph`, already checked out).
- Same wheel libclang 18.1.1 as Python (irrelevant at runtime; graph is DB-only,
  but the parity harness still pins `CIDX_LIBCLANG_LIB` so the *index* both
  tools read is built identically).

### Lesson carried from M5 (ADR-006)
ctest passing is NOT proof. The M5 `ast` port had a hollow green that hid
error-path bugs. Real parity comes from hand byte-diffs on the SAME libclang +
adversarial error-path review. M6 therefore makes `parity_check.sh` (S08) the
primary gate and budgets explicit adversarial review of every error/empty/tie
path enumerated in §Risks.

## Decision

Port `GraphQuery` as a C++ class `cidx::graph::GraphQuery` under the (currently
empty) `cidx-cpp/src/graph/` directory, reusing the M5 scaffolding verbatim:
`cli/json_out` (byte-replica of `json.dumps(indent=2)`), `cli/format`
(`ljust`/`rjust`/`py_str`/`py_repr`), the `--usr/--id/--name` selector pattern,
and the `commands.cpp run_command` dispatch switch. Add NET-NEW **read-only**
edge/edge_site/symbol traversal accessors to `Storage`, each mirroring a
`query.py` SQL string **character-for-character including `ORDER BY`**.

The graph engine lives in `src/graph/` (not `storage/`) because it is pure
read-side traversal logic (BFS, dedup, dispatch closure) layered over Storage —
the same separation Python uses (query.py vs storage.py). Only the raw SQL
accessors go into Storage (where the connection and `symbol_from`/`file_abs_path`
already live).

### Module layout (`cidx-cpp/src/graph/`)

```
src/graph/
  records.hpp     Sym, Edge, Site value structs + to_json() (json_out::Value),
                  loc(), is_stub(); Traversal struct. Mirrors query.py
                  dataclasses Sym/Edge/Site/Traversal. (NEW)
  query.hpp       class GraphQuery: ctor(Storage&), require_edges(),
                  get/find/by_name, edges_in/out, _edges, _sites_for,
                  references, sites, neighbors, _peers, walk, reaches, bases,
                  subclasses, members, overrides/overridden_by,
                  is_virtual_method, dispatch_targets, edge_count,
                  is_resolved. (NEW)
  query.cpp       Implementations — 1:1 with query.py methods used by the 8
                  subcommands. (NEW)
  emit.hpp/.cpp   emit_edges(), emit_syms() — byte-replicas of cli.py
                  _emit_edges / _emit_syms (text table + --json branch). (NEW)
```

The 8 command handlers (`cmd_graph_callers` … `cmd_graph_dispatch`) plus the
shared `_open_graph`/`_select_one`/`_select_symbol`/`_edge_kinds` helpers go into
`commands.cpp` (alongside the existing `cmd_*`), declared in `commands.hpp`,
matching where the `ast` handlers live. `run_command` gains an
`if (args.command == "graph") { ... }` block dispatching on `args.what`.

`args.hpp ParsedArgs` gains graph fields: `graph_usr`, `graph_id`, `graph_name`
(or reuse the existing `ast_usr`/`ast_id`/`name` — see §argparse), plus
`direction` (default `"out"`), `edge` (optional `--edge KINDS`), `graph_depth`
(walk default 3, path default 8), `to_usr`/`to_id`/`to_name`/`to_kind`,
`transitive`, `access` (default `"all"`), and `graph_json`/`graph_limit`
(default 50). `args.cpp parse_args` mirrors the argparse tree (§argparse).

### The compact value types (`graph/records.hpp`)

`GraphQuery` rebuilds an abs-path-bearing `Sym` from a `symbol` row exactly as
`query.py:_sym` (cli.py:606-639): prefer `file_id/line/col`; fall back to
`decl_file_id/decl_line/decl_col`; when both are null, use the raw
`decl_path`/`decl_line`/`decl_col` with `external = (decl_path != null)`. C++
already has `Symbol` (records.hpp) and `Storage::file_abs_path()` — but the graph
`Sym` is a SEPARATE projection because:
- `name = qual_name ?: spelling` (query.py:624) — the graph `Sym.name`.
- `loc` = `basename(file):line` (or just `basename` when line is null, or
  `<no-location>` when file is null) — query.py:135-140.
- `is_stub` = `!resolved && (file == null || external)` — query.py:143-153.
- `to_dict()` is the STABLE JSON schema (query.py:155-172): keys in EXACT order
  `id, usr, spelling, qual_name, kind, type_info, file, line, col,
  is_definition, is_pure, is_static, is_instantiation, is_stub`. Note: `qual_name`
  here = `Sym.name` (the COALESCE), NOT the raw column; `file` = abs path.

`Site.to_dict()` (query.py:247-254) keys: `file, line, col, conditional,
args_sig` — the recv_* provenance fields are NOT serialized (they exist on the
struct for Phase-2/3 reuse but graph output omits them).

`Edge.to_dict(sites)` (query.py:196-216): start from `peer.to_dict()`, then
append `edge_kind` (the edge-kind NAME), `count`, conditionally `base_access`
(when non-null), conditionally `is_virtual` (when non-null, as a bool), and
`sites` (a list of `Site.to_dict()`). Key order matters: peer keys first, then
`edge_kind`, `count`, `[base_access]`, `[is_virtual]`, `sites`.

File-path cache: replicate `query.py:_files()` (query.py:587-604) — one query
joining `file/directory/component`, building `{file_id: (abs_path, comp_name)}`,
loaded once. Path = `join(component.path, directory.path, file.name)` when
`directory.path` is non-empty, else `join(component.path, file.name)`. (C++
`Storage::file_abs_path` does the same join per-id; the graph cache batches it,
and crucially `component` name is captured though graph output never prints it.)

## New read-only Storage accessors (one per query.py SQL, EXACT ORDER BY)

These go into `Storage` (storage.hpp/.cpp) as `const`-friendly readers. Each
returns plain `Symbol`/`Edge`/`EdgeSite` rows (records.hpp); the `Sym`/`Edge`
projection happens in `GraphQuery`. Column SELECT lists reuse `kSymbolColsS`
(aliased `s.`) which already matches `query.py:_SYM_COLS` field set
(query.py:489-494) — verify field-by-field (both include `decl_path`,
`is_instantiation`, `is_static`).

| # | Accessor | Mirrors | SQL (verbatim from query.py) — note the ORDER BY |
|---|----------|---------|---------------------------------------------------|
| A1 | `edge_count()` | query.py:558 | `SELECT COUNT(*) FROM edge` |
| A2 | `graph_resolved() -> bool` | query.py:579-583 | `SELECT value FROM meta WHERE key = 'graph_resolved_at'` → `bool(row && row[0])` |
| A3 | `get_symbol_by_usr(usr)` | query.py:666-668 | `SELECT {_SYM_COLS} FROM symbol s WHERE s.usr = ?` |
| A4 | `get_symbol_by_id(id)` | query.py:666-668 | `SELECT {_SYM_COLS} FROM symbol s WHERE s.id = ?` |
| A5 | `find_symbols(pattern, kind, limit)` | query.py:707-738 | `SELECT {_SYM_COLS} FROM symbol s WHERE COALESCE(s.qual_name, s.spelling) LIKE ? ESCAPE '\'` `[+ AND s.kind = ?]` `ORDER BY LENGTH(COALESCE(s.qual_name, s.spelling)), COALESCE(s.qual_name, s.spelling) LIMIT ?`. **MUST be a NEW accessor — `search_symbols()` differs (qual_name-only, no spelling COALESCE, no LIMIT) — see Risk R1.** LIKE pattern built from `::`-split segments with `%`/`_` escaped, joined by `%`, wrapped `%…%`. |
| A6 | `graph_edges(mine_id, direction, kind_ids, count_resolved, limit)` | query.py:782-813 (`_edges`) | `SELECT e.id AS eid, e.src_id, e.dst_id, e.kind AS ekind, {count_expr} AS ecount, e.count AS rawcount, e.base_access, e.is_virtual, {_SYM_COLS} FROM edge e JOIN symbol s ON s.id = e.{peer} WHERE e.{mine} = ?` `[+ AND e.kind IN (…)]` `ORDER BY ecount DESC, e.kind LIMIT ?`. `mine/peer` = (`dst_id`,`src_id`) for `in`, (`src_id`,`dst_id`) for `out`. `count_expr` = `e.count` when resolved else `(SELECT COUNT(*) FROM edge_site es WHERE es.edge_id = e.id)`. Returns rows carrying (eid, src_id, dst_id, ekind, ecount, rawcount, base_access, is_virtual, +full symbol). |
| A7 | `edge_sites_for(edge_ids)` | query.py:847-852 (`_sites_for`) | `SELECT edge_id, file_id, line, col, conditional, args_sig, recv_src_kind, recv_type_usr, recv_decl_usr, recv_param_pos, recv_type_is_value FROM edge_site WHERE edge_id IN (…) ORDER BY edge_id, file_id, line, col` |
| A8 | `edge_sites_one(edge_id, limit)` | query.py:884-888 (`sites`) | `SELECT file_id, line, col, conditional, args_sig, recv_* FROM edge_site WHERE edge_id = ? ORDER BY file_id, line, col LIMIT ?` |

`graph_edges` is the workhorse; `callers/callees/refs/neighbors/walk/path/
hierarchy/dispatch` are ALL expressed in terms of it (plus its post-processing).
Two existing graph WRITE accessors (`add_edge`, `add_edge_site`) are untouched;
A6-A8 are the missing READ side.

### Per-subcommand mapping (handler → accessor calls → traversal → emit)

All handlers share the prologue: `g = _open_graph(args)` (returns null + prints
`error: <msg>` on missing-index / no-edges, exit 1 — query.py NoIndexError /
NoEdgesError text at query.py:511-516 / 567-571), then `_select_symbol(g, args)`
(query.py selector → `(Sym, rc)`; null → return rc). `--limit` default 50.

1. **callers** (cli.py:1094-1103) — `g.edges_in(sym, ["calls"], limit)`
   → A6 (direction `in`, kind {1}) + A7 sites → `emit_edges(g, edges, args,
   "callers of {name} (@{loc}):")`.

2. **callees** (cli.py:1106-1115) — `g.edges_out(sym, ["calls"], limit)`
   → A6 (direction `out`, kind {1}) + A7 → `emit_edges(…, "callees of {name}
   (@{loc}):")`.

3. **refs** (cli.py:1118-1127) — `g.references(sym, limit)` =
   `edges_in(sym, ["calls","uses"], limit)` → A6 (direction `in`, kinds {1,7})
   + A7 → `emit_edges(…, "references to {name} (@{loc}):")`.

4. **neighbors** (cli.py:1130-1149) — `g._edges(sym, args.direction,
   _edge_kinds(args.edge), limit)` → A6 (direction from `--direction`, kinds from
   `--edge` or null=all). On an unknown edge kind, `_kind_ids` raises
   `ValueError` (query.py:651) → handler prints `error: {e}` to stderr, exit 1.
   Header: `"{direction}-neighbors of {name} (@{loc}) over {edge or 'all'}:"`
   (the literal `args.edge` string, or `"all"`).

5. **walk** (cli.py:1152-1175) — `kinds = _edge_kinds(args.edge) or ("calls",)`;
   `tr = g.walk(sym, kinds, direction, depth=args.depth, max_nodes=args.limit)`
   (query.py:967-1003 BFS) → `nodes = [n for n in tr.nodes if n.id != sym.id]`
   → `emit_syms(nodes, args, "reachable from {name} (@{loc}) over
   {','.join(kinds)} {direction}, depth<={depth}:", depths=tr.depth_by_id)`.
   `Traversal.nodes` sorts by `(depth, name)` (query.py:1670-1673) — REPLICATE
   EXACTLY (see R5). `walk` default `--depth 3`.

6. **path** (cli.py:1178-1208) — second selector resolves the destination via
   `_select_one(g, to_usr, to_id, to_name, to_kind, first)`. `kinds =
   _edge_kinds(args.edge) or ("calls",)`; `chain = g.reaches(src, dst,
   kinds, direction, max_depth=args.depth)` (query.py:1005-1046 BFS shortest
   path). `chain is None` → json: print `"null"`; text: print `"no path from
   {src.name} to {dst.name} over {','.join(kinds)} {direction} within depth
   {depth}"`; **exit 1**. Else `emit_syms(chain, args, "path {src.name} ->
   {dst.name} ({len(chain)-1} hop(s)):")` (no depths → no `dN` column). `path`
   default `--depth 8`.

7. **hierarchy** (cli.py:1211-1245) — `direct = not args.transitive`;
   `bases = g.bases(sym, direct)`, `subs = g.subclasses(sym, direct)`,
   `mems = g.members(sym, access=(null if access=="all" else access))`.
   - `bases` direct = A6 out kind {2} peers; transitive = `walk(sym,["inherits"],
     "out", depth=16).nodes` minus self (query.py:1050-1059).
   - `subclasses` direct = A6 in kind {2}; transitive = walk in depth 16 minus
     self (query.py:1061-1070).
   - `members` = union of A6 out kind {3} (`contains`) peers THEN A6 in kinds
     {8,9} (`field_of`,`method_of`) peers, dedup by id preserving order, then
     access filter (query.py:1072-1105). `members(access)` raises ValueError on a
     bad access → handler prints `error: {e}`, exit 1 (but argparse `choices`
     already constrains `--access`, so this path is unreachable via CLI — keep
     for fidelity).
   - JSON: one object `{symbol, bases[], subclasses[], members[]}` (key order
     fixed). Text: header `"hierarchy of {name} (@{loc}):"` then THREE nested
     `emit_syms` calls with headers `"  bases ({scope}):"`,
     `"  subclasses ({scope}):"`, `"  members:"` where `scope = "all" if
     transitive else "direct"`. **Each nested `emit_syms` re-computes its own
     width and prints its own "N result(s)" line** — see R6.

8. **dispatch** (cli.py:1248-1273) — `targets = g.dispatch_targets(sym)`
   (query.py:1366-1393: self unless pure, then BFS down incoming `overrides`
   kind {6}, skipping pure, dedup), `virtual = g.is_virtual_method(sym)`
   (query.py:1356-1364: pure OR overridden_by OR overrides). JSON: `{method,
   is_virtual, targets[]}`. Text: `note = "" if virtual else "  (not a virtual
   method -- only itself)"`; `emit_syms(targets, args, "run-time dispatch
   targets of {name} (@{loc}){note}:")`. **`dispatch_targets` insertion order is
   `dict.values()` insertion order (query.py:1376-1393): self first (if not
   pure), then override-closure in BFS order** — REPLICATE with an
   insertion-ordered map (R4).

### `emit_edges` / `emit_syms` (graph/emit.cpp — byte-replicas)

`emit_edges` (cli.py:1054-1069):
- `--json`: `dumps_indent2([e.to_dict(sites=g.sites(e)) for e in edges])` + `\n`.
  Note: the per-edge sites passed to `to_dict` come from a FRESH `g.sites(e)`
  call (A8, limit default 200), NOT the eager `e.sites`. This re-queries with the
  `sites` ORDER BY — must match.
- text: print `header`. `width = max(len(peer.name or peer.usr) for e)` (0 when
  empty). Per edge: `cnt = "  x{count}" if count and count != 1 else ""`;
  `sample = g.sites(e, limit=1)`; `site = "  ({sample[0].loc})" if sample else
  ""`; `stub = "  [stub]" if peer.is_stub else ""`; `nm = peer.name or peer.usr`;
  line = `f"  {peer.kind:<14} {nm:<{width}}  @{peer.loc}{cnt}{site}{stub}"`.
  Trailer: `f"{len(edges)} result(s)"`.

`emit_syms` (cli.py:1072-1091):
- `--json`: list of `s.to_dict()`, optionally augmented with
  `d["depth"] = depths.get(s.id)` (key `depth` ADDED LAST when `depths` is not
  null — walk only) + `\n`.
- text: print `header`. `width = max(len(s.name or s.usr))` (0 empty). Per sym:
  `dep = "  d{depths[s.id]}" if depths is not null and s.id in depths else ""`;
  `stub = "  [stub]" if s.is_stub else ""`; `nm = s.name or s.usr`; line =
  `f"  {s.kind:<14} {nm:<{width}}  @{s.loc}{dep}{stub}"`. Trailer
  `f"{len(syms)} result(s)"`.

Reuse `cli::format::ljust` for `:<14` and `:<{width}`. The `{count}`/`{depth}`
ints go through plain decimal (no grouping).

### argparse fidelity (`args.cpp`)

Mirror `cli.py:1576-1697` exactly (NO prefix abbreviation, exit 2 on misuse,
help text byte-verbatim — captured with `COLUMNS=80 python -m indexer graph
<sub> -h`). Structure:
- `graph` parser with a REQUIRED `what` subparser (`required=True` → "the
  following arguments are required: what" style error, exit 2) over the 8 names
  in order callers,callees,refs,neighbors,walk,path,hierarchy,dispatch.
- `_selector(q)` shared block on EVERY subcommand: a REQUIRED mutually-exclusive
  group `(--usr USR | --id N | --name FUZZY)` + `--kind {…17 kinds…}` +
  `--first` + `--db PATH` (dest `graph_db`, overrides `args.index`) + `--json` +
  `--limit N` (default 50). Mutex violation / missing-required → exit 2 with the
  argparse usage block.
- `neighbors`: + `--edge KINDS` (help `edge_help + " (default: all)"`) +
  `--direction {in,out}` (default out).
- `walk`: + `--edge KINDS` (default: calls) + `--direction {in,out}` (out) +
  `--depth N` (default 3).
- `path`: + a SECOND required mutex `(--to-usr | --to-id | --to-name)` +
  `--to-kind {…}` + `--edge KINDS` (default: calls) + `--direction` (out) +
  `--depth N` (default 8). Help/usage interleave order matters (selector block,
  then to-* block, then edge/direction/depth — see captured `path -h`).
- `hierarchy`: + `--transitive` (store_true) + `--access
  {public,protected,private,all}` (default all).
- `dispatch`: selector only.
- `edge_help` = `"comma-separated edge kinds (" + ", ".join(sorted(EDGE_KINDS))
  + ")"` = `calls, contains, field_of, inherits, instantiates, method_of,
  overrides, specializes, uses`.

`main()` already wires `--db`→`args.index` via `graph_db` (cli.py:1817-1819);
the C++ `args.index` override path used by `set/file/dump-cc` (`index_db`) is the
same mechanism — reuse it.

### Plug-in to `run_command` (commands.cpp:2085)

Add before the `list` fallthrough:
```cpp
if (args.command == "graph") {
  if (args.what == "callers")   return cmd_graph_callers(args, ctx);
  if (args.what == "callees")   return cmd_graph_callees(args, ctx);
  if (args.what == "refs")      return cmd_graph_refs(args, ctx);
  if (args.what == "neighbors") return cmd_graph_neighbors(args, ctx);
  if (args.what == "walk")      return cmd_graph_walk(args, ctx);
  if (args.what == "path")      return cmd_graph_path(args, ctx);
  if (args.what == "hierarchy") return cmd_graph_hierarchy(args, ctx);
  return cmd_graph_dispatch(args, ctx);     // "dispatch"
}
```

## Parity strategy (the gate)

1. **Unit/golden ctest** (`tests/graph_query_test.cpp`, new): seed an in-memory
   DB with a tiny edge graph (reuse `graph_storage_test.cpp` fixtures) and assert
   each accessor's row order + each emitter's exact bytes against literals. Pins
   `ORDER BY` and the table formatting independent of libclang.
2. **S08 parity gate** (`scripts/parity_check.sh`): EXTEND `run_script` to append
   `graph` commands after the graphlab import/index/resolve (already present at
   parity_check.sh:263-265). Add one `run_one` per: each subcommand text + `--json`
   over a stable graphlab symbol (use `--id N` or `--usr` to avoid `find()`
   nondeterminism — but ALSO test one `--name … --first` and one ambiguous
   `--name` (no --first) to lock the exit-2 candidate list); empty-result cases
   (callers of a leaf); `path` found + `path` none (exit 1) + `path null --json`;
   `hierarchy --transitive` + `--access public`; `dispatch` on virtual
   (`chain::A::rank`, 4 targets) + non-virtual (note line); `neighbors --edge
   inherits --direction in`; bad `--edge` kind (exit 1 stderr); missing
   `--usr`/`--id` (exit 1); `graph -h` and each `graph <sub> -h` (exit 0,
   byte-exact help); `graph` no-sub (exit 2); arg-error (exit 2). The transcript
   diff is byte-strict; the DB is untouched by graph (read-only) so the existing
   DB-dump diff still passes unchanged.
3. **Adversarial hand-diff** (M5 lesson): for EACH subcommand, run both binaries
   over graphlab on the SAME `CIDX_LIBCLANG_LIB` and `diff` text + json + exit
   code by hand for: empty results, a stub peer (`[stub]` + external `loc`), an
   ambiguous `--name`, `--limit 0`/`--limit 1`, `--depth 0`, and a symbol with no
   `qual_name` (C symbol — exercises the COALESCE in `find()`/`Sym.name`).

Use `--id`/`--usr` selectors in the deterministic transcript assertions wherever
the exact result set matters, since `find()` ties on `(LENGTH, name)` could in
principle order-differ if SQLite collation diverged (it will not — both use the
default BINARY collation — but pinning by id removes the variable).

## Risks (enumerated byte-parity hazards)

- **R1 — `find()` vs `search_symbols()` divergence (HIGH).** Graph `--name` calls
  `query.py find()` which orders on `COALESCE(qual_name, spelling)` WITH a
  `LIMIT ?` and a spelling fallback. The existing C++ `Storage::search_symbols`
  (storage.cpp:1254) is qual_name-ONLY, no LIMIT, no COALESCE — using it would
  silently drop C symbols (no qual_name) and mis-order. **MUST add the NEW
  `find_symbols()` accessor (A5).** `_select_one`'s ambiguous-candidate list and
  the `hits[:25]` slice + `... and N more` line all depend on this exact order.
- **R2 — result ordering of `_edges` (A6) `ORDER BY ecount DESC, e.kind`.** When
  two edges tie on count and kind, SQLite's row order is the tiebreak — IDENTICAL
  for both tools only because they read the SAME `index.db` with the same
  physical row order. Do NOT add an implicit secondary sort. The `count_expr`
  branch (resolved vs not) changes `ecount`, hence order — replicate `is_resolved`
  (A2) exactly; the parity DB is resolved, so `e.count` path is primary, but test
  an UNresolved-index case too (`index` without `resolve`).
- **R3 — count fallback (`_edges` tail, query.py:816-817).** `cnt = ecount; if
  not cnt: cnt = rawcount or 1`. A zero `ecount` (no sites) falls back to
  `rawcount`, then to 1. Replicate the falsy chain (`0` is falsy, `None` is
  falsy) — a naive `ecount` print would show `x0` and diverge.
- **R4 — `dispatch_targets` ordering (HIGH).** Python builds an insertion-ordered
  `dict` (self first if not pure, then BFS-down `overridden_by` order) and returns
  `list(targets.values())`. Use a vector + seen-set (NOT `std::map`, which sorts
  by id). `overridden_by` itself = A6 in kind {6} ordered by `ecount DESC, e.kind`
  — that order feeds the BFS, so it is load-bearing.
- **R5 — `Traversal.nodes` sort `(depth, name)` (HIGH).** walk output sorts by
  `(depth_by_id[id], s.name)`. `s.name` = `qual_name ?: spelling`. Python sort is
  stable + uses Python string `<` (Unicode code-point, == C++ `std::string` byte
  `<` for ASCII; non-ASCII identifiers would diverge — graphlab is ASCII).
  Replicate `(depth, name)` with a stable sort. Equal-name nodes (e.g. two
  `app::scale` overloads at different lines, seen in the dogfood) keep BFS
  discovery order under stable sort — `std::stable_sort` required.
- **R6 — nested `emit_syms` in `hierarchy`.** `hierarchy` text calls `emit_syms`
  THREE times; each prints its OWN `width` and its OWN `"N result(s)"` line, with
  a leading 2-space-indented header. An empty section still prints `"0
  result(s)"`. Do NOT hoist a shared width. `members:` header has NO `(scope)`
  suffix; `bases`/`subclasses` do.
- **R7 — `Sym.to_dict()` / `Edge.to_dict()` / `Site.to_dict()` key ORDER + the
  `qual_name` field meaning.** JSON object member order is insertion order
  (`json_out::Object` is a vector). `Sym.to_dict`'s `qual_name` = the COALESCED
  `Sym.name`, and `file` = the abs path (not the raw column). `Edge.to_dict`
  conditionally includes `base_access`/`is_virtual` only when non-null — for
  `calls`/`uses` edges they ARE null and must be ABSENT (not `null`). The
  `is_virtual` value is a BOOL (`bool(self.is_virtual)`), not the raw int.
- **R8 — `g.sites(e)` re-query in `emit_edges --json`.** The JSON path passes
  `sites=g.sites(e)` (A8, default limit 200) — a DIFFERENT call from the eager
  `_sites_for` (A7) used for the struct's `.sites`. Both share the `file_id,
  line, col` ORDER BY but A8 lacks `edge_id` in the ORDER (single edge) and has a
  LIMIT. For a multi-site edge under both, the rows are the same set; assert the
  emitted order matches A8's `ORDER BY file_id, line, col`.
- **R9 — stub/external `loc` formatting.** `Sym.loc` = `<no-location>` (file
  null), else `basename(file):line`, else just `basename` (line null). External
  stubs (system headers) carry a raw `decl_path` → `loc` shows e.g.
  `string:1300`. The dogfood showed a `std::__1::basic_string::size` stub peer in
  `callees --json` of `main` with `is_stub:true` + an SDK path `file`. The parity
  transcript MUST mask the SDK path? — NO: both tools read the SAME index.db so
  the stored path is identical; do NOT mask (masking would hide a real divergence).
- **R10 — exit codes.** `_open_graph` fail → 1. selector: USR/id not found → 1;
  `--name` no match → 1; `--name` ambiguous (no --first) → **2** (candidate list
  to STDERR). `neighbors`/`walk`/`path`/`hierarchy` ValueError → 1.
  `path` no path → 1. arg/usage errors → 2. Everything else → 0. Replicate each.
- **R11 — empty graph / `--no-graph` index.** `require_edges=True` → `NoEdgesError`
  text (query.py:567-571) exit 1. The standard cidx-cpp self-index HAS edges; a
  `--no-graph` build must produce the identical error string.
- **R12 — `--limit 0`.** Python passes `LIMIT 0` to SQLite (returns 0 rows) — it
  does NOT mean "all" for graph (unlike `search`/`list`'s `0 = all`). Bind the
  raw int; `LIMIT 0` yields an empty table + `"0 result(s)"`.

## Alternatives considered

- **A. Reuse `Storage::search_symbols` for `--name` instead of adding
  `find_symbols`.** Rejected: it is qual_name-only with no LIMIT and no spelling
  COALESCE (storage.cpp:1284-1291) — it would drop C symbols and mis-order,
  breaking R1 directly. The two SQLs are genuinely different and both must exist.
- **B. Put the graph engine inside `Storage` (no `src/graph/`).** Rejected:
  Storage is the SQL layer; the BFS/dedup/dispatch-closure/emit logic is the
  read-side query layer (Python's query.py vs storage.py split). Mixing them
  bloats Storage and diverges from the proven M5 layout (ast logic lived in
  `clangx/ast_query` + `cli`, not in Storage). Only the raw accessors A1-A8 go
  into Storage.
- **C. Emit JSON via the existing strings-only `util/json_min`.** Rejected:
  `json_min` is compact + strings-only; graph JSON needs ints, bools, null, and
  `json.dumps(indent=2)` pretty form — exactly what `cli/json_out` (M5) already
  provides. Reuse `json_out`. No viable alternative.
- **D. Build `Sym` from the existing `Symbol` record directly (no separate graph
  Sym).** Rejected: the graph `Sym` is a projection with derived `name`
  (COALESCE), abs-path `file`, `loc`, `is_stub`, and a DIFFERENT `to_dict` schema
  than `cmd_show_symbol`. A separate struct keeps the projection logic in one
  place mirroring query.py `_sym`.

## Consequences

Positive: `graph` reaches Python↔C++ parity (read-side graph layer no longer
Python-only); the new read accessors A1-A8 are reusable by any future C++
read-side feature; the S08 gate gains 8 subcommands of coverage over the graphlab
corpus.
Negative: more SQL surface in Storage to keep in lockstep with query.py on any
future schema change; the `find_symbols` vs `search_symbols` duplication is a
maintenance hazard (document both, cross-reference).
Follow-ups: NOT in scope for M6 — the `include_instantiations` rollup overloads
(query.py CallerWithContext), `dispatch_selection`/devirt Phase-1/2/3, template
param/arg readers, and `symbols_in_file`/`def_decl_locations` are NOT exposed by
the 8 graph subcommands and are deferred. No version bump, no schema change.

## References

- Python engine: `project/indexer/query.py:497-1393` (GraphQuery), `:1659-1697`
  (Traversal), `:108-258` (Sym/Edge/Site), `:489-494` (_SYM_COLS).
- Python CLI: `project/indexer/cli.py:1054-1273` (emit + cmd_graph_*),
  `:987-1046` (_open_graph/_edge_kinds/_select_one), `:1576-1697` (argparse tree).
- M5 scaffolding: `cidx-cpp/docs/adr/ADR-006-cpp-ast-port.md`,
  `cidx-cpp/src/cli/{json_out,format,args,commands}.{hpp,cpp}`,
  `cidx-cpp/src/storage/storage.cpp:209-238` (kSymbolCols), `:959` (file_abs_path),
  `:1254-1301` (search_symbols — the contrast for R1).
- Records: `cidx-cpp/src/storage/records.hpp:40-96` (Symbol/Edge/EdgeSite).
- Parity gate: `cidx-cpp/scripts/parity_check.sh:263-265` (graphlab import),
  `:202-360` (run_script). Corpus: `manifests/graphlab/`, `manifests/project/`.
- Cognee: `task:cidx-cpp-graph`, `role:architect`.
