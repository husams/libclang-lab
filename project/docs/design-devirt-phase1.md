# cidx Devirtualized Callgraph — Phase 1 Technical Design

Status: design (architect), 2026-06-16
Scope: **Phase 1 only** — the conservative superset + selection map. NO pruning,
NO schema/index change, NO C++ port. Python query/model layer only.
Spec: `~/workspace/wiki/pages/planning/cidx-devirtualized-callgraph.md`

## 0. Phase 1 contract (restated, firm)

1. At every **virtual** call site, expand to ALL `dispatch_targets()` AND attach a
   **selection map** `{ concrete-type → target-method }` derived purely from the
   override/inherits graph + `dispatch_targets()`.
2. Mark a site **UNPRUNABLE** when its candidate set cannot be safely enumerated.
3. **Do NOT change existing `callgraph()` / `callees()` behaviour by default.** The
   selection-map data is ADDITIVE; it is produced only through new methods or an
   opt-in flag. The default walk stays exactly the static-edge walk it is today.
4. No DB write, no new table, no `cidx index` change. Everything is **derivable
   from existing edges** (`overrides`, `inherits`) plus `Sym.parent_usr` and
   `dispatch_targets()` — proven in §5.

Pruning (using a type environment Γ) is **Phase 2** and explicitly out of scope.
Phase 1's only job is to attach enough structured data that Phase 2 can prune
without re-querying clang.

---

## 1. API surface (additive, low-risk)

### 1.1 `query.py` — `GraphQuery.dispatch_selection(method) -> DispatchSite`

The single new primitive. Given a (declared) callee method, it computes the
dispatch site model for a virtual call to that method. Pure read over existing
edges.

```python
def dispatch_selection(self, method) -> "DispatchSite":
    """The Phase-1 conservative dispatch model for a virtual call whose
    *declared* (static) callee is `method`.

    Returns a DispatchSite carrying:
      - receiver_static_type : the Sym of the class that declares `method`
                               (method.parent_usr resolved), or None
      - candidates           : tuple[Selection] — one per dispatch target,
                               each mapping a selecting concrete type to the
                               target method it dispatches to
      - prunable             : False when the candidate set cannot be trusted
                               as complete/enumerable (see §3); True otherwise
      - unprunable_reasons   : tuple[str] explaining why (empty when prunable)

    Read-only and additive: does not touch dispatch_targets()/callees()/
    callgraph() semantics. For a NON-virtual method the result has
    prunable=False, reason 'not-virtual', candidates=() (callers should treat a
    non-virtual callee as an ordinary static edge and not consult this)."""
```

### 1.2 `query.py` — `GraphQuery.virtual_call_sites(fn) -> list[DispatchSite]`

Convenience: every virtual callee of `fn`, each expanded to its `DispatchSite`.
Built on the existing `virtual_callees(fn)`. This is the per-caller view Phase 2
will consume.

```python
def virtual_call_sites(self, fn) -> list[DispatchSite]:
    """For each virtual callee of `fn` (see virtual_callees), the DispatchSite
    expanding it to all run-time targets + the selection map. Non-virtual
    callees are omitted (they are plain static edges)."""
```

### 1.3 `model.py` — `Method.dispatch_selection() -> DispatchSiteModel`

The ergonomic wrapper, mirroring how `Method.dispatch_targets()` wraps
`GraphQuery.dispatch_targets()`. Returns a model-layer value whose
`receiver_static_type` is a `Class` and whose selections carry `Method`/`Class`
entities instead of raw `Sym`s.

### 1.4 `model.py` — `Callable.devirtualized_callgraph(...)` (the opt-in walk)

The existing `callgraph()` generator is **untouched**. We add a sibling
generator that yields the same `(callee, depth)` stream PLUS the dispatch site
at each virtual hop:

```python
def devirtualized_callgraph(self, depth=None, *, fanout=500
                            ) -> "Iterator[CallStep]":
    """Like callgraph(), but each yielded step is a CallStep carrying the
    callee, depth, AND (when the callee is virtual) the DispatchSiteModel for
    that hop. Conservative superset: at a virtual hop the walk still descends
    into the *declared* callee exactly as callgraph() does (behaviour-identical
    expansion); the DispatchSite is ATTACHED metadata, not a change to which
    nodes are visited. Phase 2 will use site.candidates + a type environment to
    decide which targets to keep."""
```

Naming rationale: a separate method name (not a `devirtualize=` kwarg on
`callgraph()`) is the lowest-risk additive shape — the existing generator's
signature, return type, and traversal are provably unchanged because they are
not edited at all. (The spec floats `callgraph(devirtualize=True)`; an ADR
records why we chose a sibling method instead — see ADR-001.)

