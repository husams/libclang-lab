# AGENTS.md — cidx (Python + C++)

This repo ships **two implementations of the same tool**, `cidx`:

| Implementation | Location | Role |
|----------------|----------|------|
| Python (reference) | `project/indexer/` | Canonical behavior; defines the spec |
| C++ (port) | `cidx-cpp/` | Performance port; must match the reference |

For the full lab/agent guide see [`CLAUDE.md`](./CLAUDE.md).

---

## 🚨 CRITICAL — Python ↔ C++ PARITY RULE (STRICT, NON-NEGOTIABLE)

**Every behavioral change to cidx MUST land in BOTH the Python implementation
(`project/indexer/`) AND the C++ port (`cidx-cpp/`) in the SAME change/PR.
NEVER change one and leave the other.**

This applies to anything that alters observable behavior or the on-disk contract:

- the SQLite schema (tables, columns, indexes, schema version)
- the indexer / write path (symbols, edges, `edge_site`, template rows, USRs)
- CLI subcommands, flags, argument parsing, and their output
- JSON output shape (key names, nesting, ordering) — must be **identical by spec**
- the query/read layer (`GraphQuery`, `cidx graph …`) once it exists on both sides
- compile-command handling, toolchain/driver logic, parse options

### The only way to satisfy this rule

1. Make the change in the Python reference.
2. Make the **equivalent** change in the C++ port **in the same pass**.
3. Run BOTH test suites green: `pytest` (Python) **and** `ctest` (C++).
4. Confirm golden/parity output matches between the two.

### If parity genuinely cannot land in the same PR

That is an **exception, not the default**. It is allowed ONLY when:

- the feature does not yet exist on the other side at all (e.g. the C++ query
  layer is not built yet), **and**
- you explicitly state the gap in the PR description as a tracked follow-up, **and**
- the underlying data contract (schema + written rows) is unchanged, so the
  ports do not silently diverge on disk.

A Python-only or C++-only merge with **no** stated follow-up is a rule violation.

### What does NOT require a port

A change confined to one side's layer that the other side does not have **and
that does not touch the shared data contract** does not require a mirror change
— but you must say so explicitly and record it as a parity follow-up. Example:
a tweak to how Python's `GraphQuery` *serializes* existing `edge_site` rows
needs no indexer change (the rows are already written identically by both
ports); it becomes a requirement only when the C++ query layer is built.

---

## Before you commit a cidx change — checklist

- [ ] Change made in `project/indexer/` (Python reference)
- [ ] Equivalent change made in `cidx-cpp/` (C++ port) — or exception stated in PR
- [ ] Schema version bumped on BOTH sides if the schema changed
- [ ] `pytest` green (Python)
- [ ] `ctest` green (C++)
- [ ] JSON / CLI output verified identical between ports
- [ ] Any intentional parity gap documented as a follow-up in the PR
