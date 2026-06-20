// Exception hierarchy (design D23). Exceptions stay inside C++ frames only:
// main() is the single catch-site mapping them to exit codes; libclang C
// callbacks are noexcept and stash errors in their context structs.
// Expected absence (lookups) uses std::optional, never exceptions.
#pragma once

#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "storage/records.hpp"

namespace cidx {

// Base for every cidx-raised error.
class CidxError : public std::runtime_error {
public:
  explicit CidxError(const std::string &msg) : std::runtime_error(msg) {}
};

// A translation unit could not be parsed (fatal diagnostics, bad libclang).
// Carries the diagnostics (severity >= warning) seen on the failed TU so the
// caller can still record WHY a file failed, even though no AST was indexed;
// empty when the TU never materialised.
class ClangParseError : public CidxError {
public:
  using CidxError::CidxError;
  ClangParseError(const std::string &msg, std::vector<Diagnostic> diags)
      : CidxError(msg), diagnostics_(std::move(diags)) {}
  const std::vector<Diagnostic> &diagnostics() const noexcept {
    return diagnostics_;
  }

private:
  std::vector<Diagnostic> diagnostics_;
};

// SQLite / schema / persistence failure.
class StorageError : public CidxError {
public:
  using CidxError::CidxError;
};

// CLI misuse; carries the process exit code (argparse parity: usage = 2).
class UsageError : public CidxError {
public:
  explicit UsageError(const std::string &msg, int exit_code = 2)
      : CidxError(msg), exit_code_(exit_code) {}
  int exit_code() const noexcept { return exit_code_; }

private:
  int exit_code_;
};

} // namespace cidx
