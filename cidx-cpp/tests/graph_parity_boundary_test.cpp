// graph_parity_boundary_test.cpp — Boundary / parametrised tests for M6
// graph command group (QA addition; category: mutation/boundary per role
// charter).
//
// Covers FIVE categories of QA additions (beyond the developer's G1-G13):
//
//   GB1  Help-text usage-line continuation indent — each of the 8 subcommands
//        must align continuation at len("usage: cidx graph <sub> ") spaces.
//        Catches the `GRAPH_SELECTOR_USAGE_ARGS` macro using a fixed 26-space
//        indent that is wrong for `refs` (needs 23) and `dispatch` (needs 27).
//        (Defect: args.cpp:214-219 — file-scoped macro)
//
//   GB2  open_graph stat-before-Storage ordering — when index.db does NOT
//        exist, the C++ tool must emit the NoIndexError message, NOT the
//        no-graph-edges message. Reproduces the bug where Storage construction
//        (which calls sqlite3_open_v2 with SQLITE_OPEN_CREATE) runs BEFORE the
//        stat() check, creating the file and causing the wrong branch.
//        (Defect: commands.cpp:2201 — h.storage allocated before stat())
//
//   GB3  Parametrised boundary — `--limit N` clamps results at exactly N;
//        `--limit 0` returns 0 results (SQLite LIMIT 0 = empty); `--depth 1`
//        limits walk BFS to exactly one hop. Exercises R12 and spec-required
//        edge-case behaviours not covered by the developer's G6.
//
//   GB4  Traversal::nodes() insertion-order stability — when multiple symbols
//        share the same (depth, name) key, the stable_sort must preserve BFS
//        discovery order (i.e., insertion order into nodes_by_id).
//        CURRENTLY FAILING: nodes_by_id uses std::unordered_map which does NOT
//        preserve insertion order, so the stable_sort input is unordered and
//        produces non-deterministic ties.
//        (QA_DEFECT QD-1; cpp_location: graph/records.hpp:209)
//
//   GB5  argparse prefix-abbreviation parity — Python argparse allows unambiguous
//        abbreviations of long options by default (allow_abbrev=True).  The C++
//        parser rejects them with exit 2.  For byte-identical parity every
//        unambiguous prefix (--na, --li, --dep, --tra, --us) must be accepted.
//        CURRENTLY FAILING: find_long() uses exact-match only (args.cpp:1204).
//        (QA_DEFECT QD-2; cpp_location: cidx-cpp/src/cli/args.cpp:1204)
//
// Category: mutation/boundary (role charter §mandatory-test-additions option 3)
// Framework: doctest (same as all other cidx-cpp tests)
// Label: "default" (hermetic — no libclang, no network, filesystem only for GB2)

#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <sys/stat.h>
#include <unistd.h>

#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "cli/args.hpp"
#include "cli/commands.hpp"
#include "graph/emit.hpp"
#include "graph/query.hpp"
#include "graph/records.hpp"
#include "storage/records.hpp"
#include "storage/storage.hpp"
#include "util/errors.hpp"

using cidx::Storage;
using cidx::Symbol;
using cidx::graph::GraphQuery;
using cidx::graph::Sym;
using cidx::graph::emit_edges;
using cidx::graph::emit_syms;
namespace cli = cidx::cli;

// ===========================================================================
// Helpers
// ===========================================================================

namespace {

// Count leading spaces in s.
std::size_t leading_spaces(const std::string &s) {
  std::size_t n = 0;
  while (n < s.size() && s[n] == ' ')
    ++n;
  return n;
}

// Return the second line of s (the first continuation line of a usage block).
std::string second_line(const std::string &s) {
  auto nl = s.find('\n');
  if (nl == std::string::npos)
    return "";
  auto nl2 = s.find('\n', nl + 1);
  return s.substr(nl + 1, nl2 == std::string::npos ? std::string::npos
                                                    : nl2 - nl - 1);
}

// Seed a minimal graph (A --calls--> B --calls--> C) in an in-memory Storage.
// Returns the Storage and ids.
struct MinGraph {
  Storage db;
  int64_t id_A = -1, id_B = -1, id_C = -1;

