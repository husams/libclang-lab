// AST indexer — cursor walk, Symbol extraction, header indexing (design
// §5.8; analysis §5.6). Port of project/indexer/clang/ast.py.
//
// index_symbols() stores only cursors from one file of the TU — the caller
// passes the main-file path EXACTLY as it was passed to parse() (== the
// ParsedTu spelling, G24); cursors pulled in through #include are skipped.
// index_headers() then covers the headers: every file the TU includes
// (clang_getInclusions is transitive, so nested includes are reached too) is
// indexed as its own file row, skipping headers that are already indexed
// and — by default — system headers ($INDEXER_IGNORE_SYSTEM_HEADERS with a
// value in {0,false,no,off} indexes those as well).
//
// Small-footprint contract (design §7): cursors are streamed through
// clang_visitChildren callbacks and dropped immediately — only plain-data
// Symbol records are extracted; nothing libclang-owned outlives a visit.
#pragma once

#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <utility>

#include "clang-c/Index.h"

#include "clangx/parse.hpp"
#include "storage/records.hpp"
#include "storage/storage.hpp"
#include "util/logger.hpp"

namespace cidx {

// Set to 0/false/no/off to index system headers too (ast.py:22).
constexpr const char *kIgnoreSystemHeadersEnv = "INDEXER_IGNORE_SYSTEM_HEADERS";

// The {indexed, symbols, already, system, unowned} counters returned by
// index_headers (ast.py:203).
struct HeaderStats {
  int indexed = 0; // headers indexed by this call
  int symbols = 0; // symbols stored while indexing them
  int already = 0; // skipped: file row indexed with matching md5
  int system = 0;  // skipped: system header (default policy)
  int unowned = 0; // skipped: no registered component owns the path
};

class AstIndexer {
public:
  explicit AstIndexer(Storage &db, Logger &log = Logger::root())
      : db_(db), log_(log) {}
  AstIndexer(const AstIndexer &) = delete;
  AstIndexer &operator=(const AstIndexer &) = delete;

  // Store the symbols of `filename` (the TU's main file: pass the parse path
  // == tu.spelling, G24) under file_id; returns the stored count. One
  // db.transaction() wraps the whole file (ast.py:142) — must not be called
  // inside another open Transaction (S02: non-nestable).
  int index_symbols(const ParsedTu &tu, const std::string &filename,
                    int64_t file_id);

  // Index every header this TU includes, skipping ones already indexed
  // (ast.py:183-226). For each header not yet indexed (file row missing,
  // never indexed, or md5 changed) its symbols are read out of THIS TU's AST
  // — no separate parse — then the file row is marked indexed. The header's
  // file row records mtime + md5 but NULL compile_options/driver (G20).
  // ignore_system defaults to the $INDEXER_IGNORE_SYSTEM_HEADERS policy.
  HeaderStats
  index_headers(const ParsedTu &tu,
                const std::optional<bool> &ignore_system = std::nullopt);

private:
  // Store one file's symbols inside one transaction; (stored, skipped) —
  // ast.py:133-160. `filename` is matched against cursor expansion-file
  // names: the parse path for the main file, the include SPELLING for
  // headers (G23).
  std::pair<int, int> index_file(const ParsedTu &tu,
                                 const std::string &filename, int64_t file_id);

  // Pre-order walk via clang_visitChildren streaming only cursors located in
  // `filename` to fn (ast.py:62-74). The visitor is noexcept (D23; errors
  // are stashed and rethrown here). A child whose expansion-location file is
  // null or != filename gets CXChildVisit_Continue — the entire subtree is
  // pruned (G21). Function-like cursors (FunctionDecl, CXXMethod,
  // Constructor, Destructor, FunctionTemplate) are streamed but their bodies
  // are not walked; everything else recurses.
  void for_file_cursors(const ParsedTu &tu, const std::string &filename,
                        const std::function<void(CXCursor)> &fn);

  // Storage Symbol for a cursor, or nullopt when it is not indexable: kind
  // outside the frozen 17-entry map, or empty USR (ast.py:94-130).
  // Anonymous entities WITH a USR are indexed; qual_name skips
  // empty-spelling semantic-parent levels (G25). Declaration cursors record
  // their own site as decl_*, definition cursors leave decl_* null.
  std::optional<Symbol> to_symbol(CXCursor cursor, int64_t file_id);

  // Store policy (G15): an existing resolved row is skipped (returns false)
  // — but when this cursor carries a decl site and the stored row has none,
  // decl_file_id/line/col are patched via update_symbol. Otherwise upsert
  // (returns true).
  bool store(const Symbol &sym);

  Storage &db_;
  [[maybe_unused]] Logger &log_; // design §5.8 wires the sink; ast.py logs
                                 // nothing, so no record is ever written
};

} // namespace cidx
