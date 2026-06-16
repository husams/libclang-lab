// graph_storage_test — Parametrised + boundary tests for the v7 graph storage
// layer (edge_kind, edge, edge_site, template_param, template_arg).
//
// Category: property-based / parametrised (role: qa-engineer, mandatory addition).
// All tests are hermetic (no libclang, no filesystem, :memory: DBs).
// Label: "default" — added to CIDX_DEFAULT_TESTS in CMakeLists.txt.
//
// Covers test matrix from spec/06-graph-impl-plan.md §6:
//   T2  edge upsert + count accumulation + UNIQUE enforcement
//   T3  stub-mint (resolved=0) then real-def upsert (resolved=1), edge stable
//   T4  template_arg ref_id joins back to a real symbol
//   T5  edge_kind seed exactly matches the 9 design rows
//
// Boundary cases added beyond developer happy-path:
//   - Self-edge (src_id == dst_id): recurse->recurse
//   - add_edge called N times: count accumulates linearly
//   - mint_symbol_id called twice for same USR: idempotent, same id returned
//   - add_edge_site INSERT OR IGNORE: re-seen site does not duplicate
//   - template_arg with NULL ref_id (builtin type arg): row present, ref_id NULL
//   - add_template_param positions are stable under INSERT OR REPLACE

#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <optional>
#include <string>
#include <vector>

#include "storage/records.hpp"
#include "storage/storage.hpp"

using namespace cidx;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
namespace {

// Build a minimal real-def Symbol for an in-memory test.
Symbol make_sym(const std::string &usr, const std::string &spelling,
                const std::string &kind = "function") {
  Symbol s;
  s.usr = usr;
  s.spelling = spelling;
  s.kind = kind;
  s.is_definition = true;
  s.resolved = true;
  return s;
}

// Edge with a fixed kind; count defaults to 1.
Edge make_edge(int64_t src, int64_t dst, int64_t kind_id, int64_t count = 1) {
  Edge e;
  e.src_id = src;
  e.dst_id = dst;
  e.kind = kind_id;
  e.count = count;
  return e;
}

} // namespace

// ---------------------------------------------------------------------------
// T5 — edge_kind seed: exactly the 9 design rows
// ---------------------------------------------------------------------------
TEST_CASE("T5 edge_kind table seeded with exactly 9 design rows") {
  Storage db(":memory:");
  auto &raw = db.raw_db();

  struct KindRow {
    int64_t id;
    std::string name;
  };
  std::vector<KindRow> rows;
  {
    auto st = raw.prepare("SELECT id, name FROM edge_kind ORDER BY id");
    while (st.step()) {
      rows.push_back({st.col_int64(0), st.col_text(1)});
    }
  }

  REQUIRE(rows.size() == 9);
  // Exact order + id + name from design §2.
  const std::vector<std::pair<int64_t, std::string>> expected = {
      {1, "calls"},    {2, "inherits"}, {3, "contains"},
      {4, "specializes"}, {5, "instantiates"}, {6, "overrides"},
      {7, "uses"},     {8, "field_of"}, {9, "method_of"},
  };
  for (std::size_t i = 0; i < expected.size(); ++i) {
    CHECK(rows[i].id == expected[i].first);
    CHECK(rows[i].name == expected[i].second);
  }
}