  MinGraph() : db(":memory:") {
    Symbol sA;
    sA.usr = "USR_A"; sA.spelling = "funcA"; sA.kind = "function";
    sA.is_definition = true; sA.resolved = true;
    Symbol sB;
    sB.usr = "USR_B"; sB.spelling = "funcB"; sB.kind = "function";
    sB.is_definition = true; sB.resolved = true;
    Symbol sC;
    sC.usr = "USR_C"; sC.spelling = "funcC"; sC.kind = "function";
    sC.is_definition = true; sC.resolved = true;

    id_A = db.add_symbol(sA);
    id_B = db.add_symbol(sB);
    id_C = db.add_symbol(sC);

    // A --calls(1)--> B (x3 edges so ecount=3)
    cidx::Edge eAB; eAB.src_id = id_A; eAB.dst_id = id_B; eAB.kind = 1; eAB.count = 1;
    db.add_edge(eAB); db.add_edge(eAB); db.add_edge(eAB);

    // B --calls(1)--> C (x1 edge)
    cidx::Edge eBC; eBC.src_id = id_B; eBC.dst_id = id_C; eBC.kind = 1; eBC.count = 1;
    db.add_edge(eBC);
  }
};

// Return a temp dir (caller must rm -rf).
std::string make_temp_dir() {
  char tmpl[] = "/tmp/cidx_gb_XXXXXX";
  char *d = ::mkdtemp(tmpl);
  REQUIRE(d != nullptr);
  return std::string(d);
}

void rm_rf(const std::string &path) {
  // Only used for /tmp dirs — simple recursive removal is fine.
  ::system(("rm -rf " + path).c_str()); // NOLINT
}

} // namespace

// ===========================================================================
// GB1: Help-text usage-line continuation indent
// ===========================================================================
// Python argparse aligns continuation at len("usage: cidx graph <sub> "):
//   "usage: cidx graph " = 19 chars, sub + " " => total = 19 + len(sub) + 1
//
// The GRAPH_SELECTOR_USAGE_ARGS macro (args.cpp:214-219) uses a fixed
// 26-space indent — correct for "callers"/"callees" (len 7 → 19+7+1=27? let
// me recount: "usage: cidx graph callers " = 26 chars) but wrong for:
//   "refs"     → "usage: cidx graph refs " = 23 chars (macro: 26 — WRONG)
//   "dispatch" → "usage: cidx graph dispatch " = 27 chars (macro: 26 — WRONG)
//
// These tests pin the exact expected leading-space count per subcommand and
// will FAIL until the macro is fixed with per-subcommand usage strings.

TEST_CASE("GB1: refs -h continuation indent = 23 spaces") {
  const auto pa = cli::parse_args({"graph", "refs", "-h"});
  REQUIRE(pa.help_text);
  // Second line of help text is the continuation of the usage line.
  const std::string line2 = second_line(*pa.help_text);
  // Must align at position 23 (len("usage: cidx graph refs ") = 23).
  const std::size_t sp = leading_spaces(line2);
  CHECK_MESSAGE(sp == 23,
      "refs continuation has " << sp << " spaces but expected 23; "
      "fix GRAPH_SELECTOR_USAGE_ARGS in args.cpp to use per-subcommand indent");
}

TEST_CASE("GB1: dispatch -h continuation indent = 27 spaces") {
  const auto pa = cli::parse_args({"graph", "dispatch", "-h"});
  REQUIRE(pa.help_text);
  const std::string line2 = second_line(*pa.help_text);
  // Must align at position 27 (len("usage: cidx graph dispatch ") = 27).
  const std::size_t sp = leading_spaces(line2);
  CHECK_MESSAGE(sp == 27,
      "dispatch continuation has " << sp << " spaces but expected 27; "
      "fix GRAPH_SELECTOR_USAGE_ARGS in args.cpp to use per-subcommand indent");
}

// Regression guard for the already-correct subcommands — must stay correct.
TEST_CASE("GB1: callers/callees -h continuation indent = 26 spaces") {
  for (const auto *sub : {"callers", "callees"}) {
    INFO("subcommand = " << sub);
    const auto pa = cli::parse_args({"graph", std::string(sub), "-h"});
    REQUIRE(pa.help_text);
    const std::string line2 = second_line(*pa.help_text);
    CHECK(leading_spaces(line2) == 26);
  }
}

