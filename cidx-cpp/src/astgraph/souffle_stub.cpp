#include "astgraph/souffle_runner.hpp"

#include "util/errors.hpp"

namespace cidx::astgraph {

bool native_souffle_available() { return false; }

std::vector<CallFact> run_callgraph(const std::string &, int) {
  throw CidxError(
      "native Souffle support is unavailable; rebuild with "
      "-DCIDX_ASTGRAPH_SOUFFLE=ON and a Souffle development installation");
}

} // namespace cidx::astgraph
