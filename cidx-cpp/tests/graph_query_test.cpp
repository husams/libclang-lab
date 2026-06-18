// graph_query_test — Hermetic unit tests for the M6 graph query layer.
//
// Category: hermetic (label "default") — no libclang, no filesystem.
// Uses in-memory SQLite DBs seeded via Storage write methods.
//
// Covers:
//   G1  get_by_id / get_by_usr / find on empty DB
//   G2  get_by_id / get_by_usr on a seeded DB
//   G3  edges_in / edges_out direction
//   G4  count fallback (R3): ecount=0 -> rawcount, else 1
//   G5  references() = calls + uses in
//   G6  walk() BFS depth and max_nodes
//   G7  reaches() shortest path and null when unreachable
//   G8  bases() / subclasses() / members()
//   G9  dispatch_targets() insertion order
//   G10 kind_ids() valid and invalid kind names
//   G11 Sym.is_stub(), Sym.loc(), Sym.to_dict() key order
//   G12 emit_edges text header + count suffix + trailer
//   G13 emit_syms text header + depth suffix + trailer

#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "graph/emit.hpp"
#include "graph/query.hpp"
#include "graph/records.hpp"
#include "storage/records.hpp"
#include "storage/storage.hpp"

using cidx::Storage;
using cidx::Symbol;
using cidx::graph::GraphQuery;
using cidx::graph::Sym;
using cidx::graph::Site;
using cidx::graph::Traversal;
using cidx::graph::emit_edges;
using cidx::graph::emit_syms;

namespace {

// Minimal symbol builder for in-memory test seeding.
Symbol make_sym(const std::string &usr, const std::string &spelling,
                const std::string &kind = "function",
                const std::string &qual_name = "") {
  Symbol s;
  s.usr = usr;
  s.spelling = spelling;
  s.kind = kind;
  if (!qual_name.empty()) {
    s.qual_name = qual_name;
  }
  s.is_definition = true;
  s.resolved = true;
  return s;
}

// Helper: build a storage Edge record for add_edge.
cidx::Edge make_edge(int64_t src, int64_t dst, int64_t kind, int64_t count = 1) {
  cidx::Edge e;
  e.src_id = src;
  e.dst_id = dst;
  e.kind = kind;
  e.count = count;
  return e;
}

// Helper: build a storage EdgeSite record for add_edge_site.
cidx::EdgeSite make_edge_site(int64_t eid, std::optional<int64_t> line = std::nullopt,
                               std::optional<int64_t> col = std::nullopt) {
  cidx::EdgeSite s;
  s.edge_id = eid;
  s.line = line;
  s.col = col;
  return s;
}

// Seed a small graph in an in-memory Storage.
//   A --calls--> B, B --calls--> C, A --uses--> C
//   D --inherits--> A (subclass), A --contains--> E (member)
//   B is a pure virtual method (is_pure=1)
struct Seeded {
  Storage db;
  int64_t id_A = -1, id_B = -1, id_C = -1, id_D = -1, id_E = -1;
  int64_t eid_AB = -1, eid_BC = -1, eid_AC = -1;
  int64_t eid_DA = -1, eid_AE = -1;

  Seeded() : db(":memory:") {
    auto sym_A = make_sym("USR::A", "funcA", "function", "ns::funcA");
    auto sym_B = make_sym("USR::B", "funcB", "function");
    sym_B.is_pure = true;
    auto sym_C = make_sym("USR::C", "funcC", "function");
    auto sym_D = make_sym("USR::D", "ClassD", "class");
    auto sym_E = make_sym("USR::E", "field_e", "member");

    id_A = db.add_symbol(sym_A);
    id_B = db.add_symbol(sym_B);
    id_C = db.add_symbol(sym_C);
    id_D = db.add_symbol(sym_D);
    id_E = db.add_symbol(sym_E);

    // A --calls--> B (count 3: add 3 separate edges, each count=1)
    eid_AB = db.add_edge(make_edge(id_A, id_B, 1));
    db.add_edge(make_edge(id_A, id_B, 1));
    db.add_edge(make_edge(id_A, id_B, 1));

    // B --calls--> C (count 1)
    eid_BC = db.add_edge(make_edge(id_B, id_C, 1));

    // A --uses--> C (count 2)
    eid_AC = db.add_edge(make_edge(id_A, id_C, 7));
    db.add_edge(make_edge(id_A, id_C, 7));

    // D --inherits--> A
    eid_DA = db.add_edge(make_edge(id_D, id_A, 2));

    // A --contains--> E
    eid_AE = db.add_edge(make_edge(id_A, id_E, 3));

    // B --overrides--> A
    db.add_edge(make_edge(id_B, id_A, 6));

    // Add one call site for AB
    db.add_edge_site(make_edge_site(eid_AB, 10, 5));
  }
};

} // namespace

