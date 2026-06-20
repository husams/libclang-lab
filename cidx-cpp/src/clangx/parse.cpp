// Parser + diagnostic policy -- see parse.hpp. Line-level behavior is pinned
// to project/indexer/clang/util.py (cited per function).
#include "clangx/parse.hpp"

#include <algorithm>
#include <cctype>
#include <cstddef>

#include "clangx/libclang.hpp"
#include "util/env.hpp"
#include "util/errors.hpp"

namespace cidx {
namespace {

constexpr const char *kLogName = "cidx.clang";
constexpr const char *kStrictEnv = "CIDX_STRICT"; // util.py:361
// CIDX_MEM=1 logs one per-TU memory line (clang_getCXTUResourceUsage) after a
// successful parse. Pure observability: the libclang C API exposes no
// allocator hook, only this usage breakdown + the dispose lifecycle.
constexpr const char *kMemEnv = "CIDX_MEM";
// util.py:363-365 -- per-file cap on individual diagnostic lines written to
// the log, so one rotten TU can't flood cidx.log.
constexpr std::size_t kDiagLogCap = 25;

// One diagnostic, copied out of libclang so nothing CX-owned is retained.
struct DiagInfo {
  int severity = 0;
  std::string file; // "None" when locationless (Python str(None) parity)
  unsigned line = 0;
  std::string spelling;
};

std::string strip_lower(const std::string &s) {
  const char *ws = " \t\n\r\f\v";
  const std::size_t b = s.find_first_not_of(ws);
  if (b == std::string::npos) {
    return std::string();
  }
  const std::size_t e = s.find_last_not_of(ws);
  std::string out = s.substr(b, e - b + 1);
  std::transform(out.begin(), out.end(), out.begin(), [](unsigned char c) {
    return static_cast<char>(std::tolower(c));
  });
  return out;
}

// util.py:388-402 -- severity that aborts a parse: Fatal by default,
// Error when CIDX_STRICT is set truthy (G6).
int abort_level() {
  const std::string strict = strip_lower(get_env(kStrictEnv).value_or(""));
  if (strict.empty() || strict == "0" || strict == "off" || strict == "none" ||
      strict == "false") {
    return CXDiagnostic_Fatal;
  }
  return CXDiagnostic_Error;
}

// CIDX_MEM truthy (same falsy spellings as CIDX_STRICT) -> emit the per-TU
// memory report. Default OFF, so a clean parse still writes nothing (G27).
bool mem_reporting_enabled() {
  const std::string v = strip_lower(get_env(kMemEnv).value_or(""));
  return !(v.empty() || v == "0" || v == "off" || v == "none" ||
           v == "false");
}

// One INFO line per TU: total bytes (+ KiB) followed by every non-zero
// category from clang_getCXTUResourceUsage. All kinds 1..14 are
// MEMORY_IN_BYTES, so `amount` is bytes. The CX-owned buffer is disposed
// before returning -- nothing libclang-owned is retained.
void report_resource_usage(Logger &log, LibClang &lib, CXTranslationUnit tu,
                           const std::string &path) {
  CXTUResourceUsage usage = lib.clang_getCXTUResourceUsage(tu);
  unsigned long total = 0;
  std::string breakdown;
  for (unsigned i = 0; i < usage.numEntries; ++i) {
    const CXTUResourceUsageEntry &e = usage.entries[i];
    total += e.amount;
    if (e.amount == 0) {
      continue;
    }
    const char *name = lib.clang_getTUResourceUsageName(e.kind);
    if (!breakdown.empty()) {
      breakdown += ", ";
    }
    breakdown += std::string(name != nullptr ? name : "?") + "=" +
                 std::to_string(e.amount);
  }
  lib.clang_disposeCXTUResourceUsage(usage);
  log.info(kLogName, path + ": TU memory total=" + std::to_string(total) +
                         " bytes (" + std::to_string(total / 1024) +
                         " KiB); " + breakdown);
}

// util.py:380-385 with level=Error -- every diagnostic at severity >= ERROR,
// in TU order. Fatals are a subset (severity >= Fatal).
std::vector<DiagInfo> error_diagnostics(LibClang &lib, CXTranslationUnit tu) {
  std::vector<DiagInfo> out;
  const unsigned n = lib.clang_getNumDiagnostics(tu);
  for (unsigned i = 0; i < n; ++i) {
    CXDiagnostic d = lib.clang_getDiagnostic(tu, i);
    const int severity = static_cast<int>(lib.clang_getDiagnosticSeverity(d));
    if (severity >= CXDiagnostic_Error) {
      DiagInfo info;
      info.severity = severity;
      CXFile file = nullptr;
      unsigned line = 0;
      unsigned column = 0;
      unsigned offset = 0;
      lib.clang_getExpansionLocation(lib.clang_getDiagnosticLocation(d), &file,
                                     &line, &column, &offset);
      info.line = line;
      info.file = file != nullptr
                      ? CxString(lib, lib.clang_getFileName(file)).str()
                      : std::string("None");
      info.spelling = CxString(lib, lib.clang_getDiagnosticSpelling(d)).str();
      out.push_back(std::move(info));
    }
    lib.clang_disposeDiagnostic(d);
  }
  return out;
}

// util.py:368-377 -- each diagnostic at INFO (the per-file summary carries
// the WARNING/ERROR level, keeping the CLI warning counter one-per-file,
// G27), capped at 25 with a suppressed-count line.
void log_diagnostics(Logger &log, const std::string &filename,
                     const std::vector<DiagInfo> &diags) {
  const std::size_t shown = std::min(diags.size(), kDiagLogCap);
  for (std::size_t i = 0; i < shown; ++i) {
    const DiagInfo &d = diags[i];
    log.info(kLogName, filename + ": diag " + d.file + ":" +
                           std::to_string(d.line) + ": " + d.spelling);
  }
  if (diags.size() > kDiagLogCap) {
    log.info(kLogName, filename + ": ... " +
                           std::to_string(diags.size() - kDiagLogCap) +
                           " more diagnostic(s) suppressed");
  }
}

std::string join(const std::vector<std::string> &parts, const char *sep) {
  std::string out;
  for (std::size_t i = 0; i < parts.size(); ++i) {
    if (i != 0) {
      out += sep;
    }
    out += parts[i];
  }
  return out;
}

} // namespace

ParsedTu::~ParsedTu() {
  LibClang &lib = LibClang::instance();
  // disposeTranslationUnit first, then the Index that produced it (§5.7).
  // A1: facade methods are always callable; guards are on the handle values.
  if (tu != nullptr) {
    lib.clang_disposeTranslationUnit(tu);
  }
  if (index != nullptr) {
    lib.clang_disposeIndex(index);
  }
}

ParsedTu &ParsedTu::operator=(ParsedTu &&other) noexcept {
  if (this != &other) {
    ParsedTu tmp(std::move(other)); // dispose our current TU/Index via tmp
    std::swap(tu, tmp.tu);
    std::swap(index, tmp.index);
    std::swap(spelling, tmp.spelling);
  }
  return *this;
}

std::vector<std::string>
Parser::final_args(const std::string &path,
                   const std::vector<std::string> &args,
                   const std::optional<std::string> &driver) {
  // util.py:424-426 -- stored args + toolchain flags + -ferror-limit=0 (G5).
  std::vector<std::string> flags = args;
  const std::vector<std::string> toolchain =
      toolchain_.toolchain_flags(Toolchain::is_cpp(path, args), driver);
  flags.insert(flags.end(), toolchain.begin(), toolchain.end());
  flags.emplace_back("-ferror-limit=0");
  return flags;
}

// util.py collect_diagnostics -- plain-data diagnostics at/above WARNING, in
// TU order, for the index. A locationless diagnostic leaves file_path/line/col
// unset (NULL) so the stored row matches the Python binding byte-for-byte.
std::vector<Diagnostic> Parser::collect_diagnostics(const ParsedTu &tu) {
  LibClang &lib = LibClang::instance();
  std::vector<Diagnostic> out;
  if (tu.tu == nullptr) {
    return out;
  }
  const unsigned n = lib.clang_getNumDiagnostics(tu.tu);
  for (unsigned i = 0; i < n; ++i) {
    CXDiagnostic d = lib.clang_getDiagnostic(tu.tu, i);
    const int severity = static_cast<int>(lib.clang_getDiagnosticSeverity(d));
    if (severity >= CXDiagnostic_Warning) {
      Diagnostic info;
      info.severity = severity;
      info.spelling = CxString(lib, lib.clang_getDiagnosticSpelling(d)).str();
      CXFile file = nullptr;
      unsigned line = 0;
      unsigned column = 0;
      unsigned offset = 0;
      lib.clang_getExpansionLocation(lib.clang_getDiagnosticLocation(d), &file,
                                     &line, &column, &offset);
      if (file != nullptr) {
        info.file_path = CxString(lib, lib.clang_getFileName(file)).str();
        info.line = static_cast<int64_t>(line);
        info.col = static_cast<int64_t>(column);
      }
      out.push_back(std::move(info));
    }
    lib.clang_disposeDiagnostic(d);
  }
  return out;
}

ParsedTu Parser::parse(const std::string &abs_path,
                       const std::vector<std::string> &args,
                       const std::optional<std::string> &driver) {
  LibClang &lib = LibClang::instance();
  // Idempotent; MUST precede toolchain_flags so the gnuc cap consults the
  // real libclang major instead of the unloaded-0 fallback (S04 handoff).
  lib.load();

  const std::vector<std::string> flags = final_args(abs_path, args, driver);
  std::vector<const char *> argv;
  argv.reserve(flags.size());
  for (const std::string &f : flags) {
    argv.push_back(f.c_str());
  }

  // Fresh Index per parse (util.py:427, analysis §2.2); ParsedTu owns it
  // from here so every exit path below disposes deterministically.
  ParsedTu result(lib.clang_createIndex(0, 0), abs_path);

  CXTranslationUnit tu = nullptr;
  const CXErrorCode err = lib.clang_parseTranslationUnit2(
      result.index, abs_path.c_str(), argv.data(),
      static_cast<int>(argv.size()), nullptr, 0,
      /*options=*/0, // D19: no DETAILED_PREPROCESSING_RECORD, no skip-bodies
      &tu);
  result.tu = tu;
  if (err != CXError_Success || result.tu == nullptr) {
    // TranslationUnitLoadError parity (util.py:430-431).
    throw ClangParseError("cannot parse " + abs_path);
  }

  apply_diagnostic_policy(result.tu, abs_path, flags); // may throw; RAII frees
  if (mem_reporting_enabled()) {
    report_resource_usage(log_, lib, result.tu, abs_path);
  }
  return result;
}

void Parser::apply_diagnostic_policy(
    CXTranslationUnit tu, const std::string &path,
    const std::vector<std::string> &final_args) {
  LibClang &lib = LibClang::instance();
  const int level = abort_level();
  const std::vector<DiagInfo> errors = error_diagnostics(lib, tu);

  std::size_t fatal_count = 0;
  std::vector<std::string> summary_parts; // first 3 at/above the abort level
  for (const DiagInfo &d : errors) {
    if (d.severity >= level) {
      ++fatal_count;
      if (summary_parts.size() < 3) {
        summary_parts.push_back(d.file + ":" + std::to_string(d.line) + ": " +
                                d.spelling);
      }
    }
  }

  if (fatal_count > 0) {
    // util.py:440-448 -- the flag dump is debugging detail: log it, keep it
    // out of the exception message the CLI shows on screen (G28).
    const int major = lib.major();
    log_.error(kLogName,
               path + ": failed parse flags: " + join(final_args, " ") +
                   "; libclang: " +
                   (major != 0 ? std::to_string(major) : std::string("?")));
    log_diagnostics(log_, path, errors);
    throw ClangParseError(path + ": " + std::to_string(fatal_count) +
                          " fatal diagnostic(s): " + join(summary_parts, "; "));
  }

  // util.py:449-455 -- tolerated errors (default, non-strict mode): exactly
  // ONE WARNING summary per file + the capped INFO lines (G6, G27).
  if (level > CXDiagnostic_Error && !errors.empty()) {
    log_.warning(kLogName, path + ": " + std::to_string(errors.size()) +
                               " error diagnostic(s) ignored (" + kStrictEnv +
                               "=1 to abort)");
    log_diagnostics(log_, path, errors);
  }
}

} // namespace cidx
