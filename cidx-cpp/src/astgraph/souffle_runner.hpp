// Native Souffle analyses over a cidx-astgraph SQLite artifact.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace cidx::astgraph {

struct CallFact {
  int64_t caller_node = 0;
  std::string caller_usr;
  std::string caller_name;
  int64_t callee_node = 0;
  std::string callee_usr;
  std::string callee_name;
  int64_t line = 0;
};

// True when cidx was built with the optional generated Souffle rule set.
bool native_souffle_available();

// Run the generated identity-preserving call-graph rule over `ast_db_path`.
// The returned facts are sorted for deterministic JSON output.
// Throws CidxError if the artifact is incompatible or native Souffle support
// was not enabled at configure time.
std::vector<CallFact> run_callgraph(const std::string &ast_db_path, int jobs);

} // namespace cidx::astgraph