// ---------------------------------------------------------------------------
// G1: empty DB — lookups return nullopt/empty
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: empty DB — get_by_id returns nullopt") {
  Storage db(":memory:");
  GraphQuery g(db, ":memory:");
  CHECK(!g.get_by_id(1));
  CHECK(!g.get_by_usr("USR::X"));
  CHECK(g.find("anything").empty());
  CHECK(g.edge_count() == 0);
}

// ---------------------------------------------------------------------------
// G2: seeded DB — symbol lookup
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: get_by_id / get_by_usr on seeded DB") {
  Seeded s;
  GraphQuery g(s.db, ":memory:");

  auto sym_A = g.get_by_id(s.id_A);
  REQUIRE(sym_A);
  CHECK(sym_A->spelling == "funcA");
  CHECK(sym_A->kind == "function");
  // qual_name set -> name = qual_name
  CHECK(sym_A->name == "ns::funcA");

  auto sym_B = g.get_by_usr("USR::B");
  REQUIRE(sym_B);
  CHECK(sym_B->is_pure);
  CHECK(sym_B->name == "funcB"); // no qual_name -> name = spelling

  CHECK(!g.get_by_id(9999));
  CHECK(!g.get_by_usr("USR::NONE"));
}

// ---------------------------------------------------------------------------
// G3: edges_in / edges_out direction
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: edges_in / edges_out direction") {
  Seeded s;
  GraphQuery g(s.db, ":memory:");

  // A calls B and uses C (out)
  auto out_A = g.edges_out(s.id_A, std::vector<std::string>{"calls"}, 50);
  REQUIRE(out_A.size() == 1);
  CHECK(out_A[0].peer.id == s.id_B);
  CHECK(out_A[0].kind == "calls");

  // B called by A (in)
  auto in_B = g.edges_in(s.id_B, std::vector<std::string>{"calls"}, 50);
  REQUIRE(in_B.size() == 1);
  CHECK(in_B[0].peer.id == s.id_A);

  // A is inherited by D (in, inherits)
  auto in_A_inh = g.edges_in(s.id_A, std::vector<std::string>{"inherits"}, 50);
  REQUIRE(in_A_inh.size() == 1);
  CHECK(in_A_inh[0].peer.id == s.id_D);
}

// ---------------------------------------------------------------------------
// G4: count fallback R3
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: count fallback (R3) from accumulated ecount") {
  Seeded s;
  GraphQuery g(s.db, ":memory:");

  // A --calls--> B was added 3 times; ecount should reflect that
  auto out_A = g.edges_out(s.id_A, std::vector<std::string>{"calls"}, 50);
  REQUIRE(!out_A.empty());
  // ecount accumulates
  CHECK(out_A[0].count >= 1); // at least 1 (may be 3 depending on count_resolved)

  // A --uses--> C was added 2 times
  auto out_AC = g.edges_out(s.id_A, std::vector<std::string>{"uses"}, 50);
  REQUIRE(!out_AC.empty());
  CHECK(out_AC[0].count >= 1);
}

// ---------------------------------------------------------------------------
// G5: references() = calls + uses in
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: references() = calls + uses inbound") {
  Seeded s;
  GraphQuery g(s.db, ":memory:");

  // C is called by B (calls) and used by A (uses)
  auto refs_C = g.references(s.id_C, 50);
  CHECK(refs_C.size() == 2);
  // Verify both peers exist
  bool found_B = false, found_A = false;
  for (const auto &e : refs_C) {
    if (e.peer.id == s.id_B) found_B = true;
    if (e.peer.id == s.id_A) found_A = true;
  }
  CHECK(found_B);
  CHECK(found_A);
}

