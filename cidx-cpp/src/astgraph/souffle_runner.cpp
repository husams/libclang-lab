#include "astgraph/souffle_runner.hpp"

#include <algorithm>
#include <memory>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "astgraph/schema.hpp"
#include "storage/sqlite.hpp"
#include "util/errors.hpp"

#ifdef CIDX_HAVE_ASTGRAPH_SOUFFLE
#include <souffle/CompiledSouffle.h>
#endif

namespace cidx::astgraph {

#ifndef CIDX_HAVE_ASTGRAPH_SOUFFLE
#error "souffle_runner.cpp must be compiled only for the native Souffle target"
#endif
namespace {

souffle::Relation &require_relation(souffle::SouffleProgram &program,
                                    const char *name) {
  souffle::Relation *rel = program.getRelation(name);
  if (rel == nullptr)
    throw CidxError(std::string("generated Souffle rule has no relation: ") +
                    name);
  return *rel;
}

void load_nodes(SqliteDb &db, souffle::Relation &rel) {
  SqliteStmt stmt = db.prepare(
      "SELECT id, kind_id, symbol_id, spelling, line, is_definition "
      "FROM node ORDER BY id");
  while (stmt.step()) {
    souffle::tuple row(&rel);
    row << static_cast<souffle::RamSigned>(stmt.col_int64(0))
        << static_cast<souffle::RamSigned>(stmt.col_int64(1))
        << static_cast<souffle::RamSigned>(stmt.col_int64(2))
        << stmt.col_text(3)
        << static_cast<souffle::RamSigned>(stmt.col_int64(4))
        << static_cast<souffle::RamSigned>(stmt.col_int64(5));
    rel.insert(row);
  }
}

void load_edges(SqliteDb &db, souffle::Relation &rel) {
  SqliteStmt stmt =
      db.prepare("SELECT src_id, dst_id, rel_id, ord FROM edge ORDER BY "
                 "src_id, dst_id, rel_id, ord");
  while (stmt.step()) {
    souffle::tuple row(&rel);
    row << static_cast<souffle::RamSigned>(stmt.col_int64(0))
        << static_cast<souffle::RamSigned>(stmt.col_int64(1))
        << static_cast<souffle::RamSigned>(stmt.col_int64(2))
        << static_cast<souffle::RamSigned>(stmt.col_int64(3));
    rel.insert(row);
  }
}

void load_symbols(SqliteDb &db, souffle::Relation &rel) {
  SqliteStmt stmt = db.prepare("SELECT id, usr FROM symbol ORDER BY id");
  while (stmt.step()) {
    souffle::tuple row(&rel);
    row << static_cast<souffle::RamSigned>(stmt.col_int64(0))
        << stmt.col_text(1);
    rel.insert(row);
  }
}

void require_schema_v2(SqliteDb &db) {
  SqliteStmt stmt = db.prepare(
      "SELECT value FROM meta WHERE key = 'schema_version'");
  if (!stmt.step() || stmt.col_text(0) != std::to_string(kSchemaVersion)) {
    throw CidxError("unsupported cidx-astgraph schema; regenerate the AST DB");
  }
}

} // namespace

bool native_souffle_available() { return true; }

std::vector<CallFact> run_callgraph(const std::string &ast_db_path, int jobs) {
  if (jobs < 1)
    throw CidxError("--jobs must be at least 1");

  SqliteDb db(ast_db_path);
  require_schema_v2(db);

  std::unique_ptr<souffle::SouffleProgram> program(
      souffle::ProgramFactory::newInstance("ast_callgraph"));
  if (!program)
    throw CidxError("embedded Souffle rule ast_callgraph is not linked");

  load_nodes(db, require_relation(*program, "ast_node"));
  load_edges(db, require_relation(*program, "ast_edge"));
  load_symbols(db, require_relation(*program, "ast_symbol"));
  program->setNumThreads(static_cast<std::size_t>(jobs));
  program->run();

  souffle::Relation &result = require_relation(*program, "call");
  std::vector<CallFact> calls;
  calls.reserve(result.size());
  for (auto &row : result) {
    souffle::RamSigned caller_node = 0;
    souffle::RamSigned callee_node = 0;
    souffle::RamSigned line = 0;
    CallFact fact;
    row >> caller_node >> fact.caller_usr >> fact.caller_name >> callee_node >>
        fact.callee_usr >> fact.callee_name >> line;
    fact.caller_node = static_cast<int64_t>(caller_node);
    fact.callee_node = static_cast<int64_t>(callee_node);
    fact.line = static_cast<int64_t>(line);
    calls.push_back(std::move(fact));
  }
  std::sort(calls.begin(), calls.end(), [](const CallFact &a, const CallFact &b) {
    return std::tie(a.caller_usr, a.callee_usr, a.line, a.caller_node,
                    a.callee_node) <
           std::tie(b.caller_usr, b.callee_usr, b.line, b.caller_node,
                    b.callee_node);
  });
  return calls;
}

} // namespace cidx::astgraph