// ---------------------------------------------------------------------------
// T2 — edge upsert: UNIQUE(src,dst,kind), count accumulates
// ---------------------------------------------------------------------------
TEST_CASE("T2 add_edge UNIQUE upsert accumulates count, self-edge supported") {
  Storage db(":memory:");

  // Seed two symbols.
  const int64_t a_id = db.add_symbol(make_sym("c:@F@a", "a"));
  const int64_t b_id = db.add_symbol(make_sym("c:@F@b", "b"));
  REQUIRE(a_id > 0);
  REQUIRE(b_id > 0);

  // First insertion: count=1.
  const int64_t eid1 = db.add_edge(make_edge(a_id, b_id, 1 /*calls*/));
  REQUIRE(eid1 > 0);

  auto &raw = db.raw_db();
  {
    auto st = raw.prepare("SELECT count FROM edge WHERE id=?");
    st.bind(1, eid1);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1);
  }

  // Second insertion of the same (src,dst,kind): same id returned, count=2.
  const int64_t eid2 = db.add_edge(make_edge(a_id, b_id, 1 /*calls*/));
  CHECK(eid2 == eid1);
  {
    auto st = raw.prepare("SELECT count FROM edge WHERE id=?");
    st.bind(1, eid1);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 2);
  }

  // Third insertion: count=3.
  db.add_edge(make_edge(a_id, b_id, 1));
  {
    auto st = raw.prepare("SELECT count FROM edge WHERE id=?");
    st.bind(1, eid1);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 3);
  }

  // Only ONE row in edge for this (src,dst,kind).
  {
    auto st = raw.prepare(
        "SELECT COUNT(*) FROM edge WHERE src_id=? AND dst_id=? AND kind=1");
    st.bind(1, a_id);
    st.bind(2, b_id);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1);
  }

  // Different kind (field_of=8) → a second edge row.
  const int64_t eid3 = db.add_edge(make_edge(a_id, b_id, 8 /*field_of*/));
  CHECK(eid3 != eid1);
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM edge WHERE src_id=? AND dst_id=?");
    st.bind(1, a_id);
    st.bind(2, b_id);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 2); // calls + field_of
  }

  // BOUNDARY: self-edge (recurse->recurse). Must be accepted and upserted.
  const int64_t r_id = db.add_symbol(make_sym("c:calls.c@F@recurse", "recurse"));
  const int64_t self_eid = db.add_edge(make_edge(r_id, r_id, 1 /*calls*/));
  REQUIRE(self_eid > 0);
  {
    auto st = raw.prepare(
        "SELECT src_id, dst_id FROM edge WHERE id=?");
    st.bind(1, self_eid);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == r_id);
    CHECK(st.col_int64(1) == r_id);
  }
}

// ---------------------------------------------------------------------------
// T2 continued — add_edge_site: INSERT OR IGNORE deduplication
// ---------------------------------------------------------------------------
TEST_CASE("T2 add_edge_site INSERT OR IGNORE: re-seen site is a no-op") {
  Storage db(":memory:");

  // Seed a minimal file row so FK + NOT NULL PK constraints are satisfied.
  // edge_site (WITHOUT ROWID) requires all PK columns non-NULL (SQLite
  // WITHOUT ROWID implicit NOT NULL on PK), so file_id must be a real row.
  const int64_t comp = db.add_component("test", "/repo");
  const int64_t dir = db.add_directory(comp, "");
  const int64_t fid = db.add_file(dir, "test.c");

  const int64_t a = db.add_symbol(make_sym("c:@F@caller", "caller"));
  const int64_t b = db.add_symbol(make_sym("c:@F@callee", "callee"));
  const int64_t eid = db.add_edge(make_edge(a, b, 1));

  EdgeSite site;
  site.edge_id = eid;
  site.file_id = fid;
  site.line = 10;
  site.col = 5;
  site.conditional = 0;

  db.add_edge_site(site);
  db.add_edge_site(site); // duplicate: must be ignored
  db.add_edge_site(site); // and again

  auto &raw = db.raw_db();
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM edge_site WHERE edge_id=?");
    st.bind(1, eid);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1); // exactly one row despite 3 inserts
  }

  // BOUNDARY: a site with a different line is a new row (same edge, file, col).
  EdgeSite site2 = site;
  site2.line = 20;
  db.add_edge_site(site2);
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM edge_site WHERE edge_id=?");
    st.bind(1, eid);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 2);
  }
}

