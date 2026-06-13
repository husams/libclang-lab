// Environment lookup + the two falsy-spelling sets (analysis §1.3).
// The sets are intentionally DIFFERENT and must stay exact:
//   * CIDX_GNUC_VERSION disable set:           {"0", "off", "none", "false"}
//     (clang/util.py:250-254)
//   * INDEXER_IGNORE_SYSTEM_HEADERS false set: {"0", "false", "no", "off"}
//     (clang/ast.py:171-174)
// Both comparisons strip surrounding whitespace and lowercase first.
#pragma once

#include <optional>
#include <string>

namespace cidx {

// getenv as optional: unset -> nullopt; set-but-empty -> "".
std::optional<std::string> get_env(const char *name);

// True iff v spells "disabled" for CIDX_GNUC_VERSION. nullptr -> false.
bool env_flag_disabled_gnuc(const char *v);

// True iff v spells "false" for INDEXER_IGNORE_SYSTEM_HEADERS (whose default
// is true). nullptr -> false.
bool env_flag_false_headers(const char *v);

} // namespace cidx
