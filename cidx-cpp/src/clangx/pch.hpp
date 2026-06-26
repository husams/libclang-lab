// pch.hpp -- one shared system/C++ precompiled header, used to accelerate
// indexing. Byte-behaviour port of project/indexer/pch.py.
//
// A PCH is a serialized AST of an umbrella header that #includes the heavy
// system/STL headers every C++ TU pulls in. When present and compatible it is
// injected as `-include-pch <pch>` into every C++ parse (Parser::parse), so
// libclang deserializes that AST once instead of re-lexing <vector>/<string>/
// ... per TU. It is a pure speed optimization: the indexed symbols/edges are
// identical with or without it, so it does not change index output (and so
// keeps Python<->C++ indexed-data parity).
//
// Layout (next to the per-TU .ast cache, under $INDEXER_CACHE/files):
//   files/system.pch          serialized umbrella-header AST
//   files/system.pch.json     sidecar: flags / driver / libclang version / ...
//   files/system_umbrella.hpp  generated umbrella (kept for reproducibility)
//
// Compatibility is conservative: the baked flag-set is the INTERSECTION of
// every indexed C++ TU's PCH-relevant flags, and the PCH is only injected into
// a C++ TU parsed by the SAME libclang version and the SAME driver. On any
// incompatibility the parse falls back to a normal reparse (Parser::parse), so
// a stale/mismatched PCH can only slow indexing -- never break it.
//
// CIDX_NO_PCH (truthy) disables injection entirely.
#pragma once

#include <map>
#include <optional>
#include <ostream>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace cidx {

class Parser; // fwd: build_pch drives a real parse via Parser

namespace pch {

// --- paths -------------------------------------------------------------------

std::string pch_path();      // $INDEXER_CACHE/files/system.pch
std::string sidecar_path();  // .../system.pch.json
std::string umbrella_path(); // .../system_umbrella.hpp

// --- flag selection ----------------------------------------------------------

// The subset of options that can affect a system/STL header PCH: drops include
// paths, linker options, and -x / -include / -include-pch pairs; keeps -std /
// -D / -U / -f* / -m* / -W* / --driver-mode. Mirrors pch.py pch_relevant().
std::vector<std::string> pch_relevant(const std::vector<std::string> &options);

// Default umbrella headers (heavy STL), exposed for tests.
const std::vector<std::string> &default_headers();

// --- corpus header survey (cidx pch build --from-corpus) ---------------------

// One C++ TU's flags for the survey: its driver, resolved compile options
// (sanitized + <label>/$VAR decoded), and absolute source path.
struct TuFlags {
  std::optional<std::string> driver;
  std::vector<std::string> options;
  std::string path;
};

// Survey result: `freq` = header path -> #TUs that include it transitively
// (coverage / parse-cost signal); `directable` = headers that are a direct
// (depth-1) include in at least one TU (the safe-to-umbrella entry points).
struct HeaderSurvey {
  std::map<std::string, int> freq;
  std::set<std::string> directable;
};

// Extract (flag, dir) include-search pairs from options (both `-I dir` and
// joined `-Idir`; also -isystem/-iquote/-idirafter). Order preserved.
std::vector<std::pair<std::string, std::string>>
include_dirs(const std::vector<std::string> &options);

// Run a parse-free `<driver> <opts> -E -H` survey over `tus` (up to `jobs`
// concurrent) and aggregate the inclusion frequency + directable set.
HeaderSurvey survey_headers(const std::vector<TuFlags> &tus, int jobs);

// Headers shared (transitively) by >= `coverage` fraction (and >= `min_tus`) of
// the surveyed TUs AND directly included by at least one -- most-shared first.
std::vector<std::string> select_shared_headers(const HeaderSurvey &survey,
                                               int n_cpp, double coverage,
                                               int min_tus);

// --- consumption gate (called from Parser::parse for every TU) ---------------

// {"-include-pch", <path>} when a compatible system PCH should be injected into
// this parse, else {}. Compatible = C++ TU, CIDX_NO_PCH unset, PCH + sidecar
// present, sidecar libclang-version and driver match. Never throws.
std::vector<std::string> consume_args(bool cpp,
                                      const std::optional<std::string> &driver);

// --- build / status / clear (CLI ops) ----------------------------------------

// Compile an umbrella of `headers` with `flags` (+ driver) into the cached PCH
// and write the sidecar. Disables injection while building. Returns 0 on
// success. The Storage-derived common flags are computed by the caller.
// `quoted` writes `#include "h"` (absolute corpus headers) instead of
// `#include <h>`; `corpus`/`coverage` are recorded in the sidecar.
int build_pch(Parser &parser, const std::vector<std::string> &flags,
              const std::vector<std::string> &headers,
              const std::optional<std::string> &driver, int n_cpp_tus,
              std::ostream &out, std::ostream &err, bool quoted = false,
              bool corpus = false, double coverage = 0.0);

int status_pch(std::ostream &out);
int clear_pch(std::ostream &out);

} // namespace pch
} // namespace cidx