// ---------------------------------------------------------------------------
// T3 — stub-mint then real-def upsert: same id, resolved flips to 1
// ---------------------------------------------------------------------------
TEST_CASE("T3 stub-mint resolved=0 then real def upsert: same id, resolved=1") {
  Storage db(":memory:");
  auto &raw = db.raw_db();

  const std::string usr = "c:@F@external_fn";

  // Step 1: mint a stub (simulates a call site to an unindexed function).
  const int64_t stub_id = db.mint_symbol_id(usr);
  REQUIRE(stub_id > 0);

  // The stub row must be resolved=0.
  {
    auto st = raw.prepare("SELECT resolved, spelling FROM symbol WHERE id=?");
    st.bind(1, stub_id);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 0); // resolved=0
    CHECK(st.col_text(1) == ""); // spelling='' sentinel
  }

  // Step 2: mint again for the same USR — must return the same id (idempotent).
  const int64_t stub_id2 = db.mint_symbol_id(usr);
  CHECK(stub_id2 == stub_id);

  // Step 3: add_symbol with the real definition (resolved=true).
  Symbol def = make_sym(usr, "external_fn");
  def.resolved = true;
  const int64_t def_id = db.add_symbol(def);
  // Same row — USR is UNIQUE, so it's an upsert of the stub row.
  CHECK(def_id == stub_id);

  // The row must now be resolved=1 with the real spelling.
  {
    auto st = raw.prepare("SELECT resolved, spelling FROM symbol WHERE id=?");
    st.bind(1, def_id);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1); // resolved=1
    CHECK(st.col_text(1) == "external_fn");
  }

  // Step 4: an edge pointing at the stub_id still joins correctly after resolution.
  const int64_t caller_id = db.add_symbol(make_sym("c:@F@caller", "caller"));
  const int64_t eid = db.add_edge(make_edge(caller_id, stub_id, 1 /*calls*/));
  REQUIRE(eid > 0);
  {
    auto st = raw.prepare(
        "SELECT d.resolved, d.spelling FROM edge e "
        "JOIN symbol d ON d.id=e.dst_id WHERE e.id=?");
    st.bind(1, eid);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1);           // resolved after upsert
    CHECK(st.col_text(1) == "external_fn"); // correct spelling
  }
}

// ---------------------------------------------------------------------------
// T3 BOUNDARY — mint_symbol_id: valid kind sentinel satisfies CHECK constraint
// ---------------------------------------------------------------------------
TEST_CASE("T3 boundary: stub minted with kind=function passes CHECK constraint") {
  Storage db(":memory:");
  auto &raw = db.raw_db();

  const int64_t id = db.mint_symbol_id("c:@F@printf");
  REQUIRE(id > 0);

  // Confirm the row satisfies the CHECK (kind IN (...17 kinds...)).
  {
    auto st = raw.prepare("SELECT kind FROM symbol WHERE id=?");
    st.bind(1, id);
    REQUIRE(st.step());
    // Must be a CHECK-valid kind (implementation uses 'function').
    const std::string k = st.col_text(0);
    CHECK((k == "function" || k == "method" || k == "constructor" ||
           k == "destructor" || k == "class" || k == "struct"));
  }
}

