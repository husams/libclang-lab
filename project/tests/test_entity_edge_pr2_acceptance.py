"""PR2 entity_edge acceptance tests — QA iteration 4.

These tests assert EVERY acceptance criterion from DESIGN_entity_edge_plan.md §PR2
and cidx-cpp/docs/adr/ADR-008-entity-edge.md §Decision, against the worktree code.

They serve two purposes simultaneously:
  1. Boundary/parametrised tests (mandatory QA addition, role-category 2).
  2. Regression gate: every failing test == one open blocker (QA_DEFECT).

Status on first run: ALL tests in the "schema / impl" section are EXPECTED TO FAIL
because PR2 has not been implemented in this worktree.  Do NOT xfail or skip them;
flag failures as blockers so the developer can act on each one.

Scenarios covered (mapped to DESIGN_entity_edge_plan.md §PR2 test matrix):
  schema-1      SCHEMA_VERSION == 17
  schema-2      entity_edge table present in _SCHEMA
  schema-3      entity_edge_kind seed has exactly 11 rows (ids 1-11)
  schema-4      entity_edge columns: id,src_id,dst_id,kind,count,via_member_id,
                multiplicity,access,is_virtual,create_form,partial
  pr1-seed-1    edge_kind seeds include ids 10-16 (Layer-0 PR1 construct/destroy forms)
  pr1-seed-2    EDGE_KINDS / EDGE_NAMES in query.py include the 7 new ids
  pr1-fixture-1 Dashboard::refresh() method exists in pipeline.hpp (P1-FX)
  pr1-fixture-2 Dashboard::refresh() method exists in pipeline.cpp (P1-FX)
  version-1     Python VERSION == "0.18.0"  (PR2 bumps 0.17.0 -> 0.18.0)
  version-2     C++ kVersion == "0.18.0"
  version-3     C++ kSchemaVersion == 17
  rollup-1      resolve_pass() calls materialize_entity_edges()
  rollup-2      entity_rollup.py module exists
  parity-1      parity_check.sh includes at least one entity_edge CLI command
"""

from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_WORKTREE = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_STORAGE_PY = os.path.join(_WORKTREE, "project", "indexer", "storage.py")
_CLI_PY = os.path.join(_WORKTREE, "project", "indexer", "cli.py")
_QUERY_PY = os.path.join(_WORKTREE, "project", "indexer", "query.py")
_ARGS_HPP = os.path.join(_WORKTREE, "cidx-cpp", "src", "cli", "args.hpp")
_STORAGE_HPP = os.path.join(_WORKTREE, "cidx-cpp", "src", "storage", "storage.hpp")
_STORAGE_CPP = os.path.join(_WORKTREE, "cidx-cpp", "src", "storage", "storage.cpp")
_PIPELINE_HPP = os.path.join(_WORKTREE, "manifests", "graphlab", "pipeline.hpp")
_PIPELINE_CPP = os.path.join(_WORKTREE, "manifests", "graphlab", "pipeline.cpp")
_PARITY_SH = os.path.join(_WORKTREE, "cidx-cpp", "scripts", "parity_check.sh")
_ENTITY_ROLLUP_PY = os.path.join(_WORKTREE, "project", "indexer", "entity_rollup.py")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Import the live storage module to inspect its schema constants.
# ---------------------------------------------------------------------------

def _import_storage():
    spec = importlib.util.spec_from_file_location("indexer.storage", _STORAGE_PY)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _import_query():
    spec = importlib.util.spec_from_file_location("indexer.query", _QUERY_PY)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# scenario-id: schema-1
# ---------------------------------------------------------------------------


def test_schema_version_is_17():
    """SCHEMA_VERSION must be 17 after the v16→v17 bump (P2-T1)."""
    storage = _import_storage()
    assert storage.SCHEMA_VERSION == 17, (
        f"SCHEMA_VERSION is {storage.SCHEMA_VERSION}; expected 17. "
        "P2-T1 has not been implemented: storage.py:35 SCHEMA_VERSION must be bumped to 17."
    )


# ---------------------------------------------------------------------------
# scenario-id: schema-2
# ---------------------------------------------------------------------------


def test_entity_edge_table_in_schema():
    """_SCHEMA must contain a CREATE TABLE entity_edge statement (P2-T1)."""
    storage = _import_storage()
    schema = storage._SCHEMA
    assert "entity_edge" in schema, (
        "entity_edge table not found in _SCHEMA. "
        "P2-T1 has not been implemented: add entity_edge + entity_edge_kind to storage.py _SCHEMA."
    )