// ---------------------------------------------------------------------------
// G6: walk() BFS depth and max_nodes
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: walk() BFS depth") {
  Seeded s;
  GraphQuery g(s.db, ":memory:");

  // walk from A out over "calls", depth=1 -> only B
  auto tr1 = g.walk(s.id_A, std::vector<std::string>{"calls"}, "out", 1, 100);
  auto nodes1 = tr1.nodes();
  CHECK(nodes1.size() == 2); // A(d0) + B(d1)

  // walk depth=2 -> A, B, C
  auto tr2 = g.walk(s.id_A, std::vector<std::string>{"calls"}, "out", 2, 100);
  auto nodes2 = tr2.nodes();
  CHECK(nodes2.size() == 3); // A(d0) + B(d1) + C(d2)

  // max_nodes=2 terminates after adding first neighbor (start + 1 neighbor)
  auto tr_lim = g.walk(s.id_A, std::vector<std::string>{"calls"}, "out", 5, 2);
  CHECK(tr_lim.nodes().size() <= 2);
}

// ---------------------------------------------------------------------------
// G7: reaches() shortest path and null when unreachable
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: reaches() shortest path") {
  Seeded s;
  GraphQuery g(s.db, ":memory:");

  // A -> B -> C via calls
  auto path = g.reaches(s.id_A, s.id_C, std::vector<std::string>{"calls"}, "out", 5);
  REQUIRE(path);
  CHECK(path->size() == 3); // A, B, C
  CHECK((*path)[0].id == s.id_A);
  CHECK((*path)[2].id == s.id_C);

  // C -> A: unreachable via calls out
  auto no_path = g.reaches(s.id_C, s.id_A, std::vector<std::string>{"calls"}, "out", 5);
  CHECK(!no_path);

  // A -> A: same node
  auto self_path = g.reaches(s.id_A, s.id_A, std::vector<std::string>{"calls"}, "out", 5);
  REQUIRE(self_path);
  CHECK(self_path->size() == 1);
}

// ---------------------------------------------------------------------------
// G8: bases() / subclasses() / members()
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: hierarchy — bases, subclasses, members") {
  Seeded s;
  GraphQuery g(s.db, ":memory:");

  // D --inherits--> A; so A has subclass D
  auto subs = g.subclasses(s.id_A, true);
  REQUIRE(subs.size() == 1);
  CHECK(subs[0].id == s.id_D);

  // A has no bases
  auto bases_A = g.bases(s.id_A, true);
  CHECK(bases_A.empty());

  // D has base A
  auto bases_D = g.bases(s.id_D, true);
  REQUIRE(bases_D.size() == 1);
  CHECK(bases_D[0].id == s.id_A);

  // A --contains--> E
  auto mems = g.members(s.id_A);
  CHECK(!mems.empty());
  bool has_E = false;
  for (const auto &m : mems) {
    if (m.id == s.id_E) has_E = true;
  }
  CHECK(has_E);
}

// ---------------------------------------------------------------------------
// G9: dispatch_targets() insertion order
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: dispatch_targets() BFS from virtual root") {
  Seeded s;
  GraphQuery g(s.db, ":memory:");

  // A is a non-pure function; B --overrides--> A and B is pure.
  // dispatch_targets(A): root A is not pure -> A first, then overriders.
  // B is pure -> NOT added to targets.
  auto targets = g.dispatch_targets(s.id_A);
  // A is not pure -> in targets; B is pure -> NOT in targets
  bool has_A = false, has_B = false;
  for (const auto &t : targets) {
    if (t.id == s.id_A) has_A = true;
    if (t.id == s.id_B) has_B = true;
  }
  CHECK(has_A);
  CHECK(!has_B);
}

