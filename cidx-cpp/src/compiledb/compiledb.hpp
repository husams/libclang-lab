// compile_commands.json loading + arg strip/sanitize/driver (design §5.5,
// D20). Port of project/indexer/compiledb.py; the drop sets are FROZEN (G10)
// — they are part of the stored compile_options contract.
//
// Loading goes through libclang's CXCompilationDatabase (via the LibClang
// shim, never own JSON parsing) so `command`-string shell-unquoting stays
// byte-identical to the Python path (D20).
#pragma once

#include <string>
#include <vector>

namespace cidx {

struct CompileCommand {
  std::string directory; // cmd.directory as reported by libclang
  std::string filename;  // cmd.filename, raw (may be relative)
  std::string driver;    // driver(): argv[0], abspath'd iff it has a '/'
  std::vector<std::string> args; // strip_for_libclang() output
};

class CompileDb {
public:
  // --db arg: the compile_commands.json path (trailing filename stripped) or
  // its directory; abspath'd. Throws CidxError on load failure. Requires /
  // triggers LibClang::instance().load().
  static std::vector<CompileCommand> load(const std::string &db_arg);

  // The directory handed to CXCompilationDatabase before abspath
  // (compiledb.py:17-18): trailing "compile_commands.json" stripped, an
  // empty remainder -> ".". Exposed for hermetic tests.
  static std::string db_dir_from_arg(const std::string &db_arg);

  // Raw driver invocation -> flags parse() wants (compiledb.py:70-98):
  // drop argv[0]; apply the drop sets; drop the source file (matched by the
  // command's filename OR its basename, G10); absolutize -I/-isystem/-iquote
  // against `directory` in spaced and glued forms (G12); keep the rest.
  static std::vector<std::string>
  strip_for_libclang(const std::vector<std::string> &argv,
                     const std::string &filename, const std::string &directory);

  // Re-apply ONLY the drop rules (no argv[0]/source drop, no path fixing) to
  // already-stored options — heals DBs imported by an older cidx whose drop
  // list was shorter (compiledb.py:48-67, G11).
  static std::vector<std::string>
  sanitize(const std::vector<std::string> &stored);

  // argv[0]; absolutized against `directory` iff it contains a path
  // separator, else kept bare for PATH resolution at parse time
  // (compiledb.py:106-115).
  static std::string driver(const std::vector<std::string> &argv,
                            const std::string &directory);
};

} // namespace cidx