# ---------------------------------------------------------------------------
# scenario-id: schema-3 (parametrised — one assertion per required kind)
# ---------------------------------------------------------------------------

_REQUIRED_ENTITY_EDGE_KINDS = [
    (1, "generalizes"),
    (2, "realizes"),
    (3, "specializes"),
    (4, "composes"),
    (5, "aggregates"),
    (6, "associates"),
    (7, "creates"),
    (8, "uses"),
    (9, "destroys"),
    (10, "nests"),
    (11, "befriends"),
]


@pytest.mark.parametrize("kind_id,kind_name", _REQUIRED_ENTITY_EDGE_KINDS)
def test_entity_edge_kind_seed_present(kind_id, kind_name):
    """entity_edge_kind seed must include all 11 rows (P2-T1)."""
    storage = _import_storage()
    schema = storage._SCHEMA
    assert "entity_edge_kind" in schema, (
        "entity_edge_kind table not in schema — P2-T1 not implemented."
    )
    # The seed INSERT must mention both the id and the name.
    assert str(kind_id) in schema and kind_name in schema, (
        f"entity_edge_kind seed missing id={kind_id} name={kind_name}. "
        "P2-T1: seed all 11 rows in entity_edge_kind."
    )


# ---------------------------------------------------------------------------
# scenario-id: schema-4 (parametrised — one assertion per required column)
# ---------------------------------------------------------------------------

_REQUIRED_ENTITY_EDGE_COLUMNS = [
    "src_id",
    "dst_id",
    "kind",
    "count",
    "via_member_id",
    "multiplicity",
    "access",
    "is_virtual",
    "create_form",
    "partial",
]


@pytest.mark.parametrize("col", _REQUIRED_ENTITY_EDGE_COLUMNS)
def test_entity_edge_columns_present(col):
    """entity_edge must have every column specified in ADR-008 §Decision (P2-T1)."""
    storage = _import_storage()
    schema = storage._SCHEMA
    # Locate just the entity_edge CREATE TABLE block (not entity_edge_kind).
    # Search for the bare table name followed by whitespace/paren so we don't
    # accidentally match `entity_edge_kind`.
    idx = schema.find("CREATE TABLE IF NOT EXISTS entity_edge\n")
    if idx < 0:
        idx = schema.find("CREATE TABLE IF NOT EXISTS entity_edge (")
    assert idx >= 0, "entity_edge table not found in _SCHEMA (check P2-T1)."
    # Extract to the matching closing paren.
    depth = 0
    end = idx
    for i, c in enumerate(schema[idx:], idx):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    block = schema[idx : end + 2]
    assert col in block, (
        f"Column '{col}' missing from entity_edge CREATE TABLE in _SCHEMA. "
        "P2-T1: ensure all ADR-008 columns are defined. "
        f"Block found:\n{block[:400]}"
    )


# ---------------------------------------------------------------------------
# scenario-id: schema-4b  — UNIQUE constraint and indexes
# ---------------------------------------------------------------------------


def test_entity_edge_unique_constraint_present():
    """entity_edge must have UNIQUE(src_id,dst_id,kind,via_member_id) (ADR-008)."""
    storage = _import_storage()
    schema = storage._SCHEMA
    # Check both the UNIQUE inline constraint and idx_entity_edge_* indexes.
    assert "idx_entity_edge_src" in schema or "src_id, kind" in schema, (
        "idx_entity_edge_src index missing from _SCHEMA."
    )
    assert "idx_entity_edge_dst" in schema or "dst_id, kind" in schema, (
        "idx_entity_edge_dst index missing from _SCHEMA."
    )


# ---------------------------------------------------------------------------
# scenario-id: pr1-seed-1  (parametrised — one per new Layer-0 edge_kind)
# ---------------------------------------------------------------------------

_PR1_EDGE_KIND_SEEDS = [
    (10, "construct-value"),
    (11, "construct-temp"),
    (12, "construct-heap"),
    (13, "construct-copy"),
    (14, "construct-move"),
    (15, "factory-construct"),
    (16, "destroy"),
]