TEST_CASE("GB1: neighbors/hierarchy -h continuation indent = 28 spaces") {
  for (const auto *sub : {"neighbors", "hierarchy"}) {
    INFO("subcommand = " << sub);
    const auto pa = cli::parse_args({"graph", std::string(sub), "-h"});
    REQUIRE(pa.help_text);
    const std::string line2 = second_line(*pa.help_text);
    CHECK(leading_spaces(line2) == 28);
  }
}

TEST_CASE("GB1: walk/path -h continuation indent = 23 spaces") {
  for (const auto *sub : {"walk", "path"}) {
    INFO("subcommand = " << sub);
    const auto pa = cli::parse_args({"graph", std::string(sub), "-h"});
    REQUIRE(pa.help_text);
    const std::string line2 = second_line(*pa.help_text);
    CHECK(leading_spaces(line2) == 23);
  }
}

// ===========================================================================
// GB2: open_graph must emit NoIndexError when index.db does not exist
// ===========================================================================
// Bug: open_graph (commands.cpp:2198-2226) calls
//   h.storage = std::make_unique<Storage>(ctx.index_path)
// BEFORE the ::stat() check. Storage::Storage calls sqlite3_open_v2 with
// SQLITE_OPEN_CREATE which creates the file even when it didn't exist. The
// stat() then succeeds (file exists, 0 bytes), the empty-DB branch emits the
// no-graph-edges error instead of the no-index error.
//
// Expected (Python oracle):
//   stderr: "error: no cidx index at '/tmp/.../index.db'. Build one with:\n..."
//   exit: 1
// Actual (bug):
//   stderr: "error: index '/tmp/.../index.db' has no graph edges..."
//   exit: 1

TEST_CASE("GB2: graph callers on missing index.db emits NoIndexError") {
  // A fresh temp dir with NO index.db.
  const std::string tmpdir = make_temp_dir();
  const std::string db_path = tmpdir + "/index.db";

  // Confirm file does not exist before the test.
  {
    struct stat st{};
    REQUIRE(::stat(db_path.c_str(), &st) != 0);
  }

  // Build a ParsedArgs for "graph callers --id 1".
  cli::ParsedArgs pa;
  pa.command = "graph";
  pa.what = "callers";
  pa.graph_id = 1;
  pa.graph_limit = 50;

  std::ostringstream out;
  std::ostringstream err;
  cli::Context ctx;
  ctx.cache_dir = tmpdir;
  ctx.index_path = db_path;
  ctx.logger = &cidx::Logger::root();
  ctx.out = &out;
  ctx.err = &err;

  const int rc = cli::run_command(pa, ctx);

  CHECK(rc == 1);
  // Must contain the NoIndexError text fragment, NOT the no-graph-edges text.
  const std::string errmsg = err.str();
  CHECK_MESSAGE(errmsg.find("no cidx index at") != std::string::npos,
      "Expected 'no cidx index at' in stderr but got:\n" << errmsg);
  CHECK_MESSAGE(errmsg.find("no graph edges") == std::string::npos,
      "Got wrong 'no graph edges' error when index.db is missing:\n" << errmsg);

  // Also assert that the file was NOT created as a side-effect of the command.
  // (If Storage is constructed before stat, it will exist at this point.)
  {
    struct stat st{};
    const bool created = (::stat(db_path.c_str(), &st) == 0);
    CHECK_MESSAGE(!created,
        "index.db was created as a side-effect of graph callers — "
        "Storage construction must happen AFTER stat() check");
  }

  rm_rf(tmpdir);
}

// ===========================================================================
// GB3: Parametrised boundary — --limit N / --depth N
// ===========================================================================

TEST_CASE("GB3: edges_in with limit=0 returns empty (LIMIT 0 = 0 rows, R12)") {
  MinGraph mg;
  GraphQuery g(mg.db, ":memory:");

  // A has one caller from edges_in("calls") = nobody calls A in this graph
  // but C has one caller (B). Use edges_in on id_C with limit=0.
  auto edges = g.edges_in(mg.id_C, std::vector<std::string>{"calls"}, 0);
  CHECK(edges.empty()); // LIMIT 0 must return 0 rows, not "all"
}

