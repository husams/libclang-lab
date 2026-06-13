// Exception hierarchy (design D23). Exceptions stay inside C++ frames only:
// main() is the single catch-site mapping them to exit codes; libclang C
// callbacks are noexcept and stash errors in their context structs.
// Expected absence (lookups) uses std::optional, never exceptions.
#pragma once

#include <stdexcept>
#include <string>

namespace cidx {

// Base for every cidx-raised error.
class CidxError : public std::runtime_error {
public:
  explicit CidxError(const std::string &msg) : std::runtime_error(msg) {}
};

// A translation unit could not be parsed (fatal diagnostics, bad libclang).
class ClangParseError : public CidxError {
public:
  using CidxError::CidxError;
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