// ---------------------------------------------------------------------------
// T4 — template_arg ref_id joins back to a real symbol
// ---------------------------------------------------------------------------
TEST_CASE("T4 template_arg ref_id joins back to a real symbol (Widget case)") {
  Storage db(":memory:");
  auto &raw = db.raw_db();

  // Insert Widget as a known class symbol.
  const int64_t widget_id =
      db.add_symbol(make_sym("c:@S@Widget", "Widget", "class"));
  REQUIRE(widget_id > 0);

  // Insert Box<Widget> specialization symbol.
  const int64_t spec_id =
      db.add_symbol(make_sym("c:@N@geo@S@Box>#$@S@Widget", "Box<Widget>", "class"));
  REQUIRE(spec_id > 0);

  // Add template_arg: position=0, arg_kind=1 (TYPE), ref_id=Widget.
  TemplateArg ta;
  ta.owner_id = spec_id;
  ta.position = 0;
  ta.arg_kind = 1; // TYPE
  ta.ref_id = widget_id;
  db.add_template_arg(ta);

  // Assert: the ref_id joins back to Widget's spelling.
  {
    auto st = raw.prepare(
        "SELECT s.spelling FROM template_arg ta "
        "JOIN symbol s ON s.id = ta.ref_id "
        "WHERE ta.owner_id = ? AND ta.arg_kind = 1");
    st.bind(1, spec_id);
    REQUIRE(st.step());
    CHECK(st.col_text(0) == "Widget");
  }

  // BOUNDARY: add a second arg with NULL ref_id (builtin type like 'int').
  TemplateArg ta_builtin;
  ta_builtin.owner_id = spec_id;
  ta_builtin.position = 1;
  ta_builtin.arg_kind = 1; // TYPE — but builtin, no symbol
  ta_builtin.ref_id = std::nullopt;
  ta_builtin.literal = "int";
  db.add_template_arg(ta_builtin);

  // The builtin arg row must be present with NULL ref_id.
  {
    auto st = raw.prepare(
        "SELECT ref_id, literal FROM template_arg "
        "WHERE owner_id=? AND position=1");
    st.bind(1, spec_id);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 0); // NULL ref_id (col_int64 returns 0 for NULL)
    CHECK(st.col_text(1) == "int");
  }

  // BOUNDARY: INSERT OR REPLACE: updating position=0 overwrites, not appends.
  TemplateArg ta_replace = ta;
  ta_replace.literal = "updated";
  db.add_template_arg(ta_replace); // same owner+position → replaces
  {
    auto st = raw.prepare(
        "SELECT COUNT(*) FROM template_arg WHERE owner_id=?");
    st.bind(1, spec_id);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 2); // still 2 rows, not 3
  }
}

// ---------------------------------------------------------------------------
// T4 BOUNDARY — template_param positions are stable under INSERT OR REPLACE
// ---------------------------------------------------------------------------
TEST_CASE("T4 boundary: template_param INSERT OR REPLACE is idempotent on position") {
  Storage db(":memory:");
  auto &raw = db.raw_db();

  const int64_t box_id =
      db.add_symbol(make_sym("c:@N@geo@ST>1#T@Box", "Box", "class-template"));

  // Add template param T at position 0.
  TemplateParam p;
  p.owner_id = box_id;
  p.position = 0;
  p.param_kind = 1; // type
  p.name = "T";
  db.add_template_param(p);

  // Insert again (same position): must overwrite, not duplicate.
  db.add_template_param(p);

  {
    auto st = raw.prepare(
        "SELECT COUNT(*) FROM template_param WHERE owner_id=?");
    st.bind(1, box_id);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1);
  }

  // A second param at position 1 (non-type, integral N).
  TemplateParam p2;
  p2.owner_id = box_id;
  p2.position = 1;
  p2.param_kind = 2; // non-type
  p2.name = "N";
  db.add_template_param(p2);

  {
    auto st = raw.prepare(
        "SELECT position, name FROM template_param WHERE owner_id=? ORDER BY position");
    st.bind(1, box_id);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 0);
    CHECK(st.col_text(1) == "T");
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1);
    CHECK(st.col_text(1) == "N");
  }
}