TEST_CASE("GB3: edges_out with limit=1 caps at exactly 1") {
  MinGraph mg;
  GraphQuery g(mg.db, ":memory:");

  // A --calls--> B (3 edges aggregated to 1 result in edge table with ecount=3)
  // B --calls--> C (1 edge)
  // edges_out A limit=2 should return [B] (only 1 callee from A's perspective)
  auto edges_full = g.edges_out(mg.id_A, std::vector<std::string>{"calls"}, 50);
  REQUIRE(edges_full.size() == 1);

  // Now try limit=0 and limit=1 on id_B which calls id_C
  auto e0 = g.edges_out(mg.id_B, std::vector<std::string>{"calls"}, 0);
  CHECK(e0.empty());

  auto e1 = g.edges_out(mg.id_B, std::vector<std::string>{"calls"}, 1);
  CHECK(e1.size() == 1);
  CHECK(e1[0].peer.id == mg.id_C);
}

TEST_CASE("GB3: walk --depth 1 only reaches direct neighbors") {
  MinGraph mg;
  GraphQuery g(mg.db, ":memory:");

  // A --calls--> B --calls--> C
  // walk from A, depth=1, limit=100: must reach B but NOT C
  std::optional<std::vector<std::string>> calls_kinds =
      std::vector<std::string>{"calls"};
  auto tr = g.walk(mg.id_A, calls_kinds, "out", 1, 100);

  bool found_B = false, found_C = false;
  for (const auto &[id, sym] : tr.nodes_by_id) {
    if (id == mg.id_B) found_B = true;
    if (id == mg.id_C) found_C = true;
  }
  CHECK(found_B);
  CHECK_MESSAGE(!found_C, "walk depth=1 must not reach C (2 hops from A)");
}

TEST_CASE("GB3: walk --depth 2 reaches both B and C from A") {
  MinGraph mg;
  GraphQuery g(mg.db, ":memory:");

  std::optional<std::vector<std::string>> calls_kinds =
      std::vector<std::string>{"calls"};
  auto tr = g.walk(mg.id_A, calls_kinds, "out", 2, 100);

  bool found_B = false, found_C = false;
  for (const auto &[id, sym] : tr.nodes_by_id) {
    if (id == mg.id_B) found_B = true;
    if (id == mg.id_C) found_C = true;
  }
  CHECK(found_B);
  CHECK(found_C);

  // Depth annotations: B at depth 1, C at depth 2.
  auto it_B = tr.depth_by_id.find(mg.id_B);
  auto it_C = tr.depth_by_id.find(mg.id_C);
  REQUIRE(it_B != tr.depth_by_id.end());
  REQUIRE(it_C != tr.depth_by_id.end());
  CHECK(it_B->second == 1);
  CHECK(it_C->second == 2);
}

TEST_CASE("GB3: find_symbols respects LIMIT (A5 -- R12)") {
  MinGraph mg;
  GraphQuery g(mg.db, ":memory:");

  // find with limit=1 must return exactly 1 result even when 3 match "func"
  auto all = g.find("func", std::nullopt, 50);
  REQUIRE(all.size() == 3);

  auto one = g.find("func", std::nullopt, 1);
  CHECK(one.size() == 1);

  auto zero = g.find("func", std::nullopt, 0);
  CHECK(zero.empty());
}

TEST_CASE("GB3: edges count fallback (R3) — ecount=0 raw=2 -> count=2") {
  // Reproduce R3: ecount=0 (count_resolved=false path in A6) while rawcount>0.
  // We can test this via edge_count(count_resolved=false) indirect path:
  // Seed one edge with count=2 then query via references() which uses
  // count_resolved = is_resolved().
  Storage db(":memory:");

  Symbol sA; sA.usr = "U_A"; sA.spelling = "A"; sA.kind = "function";
  sA.is_definition = true; sA.resolved = true;
  Symbol sB; sB.usr = "U_B"; sB.spelling = "B"; sB.kind = "function";
  sB.is_definition = true; sB.resolved = true;
  auto id_A = db.add_symbol(sA);
  auto id_B = db.add_symbol(sB);

  cidx::Edge e; e.src_id = id_A; e.dst_id = id_B; e.kind = 1; e.count = 2;
  db.add_edge(e);

  GraphQuery g(db, ":memory:");
  // is_resolved() checks graph_resolved_at in meta — fresh DB has no row → false
  // so count_expr = site count (A6 unresolved branch)
  // Since we have 1 edge row with no edge_sites, site count = 0 (ecount=0, rawcount=2)
  // R3: cnt = ecount; if (!cnt) cnt = rawcount ? rawcount : 1;
  // edge_site count = 0 -> ecount=0; rawcount=2 -> count = 2
  auto edges = g.edges_in(id_B, std::vector<std::string>{"calls"}, 50);
  REQUIRE(edges.size() == 1);
  // With no edge_sites, ecount=0 → fallback to rawcount=2.
  CHECK_MESSAGE(edges[0].count == 2,
      "R3 count fallback failed: expected 2 (rawcount), got " << edges[0].count);
}