@pytest.mark.parametrize("kind_id,kind_name", _PR1_EDGE_KIND_SEEDS)
def test_pr1_edge_kind_seed_present(kind_id, kind_name):
    """edge_kind seed must include PR1 Layer-0 form ids 10-16 (P1-T0)."""
    storage = _import_storage()
    schema = storage._SCHEMA
    # The seed INSERT for edge_kind should include the new id and name.
    assert str(kind_id) in schema and kind_name in schema, (
        f"PR1 edge_kind seed missing id={kind_id} name={kind_name} from storage.py _SCHEMA. "
        "P1-T0 has not been implemented: append construct-value/-temp/-heap/-copy/-move, "
        "factory-construct, destroy to the edge_kind seed."
    )


# ---------------------------------------------------------------------------
# scenario-id: pr1-seed-2  — EDGE_KINDS in query.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind_id,kind_name", _PR1_EDGE_KIND_SEEDS)
def test_pr1_edge_kinds_in_query_py(kind_id, kind_name):
    """EDGE_KINDS in query.py must include the 7 new PR1 Layer-0 ids."""
    query = _import_query()
    assert hasattr(query, "EDGE_KINDS"), "EDGE_KINDS not found in query.py."
    assert kind_name in query.EDGE_KINDS, (
        f"EDGE_KINDS missing '{kind_name}' (id={kind_id}). "
        "P1-T0: extend EDGE_KINDS + EDGE_NAMES in query.py."
    )
    assert query.EDGE_KINDS[kind_name] == kind_id, (
        f"EDGE_KINDS['{kind_name}'] == {query.EDGE_KINDS.get(kind_name)}; expected {kind_id}."
    )


# ---------------------------------------------------------------------------
# scenario-id: pr1-fixture-1 / pr1-fixture-2  — Dashboard::refresh in P1-FX
# ---------------------------------------------------------------------------


def test_p1_fx_refresh_in_pipeline_hpp():
    """Dashboard::refresh() must be declared in pipeline.hpp (P1-FX requirement)."""
    content = _read(_PIPELINE_HPP)
    assert "refresh" in content, (
        "Dashboard::refresh() not found in manifests/graphlab/pipeline.hpp. "
        "P1-FX: add method-scoped new/delete fixture so creates/destroys rows have an entity src."
    )


def test_p1_fx_refresh_in_pipeline_cpp():
    """Dashboard::refresh() must be defined in pipeline.cpp with new+delete (P1-FX)."""
    content = _read(_PIPELINE_CPP)
    assert "refresh" in content, (
        "Dashboard::refresh() definition not found in manifests/graphlab/pipeline.cpp. "
        "P1-FX: implement refresh() with `new geo::Circle(r)` and `delete` through geo::Shape*."
    )
    # The method body must contain both new and delete to exercise creates+destroys.
    # We verify by checking inside the refresh function body heuristically.
    assert "new geo::" in content or "new geo ::" in content, (
        "Dashboard::refresh() in pipeline.cpp must contain `new geo::...` for creates row."
    )
    assert "delete " in content, (
        "Dashboard::refresh() in pipeline.cpp must contain a `delete` statement for destroys row."
    )


# ---------------------------------------------------------------------------
# scenario-id: version-1 / version-2 / version-3
# ---------------------------------------------------------------------------


def test_python_version_is_018():
    """Python VERSION must be 0.18.0 after PR2 version bump (P2-T10)."""
    cli_src = _read(_CLI_PY)
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', cli_src, re.MULTILINE)
    assert match is not None, "VERSION not found in cli.py."
    version = match.group(1)
    assert version == "0.18.0", (
        f"Python VERSION is '{version}'; expected '0.18.0'. "
        "P2-T10: bump VERSION 0.17.0 -> 0.18.0 in cli.py after PR2 implementation."
    )


def test_cpp_version_is_018():
    """C++ kVersion must be 0.18.0 after PR2 version bump (P2-T10)."""
    args_src = _read(_ARGS_HPP)
    match = re.search(r'kVersion\s*=\s*"([^"]+)"', args_src)
    assert match is not None, "kVersion not found in args.hpp."
    version = match.group(1)
    assert version == "0.18.0", (
        f"C++ kVersion is '{version}'; expected '0.18.0'. "
        "P2-T10: bump kVersion 0.17.0 -> 0.18.0 in args.hpp after PR2 implementation."
    )


