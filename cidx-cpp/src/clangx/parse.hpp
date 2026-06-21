// Parser + diagnostic policy (design §5.7; analysis §5.2-§5.3). Port of the
// parse half of project/indexer/clang/util.py: parse(), _abort_level(),
// _log_diagnostics().
//
// Policy (G6): clang grades unrecoverable environment problems (header not
// found) FATAL -- those truncate the AST and the parse is rejected. Plain
// ERRORs are semantic disagreements (clang stricter than the gcc the code
// targets) with intact surrounding AST -- tolerated by default and indexed
// through. $CIDX_STRICT=1 restores abort-on-error.
//
// Logging contract (G27/G28): the per-file summary line carries the
// WARNING/ERROR level -- exactly one per file, so the CLI's warning counter
// stays one-per-file. Individual diagnostics log at INFO, capped at 25 per
// file. On an aborted parse the full flag dump + libclang major go to the
// log at ERROR, never the terminal; the exception message carries only
// "<file>: N fatal diagnostic(s): <first 3>".
#pragma once

#include <optional>
#include <string>
#include <vector>

#include "clang-c/Index.h"

#include "clangx/toolchain.hpp"
#include "storage/records.hpp"
#include "util/logger.hpp"

namespace cidx {

// RAII over one parse result: owns the CXTranslationUnit AND its CXIndex
// (fresh Index per parse, analysis §2.2). Movable, not copyable; the
// destructor disposes the TU first, then the Index -- one TU alive at a
// time, freed deterministically (design §7).
struct ParsedTu {
  CXTranslationUnit tu = nullptr;
  CXIndex index = nullptr;
  std::string spelling; // the path EXACTLY as passed to parse() (G24)

  ParsedTu() = default;
  ParsedTu(CXIndex idx, std::string path)
      : index(idx), spelling(std::move(path)) {}
  ~ParsedTu();

  ParsedTu(ParsedTu &&other) noexcept
      : tu(other.tu), index(other.index), spelling(std::move(other.spelling)) {
    other.tu = nullptr;
    other.index = nullptr;
  }
  ParsedTu &operator=(ParsedTu &&other) noexcept;

  ParsedTu(const ParsedTu &) = delete;
  ParsedTu &operator=(const ParsedTu &) = delete;
};

class Parser {
public:
  // Default sink is the process logger; tests pass their own (D7 precedent).
  // The Toolchain is the per-run memoized instance (D8).
  explicit Parser(Toolchain &toolchain, Logger &log = Logger::root())
      : toolchain_(toolchain), log_(log) {}
  Parser(const Parser &) = delete;
  Parser &operator=(const Parser &) = delete;

  // util.py:405-456. `args` are the (already stripped/sanitized) stored
  // compile options. options = 0 (D19: no DETAILED_PREPROCESSING_RECORD --
  // the `macro` kind stays unreachable); fresh CXIndex per parse.
  // Throws ClangParseError on a null TU / error CXErrorCode ("cannot parse
  // <file>") and on diagnostics at/above the abort level.
  ParsedTu parse(const std::string &abs_path,
                 const std::vector<std::string> &args,
                 const std::optional<std::string> &driver);

  // v15: plain-data diagnostics at/above WARNING from a parsed TU, in TU
  // order, for persistence in the index (file_id is filled in by the caller
  // via Storage::replace_diagnostics). Mirrors util.py collect_diagnostics:
  // a locationless diagnostic leaves file_path/line/col unset (NULL).
  static std::vector<Diagnostic> collect_diagnostics(const ParsedTu &tu);

  // The assembled final argv (util.py:424-426):
  //   stored_args + toolchain_flags(is_cpp, driver) + {"-ferror-limit=0"}
  // -ferror-limit=0 lifts clang's default 20-error cap: hitting the cap
  // emits a FATAL 'too many errors emitted, stopping now' that aborts an
  // otherwise indexable TU while naming none of the real errors (G5).
  // Public so tests can assert the assembly without a libclang.
  std::vector<std::string> final_args(const std::string &path,
                                      const std::vector<std::string> &args,
                                      const std::optional<std::string> &driver);

private:
  // One libclang parse with the fully-assembled `flags` + the diagnostic
  // policy / memory reporting. PCH injection + retry policy lives in parse();
  // this is the single-shot core (mirrors util.py _do_parse).
  ParsedTu run_parse(const std::string &abs_path,
                     const std::vector<std::string> &flags);

  // util.py:432-455 -- abort level Fatal (default) or Error (CIDX_STRICT=1,
  // G6); see the header comment for the fatal/tolerated log shapes.
  void apply_diagnostic_policy(CXTranslationUnit tu, const std::string &path,
                               const std::vector<std::string> &final_args);

  Toolchain &toolchain_;
  Logger &log_;
};

} // namespace cidx