// ===========================================================================
// GB4: Traversal::nodes() must preserve BFS insertion order for same-key ties
// QA_DEFECT QD-1 — EXPECTED TO FAIL until records.hpp:209 is fixed.
// ===========================================================================
//
// Python Traversal.nodes uses dict (insertion-ordered) + sorted() (stable).
// For symbols with the same (depth, name), the stable_sort preserves BFS
// discovery order — which is the order in which symbols were added to
// nodes_by_id during the BFS.
//
// C++ Traversal uses std::unordered_map<int64_t, Sym> nodes_by_id, which does
// NOT preserve insertion order.  Extracting values from the map before
// stable_sort gives a non-deterministic initial order, so same-key ties may
// resolve differently than Python, producing byte-different output.
//
// Fix required (records.hpp:208-231): add a std::vector<int64_t>
// insertion_order_ field, append each id there in walk(), and build the
// nodes() output vector in that order so stable_sort gets the right input.
//
// Reproducer: two symbols with identical name "scale" at depth 1 — A (id
// lower) must appear before B (id higher) in nodes() output, matching the
// BFS discovery order (A is discovered first because it has higher ecount).
TEST_CASE("GB4 [QD-1]: Traversal::nodes() preserves BFS insertion order for "
          "same-name ties") {
  // Seed: root --calls--> scaleA (ecount=3, lower id), --calls--> scaleB
  //       (ecount=1, higher id). A6 ORDER BY ecount DESC puts scaleA first.
  //       Both have the same name "scale" and depth 1.
  //       Python Traversal.nodes() sorts (depth=1, "scale") stable:
  //       scaleA < scaleB in discovery order → scaleA appears first.
  Storage db(":memory:");

  Symbol sR; sR.usr = "USR_ROOT"; sR.spelling = "root"; sR.kind = "function";
  sR.is_definition = true; sR.resolved = true;
  Symbol sA; sA.usr = "USR_A"; sA.spelling = "scale"; sA.kind = "function";
  sA.is_definition = true; sA.resolved = true;
  Symbol sB; sB.usr = "USR_B"; sB.spelling = "scale"; sB.kind = "function";
  sB.is_definition = true; sB.resolved = true;

  auto id_R = db.add_symbol(sR);
  auto id_A = db.add_symbol(sA);
  auto id_B = db.add_symbol(sB);

  // root --calls--> A with ecount=3 (inserted first -> A6 returns A before B)
  cidx::Edge eRA; eRA.src_id = id_R; eRA.dst_id = id_A; eRA.kind = 1; eRA.count = 3;
  db.add_edge(eRA);
  // root --calls--> B with ecount=1
  cidx::Edge eRB; eRB.src_id = id_R; eRB.dst_id = id_B; eRB.kind = 1; eRB.count = 1;
  db.add_edge(eRB);

  GraphQuery g(db, ":memory:");
  std::optional<std::vector<std::string>> calls = std::vector<std::string>{"calls"};
  auto tr = g.walk(id_R, calls, "out", 1, 100);

  // Exclude root; the two "scale" nodes remain.
  auto nodes = tr.nodes();
  // Remove the root node (depth 0) from the result.
  nodes.erase(std::remove_if(nodes.begin(), nodes.end(),
                             [id_R](const Sym &s) { return s.id == id_R; }),
              nodes.end());

  REQUIRE_MESSAGE(nodes.size() == 2,
      "Expected 2 'scale' nodes in walk result");
  // A (discovered first via A6 ecount DESC) must sort before B for same-key tie.
  CHECK_MESSAGE(nodes[0].id == id_A,
      "QD-1: BFS insertion-order tie-break failed: "
      "expected scaleA (id=" << id_A << ", ecount=3) first, "
      "got id=" << nodes[0].id << ". "
      "Fix: Traversal::nodes_by_id (records.hpp:209) must preserve insertion "
      "order (use std::vector<int64_t> insertion_order_ + populate in walk()).");
  CHECK_MESSAGE(nodes[1].id == id_B,
      "QD-1: expected scaleB (id=" << id_B << ") second, got id=" << nodes[1].id);
}