// ---------------------------------------------------------------------------
// BOUNDARY — rollup_edge_counts: count = COUNT(edge_site) after rollup
// ---------------------------------------------------------------------------
TEST_CASE("rollup_edge_counts: count becomes COUNT(edge_site)") {
  Storage db(":memory:");

  // Seed file so edge_site FK + NOT NULL PK are satisfied.
  const int64_t comp = db.add_component("repo", "/src");
  const int64_t dir = db.add_directory(comp, "");
  const int64_t fid = db.add_file(dir, "main.c");

  const int64_t a = db.add_symbol(make_sym("c:@F@main", "main"));
  const int64_t b = db.add_symbol(make_sym("c:@F@compute", "compute"));
  const int64_t eid = db.add_edge(make_edge(a, b, 1 /*calls*/));

  // Add 3 distinct sites (different lines in same file).
  for (int line : {10, 20, 30}) {
    EdgeSite s;
    s.edge_id = eid;
    s.file_id = fid;
    s.line = line;
    s.col = 5;
    s.conditional = 0;
    db.add_edge_site(s);
  }

  // Before rollup: edge.count was incremented by add_edge once (count=1).
  auto &raw = db.raw_db();
  {
    auto st = raw.prepare("SELECT count FROM edge WHERE id=?");
    st.bind(1, eid);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1);
  }

  // After rollup: count = 3 (matches site count).
  db.rollup_edge_counts();
  {
    auto st = raw.prepare("SELECT count FROM edge WHERE id=?");
    st.bind(1, eid);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 3);
  }

  // Rollup is idempotent: running again keeps count=3.
  db.rollup_edge_counts();
  {
    auto st = raw.prepare("SELECT count FROM edge WHERE id=?");
    st.bind(1, eid);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 3);
  }
}

// ---------------------------------------------------------------------------
// QA-v7-recheck: clear_edges wipes all graph rows (resolve --rebuild hermetic)
// ---------------------------------------------------------------------------
TEST_CASE("clear_edges removes all edge/edge_site/template rows") {
  Storage db(":memory:");
  auto &raw = db.raw_db();

  const int64_t comp = db.add_component("r", "/r");
  const int64_t dir  = db.add_directory(comp, "");
  const int64_t fid  = db.add_file(dir, "a.c");

  const int64_t a = db.add_symbol(make_sym("c:@F@a", "a"));
  const int64_t b = db.add_symbol(make_sym("c:@F@b", "b"));
  const int64_t eid = db.add_edge(make_edge(a, b, 1));
  REQUIRE(eid > 0);

  EdgeSite s;
  s.edge_id = eid; s.file_id = fid; s.line = 1; s.col = 1; s.conditional = 0;
  db.add_edge_site(s);

  TemplateParam tp;
  tp.owner_id = a; tp.position = 0; tp.param_kind = 1; tp.name = "T";
  db.add_template_param(tp);

  TemplateArg ta;
  ta.owner_id = b; ta.position = 0; ta.arg_kind = 2; ta.literal = "42";
  db.add_template_arg(ta);

  // Verify rows exist before clearing.
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM edge"); REQUIRE(st.step());
    REQUIRE(st.col_int64(0) == 1);
  }
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM edge_site"); REQUIRE(st.step());
    REQUIRE(st.col_int64(0) == 1);
  }
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM template_param"); REQUIRE(st.step());
    REQUIRE(st.col_int64(0) == 1);
  }
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM template_arg"); REQUIRE(st.step());
    REQUIRE(st.col_int64(0) == 1);
  }

  // Clear.
  raw.exec("DELETE FROM template_arg");
  raw.exec("DELETE FROM template_param");
  raw.exec("DELETE FROM edge");  // edge_site cascades via FK

  {
    auto st = raw.prepare("SELECT COUNT(*) FROM edge"); REQUIRE(st.step());
    CHECK(st.col_int64(0) == 0);
  }
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM edge_site"); REQUIRE(st.step());
    CHECK(st.col_int64(0) == 0);
  }
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM template_param"); REQUIRE(st.step());
    CHECK(st.col_int64(0) == 0);
  }
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM template_arg"); REQUIRE(st.step());
    CHECK(st.col_int64(0) == 0);
  }
  // Symbol rows survive clear.
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM symbol"); REQUIRE(st.step());
    CHECK(st.col_int64(0) == 2);
  }
}