**Decision on what the Phase-1 walk descends into:** Phase 1 keeps descending
into the single declared callee (today's behaviour) and attaches the candidate
set as data. It does NOT expand the walk into all `dispatch_targets()` by
default, because that WOULD change `callgraph()`'s node set and is the thing
Phase 2 prunes. An explicit superset walk (visit every dispatch target) is
offered as an opt-in `expand_virtual=True` flag on `devirtualized_callgraph`
only — never on `callgraph`.

---

## 2. Data model (in-memory value types)

No DB rows. Two frozen dataclasses in `query.py`, plus thin model-layer mirrors.

### 2.1 `Selection` (one concrete-type → target-method entry)

```python
@dataclass(frozen=True)
class Selection:
    selecting_type: Sym      # the concrete class whose dynamic type selects this
                             # target (the target method's owning record)
    target: Sym              # the concrete method that runs (a dispatch target)
    inherited: bool = False  # True when selecting_type does NOT itself declare
                             # the target (it inherits an ancestor's override) —
                             # reserved for the subtype-closure extension (§2.4)
```

### 2.2 `DispatchSite`

```python
@dataclass(frozen=True)
class DispatchSite:
    receiver_static_type: Optional[Sym]   # declared callee's owning class
    declared_target: Sym                   # the static callee method itself
    candidates: tuple[Selection, ...]      # the full conservative target set
    prunable: bool                         # False => keep fully expanded (sound)
    unprunable_reasons: tuple[str, ...]    # why not prunable (empty if prunable)

    @property
    def targets(self) -> tuple[Sym, ...]:
        return tuple(s.target for s in self.candidates)

    def to_dict(self) -> dict: ...          # stable JSON (id/usr/qual_name keys),
                                            # identical-by-spec to a future C++ port
```

`candidates` is the structured selection map. Concretely, for `a.rank()` on the
chain fixture:

```
DispatchSite(
  receiver_static_type = chain::A,
  declared_target      = chain::A::rank,
  candidates = [ A→A::rank, B→B::rank, C→C::rank, D→D::rank ],
  prunable   = True, unprunable_reasons = ())
```

### 2.3 Derivation (proves "no schema change needed")

`dispatch_selection(method)`:
1. `root = self.get(method)`; `recv = self.get(root.parent_usr)` (the receiver
   static type). Both already exist.
2. `tgts = self.dispatch_targets(root)` — existing transitive override BFS.
3. For each `t in tgts`: `selecting = self.get(t.parent_usr)`; emit
   `Selection(selecting_type=selecting, target=t)`.
4. Compute `prunable` / `unprunable_reasons` per §3.

Everything reads `edge(kind=overrides)`, `edge(kind=inherits)`, and
`symbol.parent_usr` — all present in schema v9. **No new column, no new table.**

### 2.4 Subtype-closure extension (specified, Phase-1-optional, Phase-2-needed)

`dispatch_targets()` returns only methods with bodies, so a concrete subclass
that does NOT re-override (e.g. `E : B` with no `rank()`) is absent from the
naive key set, yet a receiver dynamically typed `E` selects `B::rank`
(validated in §5.3). Phase 2's Γ may carry `{E}` and must still resolve to
`B::rank`.

Phase 1 specifies the resolution rule but keeps the default `candidates` keyed
by target-owner only (cheap, matches `dispatch_targets`). The closure is exposed
as an opt-in:

```python
def dispatch_selection(self, method, *, close_subtypes: bool = False)
```

When `close_subtypes=True`, for every concrete subclass `S` of
`receiver_static_type` that has no own override, add a `Selection(selecting_type=S,
target=<nearest-ancestor override>, inherited=True)`. "Nearest-ancestor override"
is computed by walking `S`'s `bases(recursive)` in inheritance order and taking
the first class whose method appears in `tgts`. This needs only `inherits` +
`overrides` edges — still no schema change. Default is `False` so Phase 1 ships
the minimal map; Phase 2 flips it on.

---

## 3. UNPRUNABLE criteria (keyed to what the index actually exposes)

A `DispatchSite` is **`prunable=False`** (keep fully expanded, sound fallback)
when ANY of the following holds. Each reason string is emitted in
`unprunable_reasons`.

| reason string | condition (index signal) | rationale |
|---|---|---|
| `not-virtual` | `is_virtual_method(method)` is False | not a dispatch site; caller should treat as static edge |
| `no-receiver-type` | `method.parent_usr` is None OR `get(parent_usr)` is None | cannot key the selection map by receiver type |
| `target-stub` | any `t in dispatch_targets` has `t.is_stub` | an override/target lives in an unindexed (system/stdlib) file; the candidate set is incomplete (validated in §5.2: 2 stub targets exist in cidx-cpp) |
| `pure-no-targets` | `dispatch_targets` is empty (pure base, no indexed override) | nothing to dispatch to in-index; cannot enumerate |
| `open-hierarchy` | `receiver_static_type` is non-final AND index is **partial** (cross-TU: `subclasses(recv, recursive)` may be incomplete) | other TUs may add subclasses the index never saw — see §3.1 |

**Not detectable in Phase 1 (documented as a known soundness hole):** calls
through a **function pointer** or **`std::function`**, and calls on a
**type-erased template** receiver. The index records these as either no `calls`
edge or a `calls` edge to a non-virtual target, so `is_virtual_method` returns
False and they never enter `dispatch_selection` as virtual sites at all. They
are therefore handled correctly by *omission* (they stay ordinary static edges
in `callgraph()`), but Phase 1 cannot mark them `unprunable` because it never
sees them as dispatch sites. This is acceptable: Phase 1's contract is that
every site it DOES flag prunable is genuinely enumerable; sites it cannot see
are unaffected and remain at today's (static-edge) behaviour. Closing this is a
Phase-2/extraction concern (the spec's "argument/receiver provenance"). Recorded
as an open risk (§6, R1).

### 3.1 `open-hierarchy` and the partial-index caveat

In a **whole-program** index, a non-`final` class with no further subclasses is
safely closed. In a **partial** index (cidx's normal cross-TU reality), a base
class may have subclasses defined in a TU that was not indexed. Phase 1 cannot
prove closure from a partial index, so:

- If `meta.graph_resolved_at` is set AND the index is treated as whole-program
  (a caller-supplied assumption), `open-hierarchy` is NOT raised.
- Default (cannot assume whole-program): emit `open-hierarchy` as a **soft**
  reason. It still sets `prunable=False` for safety, but is the one reason Phase
  2 can override when the caller asserts whole-program scope (e.g. after `cidx
  resolve` roll-up). Modelled as a separate flag so the policy is explicit:
  `dispatch_selection(method, *, assume_closed_world: bool = False)`.

This keeps Phase 1 sound-by-default (open world ⇒ unprunable ⇒ no prune ⇒
today's behaviour) while leaving the lever for Phase 2.

---

## 4. Files to change & the boundary between them

| File | Adds | Does NOT touch |
|---|---|---|
| `indexer/query.py` | `Selection`, `DispatchSite` dataclasses; `GraphQuery.dispatch_selection()`, `GraphQuery.virtual_call_sites()` | `dispatch_targets`, `callees`, `virtual_callees`, `is_virtual_method`, `_edges`, all existing methods — unchanged |
| `indexer/model.py` | `DispatchSiteModel`, `SelectionModel` wrappers; `Method.dispatch_selection()`; `Callable.devirtualized_callgraph()` + `CallStep` | `Callable.callgraph()`, `Callable.callees()`, `Method.dispatch_targets()` — unchanged |

**Boundary (the existing layering discipline holds):**
- `query.py` owns the **graph derivation** — it reads sqlite, resolves USRs to
  `Sym`, computes targets and the prunable flag. Returns raw `Sym`-bearing
  `DispatchSite`/`Selection`. Zero clang, zero model imports.
- `model.py` owns **ergonomics** — wraps each `Sym` into the typed `Entity`
  (`Method`/`Class`), so a script gets `site.receiver_static_type` as a `Class`
  and `selection.target` as a `Method`. It calls `graph.dispatch_selection()`
  and never re-derives.
- `model.py` is the layer that owns the **walk** (`devirtualized_callgraph`),
  because the walk is an ergonomic concern built on `Method.callees()` exactly
  as today's `callgraph()` is.

**Python-only is correct here:** `model.py` is Python-only by the project's
explicit model-layer exemption (per MEMORY: "cidx model layer ... Python-ONLY by
explicit decision, EXEMPT from C++ parity rule"). The new `DispatchSite` lives in
`query.py`, whose C++ port is **not yet written** (per task brief), so there is
no C++ counterpart to keep in parity *this pass*. The `to_dict()` shape is
specified identical-by-spec so the eventual C++ port matches — but porting is
out of Phase 1 scope.

---

## 5. Validation against the real code (cidx graph API)

All results below are from the standard index (`~/.cache/cidx/index.db`,
4983 symbols / 17020 edges, 40 `overrides`) via
`indexer.query.open_query()`.

### 5.1 The selection map is derivable for a real virtual call

`doctest::reportFatal` (`doctest.h:4908`) calls virtual
`doctest::IReporter::test_case_exception`:

```
virtual callee  doctest::IReporter::test_case_exception
receiver static type (callee owner) = doctest::IReporter
3 dispatch targets, selecting-types =
    ['doctest::XmlReporter', 'doctest::JUnitReporter', 'doctest::ConsoleReporter']
```

So `dispatch_selection(IReporter::test_case_exception)` yields
`candidates = [XmlReporter→XmlReporter::test_case_exception,
JUnitReporter→…, ConsoleReporter→…]`, `receiver_static_type = IReporter`,
`prunable = True`. This is the exact shape §2 specifies, computed from existing
`overrides` edges + `parent_usr` — **no schema change**.

### 5.2 The `target-stub` unprunable signal fires on real data

Scanning all 16 virtual bases in cidx-cpp: **2 dispatch targets are stubs**
(`Sym.is_stub == True`) — overrides that resolve into unindexed headers. Those
sites correctly get `prunable=False, reason='target-stub'`, satisfying the
soundness gate. `parent_usr → record` resolved for **0 missing** owners across
the probed targets, so the selection-map keying is reliable.

### 5.3 The inherited-override precision gap is real (drives §2.4)

A purpose-built fixture (`struct A{virtual rank}; B:A overrides; E:B does not`):

```
dispatch_targets(A::rank) = { A::rank (owner A), B::rank (owner B) }
subclasses(A, recursive)  = { B, E }
```

`E` is a concrete subclass but absent from `dispatch_targets` (it has no own
body). A receiver dynamically typed `E` runs `B::rank`. This is exactly why
§2.4 specifies the optional subtype-closure (`E → B::rank, inherited=True`) and
why Phase 1 keeps it opt-in: the minimal map matches `dispatch_targets`, and the
closure is computable from `inherits`+`overrides` with no schema change.

### 5.4 `edge.is_virtual` is unreliable — must use `is_virtual_method`

Probing `SELECT is_virtual, COUNT(*) FROM edge WHERE kind=1` returns
`is_virtual = NULL` for **all 9183** `calls` edges. **Design consequence:** a
virtual call site is detected via `is_virtual_method(callee)` (override-graph
based), NOT via the `calls` edge's `is_virtual` column. `virtual_call_sites()`
therefore filters `callees()` through `is_virtual_method`, exactly as the
existing `virtual_callees()` already does. (If a later extraction pass starts
populating `edge.is_virtual`, it becomes an optimization, not a correctness
dependency.)

---

## 6. Open questions / risks

- **R1 (soundness hole, documented):** function-pointer / `std::function` /
  type-erased template calls are invisible to Phase 1 as *virtual* sites
  (no virtual `calls` edge), so they cannot be flagged `unprunable`. They remain
  static edges (today's behaviour) — sound for Phase 1, but Phase 2 pruning must
  NOT assume the absence of a dispatch site means a non-virtual call. Mitigation:
  Phase 2's provenance extraction (spec §"What must be captured") is where these
  become visible.
- **R2 (open-world vs whole-program):** `open-hierarchy` defaults to unprunable.
  If too conservative in practice (every cross-TU virtual becomes unprunable),
  the `assume_closed_world` lever (§3.1) is the escape; needs a measurement on a
  real multi-TU index before Phase 2 tuning.
- **R3 (multiple inheritance / diamond):** `dispatch_targets` BFS dedups by id,
  so a diamond override is counted once — correct for the target set, but the
  subtype-closure "nearest ancestor" rule (§2.4) is ambiguous under MI. Phase 1
  ships closure OFF, so this is deferred; Phase 2 must pick a tie-break (e.g.
  emit all reachable ancestor overrides, stay sound).
- **R4 (covariant returns / overload sets):** an override with a covariant
  return type is still an `overrides` edge, so it is captured. Overloaded
  virtuals (same name, different signature) are distinct USRs and distinct
  `overrides` chains — handled per-USR, no special case. Confirm with a fixture
  in the test suite.
- **R5 (naming):** `devirtualized_callgraph` vs a `callgraph(devirtualize=True)`
  kwarg — resolved in ADR-001 (sibling method). Revisit only if the duplicated
  walk skeleton becomes a maintenance burden (could refactor the shared DFS into
  a private `_walk_calls` both call).

## 7. Testing approach (for the implementer)

- Unit: seed an in-memory index over the §5.3 chain+E fixture via the
  `tests/test_template_instances.py` harness (`clang_args` + `index_symbols` +
  `_index_edges_notxn`, wrap in `CodeBase`). Assert:
  - `dispatch_selection(A::rank).candidates == {A→A::rank, B→B::rank}`,
    `prunable True`.
  - with `close_subtypes=True`: adds `E→B::rank, inherited=True`.
  - a stub-target fixture ⇒ `prunable False, 'target-stub'`.
  - a pure base with no override ⇒ `'pure-no-targets'`.
- Regression: assert `callgraph()` output (node set + order) is **byte-identical**
  before/after — the no-behaviour-change invariant. Add this to `test_model.py`.
- Place new query tests in `tests/test_query.py`, model tests in
  `tests/test_model.py` (existing files).