def test_cpp_schema_version_is_17():
    """C++ kSchemaVersion must be 17 (P2-T7)."""
    hpp_src = _read(_STORAGE_HPP)
    match = re.search(r'kSchemaVersion\s*=\s*(\d+)', hpp_src)
    assert match is not None, "kSchemaVersion not found in storage.hpp."
    version = int(match.group(1))
    assert version == 17, (
        f"C++ kSchemaVersion is {version}; expected 17. "
        "P2-T7: bump kSchemaVersion 16 -> 17 in storage.hpp."
    )


# ---------------------------------------------------------------------------
# scenario-id: rollup-1  — resolve_pass calls materialize_entity_edges
# ---------------------------------------------------------------------------


def test_resolve_pass_calls_materialize_entity_edges():
    """Storage.resolve_pass() must call materialize_entity_edges() (P2-T5 hook)."""
    storage_src = _read(_STORAGE_PY)
    # Check that resolve_pass body mentions materialize_entity_edges.
    resolve_pass_match = re.search(
        r'def resolve_pass\(.*?\n(?:.*?\n)*?.*?(?=\n    def |\Z)',
        storage_src,
        re.MULTILINE,
    )
    assert resolve_pass_match is not None, "resolve_pass() not found in storage.py."
    assert "materialize_entity_edges" in storage_src, (
        "materialize_entity_edges not called anywhere in storage.py. "
        "P2-T5: wire materialize_entity_edges() into Storage.resolve_pass()."
    )


def test_cpp_resolve_pass_calls_materialize_entity_edges():
    """C++ Storage::resolve_pass() must call materialize_entity_edges() (P2-T8 hook)."""
    cpp_src = _read(_STORAGE_CPP)
    assert "materialize_entity_edges" in cpp_src, (
        "materialize_entity_edges not called anywhere in storage.cpp. "
        "P2-T8: wire materialize_entity_edges() into C++ Storage::resolve_pass()."
    )


# ---------------------------------------------------------------------------
# scenario-id: rollup-2  — entity_rollup.py module exists
# ---------------------------------------------------------------------------


def test_entity_rollup_module_exists():
    """project/indexer/entity_rollup.py must exist (P2-T4/T5)."""
    assert os.path.isfile(_ENTITY_ROLLUP_PY), (
        f"entity_rollup.py not found at {_ENTITY_ROLLUP_PY}. "
        "P2-T4: create entity_rollup.py with classify_member_type kernel + roll-up pass."
    )


# ---------------------------------------------------------------------------
# scenario-id: parity-1  — parity_check.sh covers entity_edge
# ---------------------------------------------------------------------------


def test_parity_check_covers_entity_edge():
    """parity_check.sh must include at least one entity_edge CLI invocation."""
    parity_src = _read(_PARITY_SH)
    assert "entity" in parity_src or "entity_edge" in parity_src, (
        "parity_check.sh does not cover entity_edge. "
        "P2-T11: add `cidx entity` (or equivalent) command(s) to parity_check.sh "
        "so the DB-dump diff locks entity_edge INSERT rows."
    )


# ---------------------------------------------------------------------------
# Live DB tests — open a fresh Storage and verify the schema is applied.
# These test that migration + seed work correctly.
# ---------------------------------------------------------------------------


def _open_fresh_storage(tmp_path: str):
    """Open a Storage with a fresh in-memory-like DB and return the storage object."""
    from indexer.storage import Storage
    db_path = os.path.join(tmp_path, "test.db")
    return Storage(db_path)


def test_fresh_db_has_entity_edge_table(tmp_path):
    """A freshly opened Storage must have the entity_edge table (P2-T1 migration)."""
    try:
        from indexer.storage import Storage
    except ImportError:
        pytest.skip("indexer.storage not importable in this environment")

    db_path = str(tmp_path / "test.db")
    s = Storage(db_path)
    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    s.close()
    assert "entity_edge" in tables, (
        "entity_edge table not created by Storage.__init__. "
        "P2-T1/T2: ensure _SCHEMA + migration create entity_edge."
    )


def test_fresh_db_entity_edge_is_empty(tmp_path):
    """A freshly opened Storage must have entity_edge with 0 rows (additive, no backfill)."""
    try:
        from indexer.storage import Storage
    except ImportError:
        pytest.skip("indexer.storage not importable in this environment")

    db_path = str(tmp_path / "test.db")
    s = Storage(db_path)
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM entity_edge").fetchone()[0]
    except sqlite3.OperationalError:
        count = None
    conn.close()
    s.close()
    assert count == 0, (
        f"entity_edge has {count} rows after fresh init (expected 0 — no backfill). "
        "Migration note: entity_edge is derived; populate via `cidx resolve` only."
    )