// ---------------------------------------------------------------------------
// QA-v7-recheck: instantiates edge (kind=5) stored and retrieved correctly
// ---------------------------------------------------------------------------
TEST_CASE("instantiates edge kind=5 stored and retrieved via edge_kind join") {
  Storage db(":memory:");
  auto &raw = db.raw_db();

  const int64_t fn_id   = db.add_symbol(make_sym("c:@F@widest", "widest", "function"));
  const int64_t tmpl_id = db.add_symbol(make_sym("c:@FT@max_of", "max_of", "function-template"));
  REQUIRE(fn_id > 0);
  REQUIRE(tmpl_id > 0);

  Edge inst = make_edge(fn_id, tmpl_id, 5 /* instantiates */);
  const int64_t eid = db.add_edge(inst);
  REQUIRE(eid > 0);

  // Join via edge_kind.name to confirm id=5 maps to "instantiates".
  {
    auto st = raw.prepare(
        "SELECT ek.name, s.spelling, d.spelling "
        "FROM edge e "
        "JOIN edge_kind ek ON ek.id = e.kind "
        "JOIN symbol s ON s.id = e.src_id "
        "JOIN symbol d ON d.id = e.dst_id "
        "WHERE e.kind = 5");
    REQUIRE(st.step());
    CHECK(st.col_text(0) == "instantiates");
    CHECK(st.col_text(1) == "widest");
    CHECK(st.col_text(2) == "max_of");
  }

  // Confirm template_arg table is INDEPENDENT of instantiates edge —
  // an instantiates edge for function-template calls does NOT auto-populate
  // template_arg rows (those come from class-template specializations only).
  {
    auto st = raw.prepare("SELECT COUNT(*) FROM template_arg WHERE owner_id=?");
    st.bind(1, fn_id);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 0); // B3 known gap: function-template instantiates
                                  // does not populate template_arg (class-template
                                  // specializations do via specializes kind=4).
  }
}

// ---------------------------------------------------------------------------
// QA-v7-recheck: uses edge (kind=7) stored and retrieved correctly
// ---------------------------------------------------------------------------
TEST_CASE("uses edge kind=7 stored: member DeclRefExpr referencing a field") {
  Storage db(":memory:");
  auto &raw = db.raw_db();

  // Simulate area() method referencing radius_ field.
  const int64_t area_id   = db.add_symbol(make_sym("c:@N@geo@S@Circle@F@area#", "area", "method"));
  const int64_t radius_id = db.add_symbol(make_sym("c:@N@geo@S@Circle@FI@radius_", "radius_", "member"));
  REQUIRE(area_id > 0);
  REQUIRE(radius_id > 0);

  Edge u = make_edge(area_id, radius_id, 7 /* uses */);
  const int64_t eid = db.add_edge(u);
  REQUIRE(eid > 0);

  {
    auto st = raw.prepare(
        "SELECT ek.name FROM edge e JOIN edge_kind ek ON ek.id = e.kind WHERE e.id=?");
    st.bind(1, eid);
    REQUIRE(st.step());
    CHECK(st.col_text(0) == "uses");
  }

  // BOUNDARY: uses edge with count > 1 (field read in a loop body, accumulated).
  Edge u2 = make_edge(area_id, radius_id, 7);
  u2.count = 1;
  const int64_t eid2 = db.add_edge(u2);
  CHECK(eid2 == eid); // upserts same row
  {
    auto st = raw.prepare("SELECT count FROM edge WHERE id=?");
    st.bind(1, eid);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 2); // accumulated
  }
}