// ===========================================================================
// GB5: argparse prefix-abbreviation parity (QA_DEFECT QD-2)
// EXPECTED TO FAIL until args.cpp:1204 find_long() supports abbreviations.
// ===========================================================================
//
// Python argparse allows unambiguous prefix abbreviations by default
// (allow_abbrev=True). For byte-identical parity the C++ parser must accept
// the same abbreviations with the same semantics. Currently find_long() uses
// exact-match only (args.cpp comment "exact match only -- no abbreviation (D6)")
// which causes exit 2 on abbreviated args while Python succeeds (exit 0/1).
//
// Confirmed affected:
//   --na  → --name    (graph callers --na zzznotexist  → py exit=1, cpp exit=2)
//   --li  → --limit   (graph callees --li 1 --id 6     → py exit=0, cpp exit=2)
//   --dep → --depth   (graph walk --dep 1 --id N       → py exit=0, cpp exit=2)
//   --tra → --transitive (graph hierarchy --tra --id N → py exit=0, cpp exit=2)
//   --us  → --usr     (graph callers --us c:@bogus     → py exit=1, cpp exit=2)
//
// Fix required (args.cpp:1204): replace exact find_long() with prefix-match
// find_long() that returns the option iff exactly one long option starts with
// the given name (ambiguous = multiple matches → treat as unrecognized, same
// as Python argparse behaviour).
TEST_CASE("GB5 [QD-2]: --na is an unambiguous prefix of --name and must parse") {
  // "graph callers --na foo" — --na uniquely prefixes --name; no other graph
  // callers option starts with --na. Must parse with name="foo", not fail.
  const auto pa = cli::parse_args({"graph", "callers", "--na", "foo"});
  CHECK_MESSAGE(!pa.help_text.has_value(),
      "QD-2: --na treated as unrecognized (exit 2 in CLI) but Python accepts it "
      "as --name abbreviation. Fix find_long() in args.cpp:1204.");
  bool name_set = pa.name.has_value();
  CHECK_MESSAGE(name_set,
      "QD-2: pa.name not set from --na abbreviation. Fix find_long() in args.cpp:1204.");
  if (name_set) {
    CHECK_MESSAGE(*pa.name == "foo",
        "QD-2: expected pa.name='foo', got '" << *pa.name << "'");
  }
}

TEST_CASE("GB5 [QD-2]: --li is an unambiguous prefix of --limit and must parse") {
  // "graph callees --id 6 --li 1" — must set graph_limit=1.
  const auto pa = cli::parse_args({"graph", "callees", "--id", "6", "--li", "1"});
  CHECK_MESSAGE(!pa.help_text.has_value(),
      "QD-2: --li treated as unrecognized (exit 2 in CLI) but Python accepts it "
      "as --limit abbreviation. Fix find_long() in args.cpp:1204.");
  CHECK_MESSAGE(pa.graph_limit == 1,
      "QD-2: expected graph_limit=1 from --li 1 abbreviation, got " << pa.graph_limit);
}

TEST_CASE("GB5 [QD-2]: --dep is an unambiguous prefix of --depth and must parse") {
  // "graph walk --id 6 --dep 1" — must set graph_depth=1.
  const auto pa = cli::parse_args({"graph", "walk", "--id", "6", "--dep", "1"});
  CHECK_MESSAGE(!pa.help_text.has_value(),
      "QD-2: --dep treated as unrecognized (exit 2 in CLI) but Python accepts it "
      "as --depth abbreviation. Fix find_long() in args.cpp:1204.");
  CHECK_MESSAGE(pa.graph_depth == 1,
      "QD-2: expected graph_depth=1 from --dep 1 abbreviation, got " << pa.graph_depth);
}