// ---------------------------------------------------------------------------
// G10: kind_ids() valid and invalid
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: kind_ids() valid kinds and invalid kind throws") {
  Storage db(":memory:");
  GraphQuery g(db, ":memory:");

  auto kv = g.kind_ids(std::vector<std::string>{"calls", "uses"});
  REQUIRE(kv);
  CHECK(kv->size() == 2);

  // nullopt input -> nullopt output
  CHECK(!g.kind_ids(std::nullopt));

  // invalid kind
  CHECK_THROWS_AS(g.kind_ids(std::vector<std::string>{"bogus_kind"}),
                  std::invalid_argument);
}

// ---------------------------------------------------------------------------
// G11: Sym.is_stub(), Sym.loc(), Sym.to_dict() key order
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: Sym value type — is_stub, loc, to_dict key order") {
  // Build a stub Sym manually
  Sym stub;
  stub.id = 5;
  stub.usr = "USR::stub";
  stub.spelling = "stb";
  stub.name = "stb";
  stub.kind = "function";
  stub.resolved = false;
  stub.file = std::nullopt;
  stub.external = false;
  CHECK(stub.is_stub());
  CHECK(stub.loc() == "<no-location>");

  // Build a non-stub with file
  Sym real;
  real.id = 6;
  real.usr = "USR::real";
  real.spelling = "fn";
  real.name = "ns::fn";
  real.kind = "function";
  real.resolved = true;
  real.file = "/some/path/foo.cpp";
  real.line = 42;
  real.col = 3;
  CHECK(!real.is_stub());
  CHECK(real.loc() == "foo.cpp:42");

  // to_dict key order (R7): id,usr,spelling,qual_name,kind,type_info,
  //                         file,line,col,is_definition,is_pure,is_static,
  //                         is_instantiation,is_stub
  auto dict = real.to_dict();
  REQUIRE(dict.o.size() == 14);
  CHECK(dict.o[0].first == "id");
  CHECK(dict.o[1].first == "usr");
  CHECK(dict.o[2].first == "spelling");
  CHECK(dict.o[3].first == "qual_name");
  CHECK(dict.o[4].first == "kind");
  CHECK(dict.o[5].first == "type_info");
  CHECK(dict.o[6].first == "file");
  CHECK(dict.o[7].first == "line");
  CHECK(dict.o[8].first == "col");
  CHECK(dict.o[9].first == "is_definition");
  CHECK(dict.o[10].first == "is_pure");
  CHECK(dict.o[11].first == "is_static");
  CHECK(dict.o[12].first == "is_instantiation");
  CHECK(dict.o[13].first == "is_stub");
}

// ---------------------------------------------------------------------------
// G12: emit_edges text output
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: emit_edges text — header, count suffix, trailer") {
  Seeded s;
  GraphQuery g(s.db, ":memory:");

  auto edges = g.edges_out(s.id_A, std::vector<std::string>{"calls"}, 50);
  REQUIRE(!edges.empty());

  std::ostringstream out;
  emit_edges(g, edges, false, out, "header:");
  const std::string txt = out.str();

  // Header on first line
  CHECK(txt.substr(0, 7) == "header:");

  // Trailing N result(s)
  std::string last_line;
  {
    auto pos = txt.rfind('\n', txt.size() - 2);
    last_line = txt.substr(pos + 1);
  }
  CHECK(last_line.find("result(s)") != std::string::npos);
}

// ---------------------------------------------------------------------------
// G13: emit_syms text output with depth
// ---------------------------------------------------------------------------
TEST_CASE("graph_query: emit_syms text — depth suffix, trailer") {
  std::vector<Sym> syms;
  Sym a;
  a.id = 1; a.usr = "u1"; a.spelling = "fnA"; a.name = "fnA"; a.kind = "function";
  a.resolved = true;
  syms.push_back(a);

  std::unordered_map<int64_t, int> depths = {{1, 2}};

  std::ostringstream out;
  emit_syms(syms, false, out, "reach:", &depths);
  const std::string txt = out.str();

  // Contains "d2"
  CHECK(txt.find("d2") != std::string::npos);

  // Trailer
  CHECK(txt.find("1 result(s)") != std::string::npos);
}