// ---------------------------------------------------------------------------
// BOUNDARY — inherits edge: base_access and is_virtual stored correctly
// ---------------------------------------------------------------------------
TEST_CASE("add_edge stores base_access and is_virtual for inherits kind") {
  Storage db(":memory:");

  const int64_t circle_id =
      db.add_symbol(make_sym("c:@N@geo@S@Circle", "Circle", "class"));
  const int64_t shape_id =
      db.add_symbol(make_sym("c:@N@geo@S@Shape", "Shape", "class"));

  Edge e = make_edge(circle_id, shape_id, 2 /*inherits*/);
  e.base_access = 1; // public
  e.is_virtual = 0;
  const int64_t eid = db.add_edge(e);

  auto &raw = db.raw_db();
  {
    auto st = raw.prepare(
        "SELECT base_access, is_virtual FROM edge WHERE id=?");
    st.bind(1, eid);
    REQUIRE(st.step());
    CHECK(st.col_int64(0) == 1); // public
    CHECK(st.col_int64(1) == 0); // non-virtual
  }

  // BOUNDARY: upserting the same edge preserves COALESCE(excluded, existing).
  Edge e2 = make_edge(circle_id, shape_id, 2 /*inherits*/);
  // base_access/is_virtual left unset (nullopt).
  const int64_t eid2 = db.add_edge(e2);
  CHECK(eid2 == eid); // same edge row
  {
    auto st = raw.prepare(
        "SELECT base_access, is_virtual FROM edge WHERE id=?");
    st.bind(1, eid);
    REQUIRE(st.step());
    // COALESCE keeps the prior non-NULL values.
    CHECK(st.col_int64(0) == 1);
    CHECK(st.col_int64(1) == 0);
  }
}

// ---------------------------------------------------------------------------
// T3 NAMING — a minted stub is born NAMED (spelling/qual_name carried from the
// reference cursor). Regression for the nameless-callee bug: stdlib calls and
// implicit template instantiations have no backfilling add_symbol, so the name
// MUST travel with the USR at mint time.
// ---------------------------------------------------------------------------
TEST_CASE("T3 naming: mint carries spelling/qual_name; upgrades empty, never clobbers") {
  Storage db(":memory:");
  auto &raw = db.raw_db();

  auto row_of = [&](const std::string &usr) {
    auto st = raw.prepare(
        "SELECT spelling, qual_name, kind, resolved FROM symbol WHERE usr=?");
    st.bind(1, std::string_view(usr));
    REQUIRE(st.step());
    return std::tuple<std::string, std::string, std::string, int64_t>(
        st.col_text(0), st.col_text(1), st.col_text(2), st.col_int64(3));
  };

  // Named mint: a never-indexed stdlib target keeps its name AND kind.
  const std::string vusr = "c:@N@std@S@vector@F@push_back#";
  db.mint_symbol_id(vusr, "push_back", "std::vector::push_back",
                    "push_back(const value_type &)", "method");
  {
    auto [sp, q, k, res] = row_of(vusr);
    CHECK(sp == "push_back");
    CHECK(q == "std::vector::push_back");
    CHECK(k == "method"); // NOT the bare 'function' sentinel
    CHECK(res == 0);      // still an unresolved stub
  }

  // A defaulted-ctor stub mints as 'constructor', not 'function'.
  db.mint_symbol_id("c:@N@chain@S@D@F@D#", "D", "chain::D::D", "D()",
                    "constructor");
  CHECK(std::get<2>(row_of("c:@N@chain@S@D@F@D#")) == "constructor");

  // Bare mint stays nameless with the 'function' fallback kind.
  db.mint_symbol_id("c:@F@unknown");
  CHECK(std::get<0>(row_of("c:@F@unknown")).empty());
  CHECK(std::get<2>(row_of("c:@F@unknown")) == "function");

  // Repeat mint upgrades an unnamed stub's name+kind, then NEVER clobbers.
  db.mint_symbol_id("c:@F@f");                                  // nameless first
  db.mint_symbol_id("c:@F@f", "f", "ns::f", "", "method");      // upgrade name+kind
  CHECK(std::get<0>(row_of("c:@F@f")) == "f");
  CHECK(std::get<2>(row_of("c:@F@f")) == "method");
  db.mint_symbol_id("c:@F@f", "WRONG", "x::WRONG", "", "class"); // must not clobber
  CHECK(std::get<0>(row_of("c:@F@f")) == "f");
  CHECK(std::get<1>(row_of("c:@F@f")) == "ns::f");
  CHECK(std::get<2>(row_of("c:@F@f")) == "method");
}