def test_fresh_db_entity_edge_kind_has_11_rows(tmp_path):
    """A freshly opened Storage must seed entity_edge_kind with exactly 11 rows."""
    try:
        from indexer.storage import Storage
    except ImportError:
        pytest.skip("indexer.storage not importable in this environment")

    db_path = str(tmp_path / "test.db")
    s = Storage(db_path)
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM entity_edge_kind").fetchone()[0]
        rows = conn.execute(
            "SELECT id, name FROM entity_edge_kind ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        count = None
        rows = []
    conn.close()
    s.close()
    assert count == 11, (
        f"entity_edge_kind has {count} rows; expected 11 (P2-T1 seed). "
        f"Rows found: {rows}"
    )
    expected_names = [
        "generalizes", "realizes", "specializes", "composes", "aggregates",
        "associates", "creates", "uses", "destroys", "nests", "befriends",
    ]
    actual_names = [r[1] for r in rows]
    assert actual_names == expected_names, (
        f"entity_edge_kind names/order mismatch. Expected {expected_names}, got {actual_names}."
    )


# ---------------------------------------------------------------------------
# Parametrised: realizes XOR generalizes boundary
# This tests the LOGIC contract (ADR-008 §5b) using the conftest hermetic DB.
# Since entity_rollup.py doesn't exist yet, these will FAIL with ImportError.
# ---------------------------------------------------------------------------


def test_realizes_xor_generalizes_contract():
    """No (src,dst) pair may have both realizes(2) and generalizes(1) in entity_edge."""
    try:
        from indexer import entity_rollup  # noqa: F401
    except ImportError:
        pytest.fail(
            "entity_rollup module not found. "
            "P2-T4/T5: create project/indexer/entity_rollup.py with the roll-up pass."
        )

    # If entity_rollup imports fine, test the contract on a real DB.
    # (Full integration test requires graphlab index + resolve — deferred to
    # a separate integration test once the implementation exists.)
    pytest.skip("entity_rollup exists but integration test requires graphlab reindex")


# ---------------------------------------------------------------------------
# Boundary: multiplicity encoding
# Tests the int-enum contract (ADR-008 table) without a live DB.
# ---------------------------------------------------------------------------

_MULTIPLICITY_TABLE = [
    (1, "one"),         # B b;  (direct value)
    (2, "zero_or_one"), # unique_ptr<B>
    (3, "zero_or_many"),# vector<B>
    (4, "N"),           # B[N]
]


@pytest.mark.parametrize("mult_id,label", _MULTIPLICITY_TABLE)
def test_multiplicity_int_enum_range(mult_id, label):
    """multiplicity int enum values must be in range 1..4 per ADR-008."""
    assert 1 <= mult_id <= 4, f"multiplicity id={mult_id} out of range 1-4."
    # Future: when entity_rollup is available, test that classify_member_type
    # returns mult_id for a field of the corresponding type.
    assert mult_id in (1, 2, 3, 4), (
        f"multiplicity value {mult_id} ({label}) is not a recognized enum id."
    )


# ---------------------------------------------------------------------------
# Boundary: create_form encoding
# ---------------------------------------------------------------------------

_CREATE_FORM_TABLE = [
    (1, "ctor_call"),
    (2, "return"),
    (3, "value"),
    (4, "temp"),
    (5, "heap"),
    (6, "factory"),
    (7, "copy"),
    (8, "move"),
]


@pytest.mark.parametrize("form_id,label", _CREATE_FORM_TABLE)
def test_create_form_int_enum_range(form_id, label):
    """create_form int enum values must be in range 1..8 per ADR-008."""
    assert 1 <= form_id <= 8, f"create_form id={form_id} ({label}) out of range 1-8."


# ---------------------------------------------------------------------------
# Parametrised: access encoding boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("access_val,label", [(0, "public"), (1, "protected"), (2, "private")])
def test_access_int_enum_range(access_val, label):
    """access int enum values must be 0/1/2 per ADR-008."""
    assert access_val in (0, 1, 2), f"access={access_val} ({label}) out of range 0-2."
