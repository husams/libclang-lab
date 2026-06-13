// cidx entry point (design §6, D23): argv -> parse_args -> run_command.
// main() is the ONLY catch-site mapping exceptions to exit codes (R1 review):
//   UsageError      -> argparse-formatted message on stderr, exit 2
//   other CidxError -> "error: <msg>" on stderr, exit 1
//   std::exception  -> "error: <msg>" on stderr, exit 1  (bad_alloc etc.)
//   (...)           -> exit 1  (guard against non-std throws)
// Mirrors cli.py main(): usage errors fire BEFORE the cache dir is created;
// the cidx.log file sink is attached lazily (G27 — read-only subcommands
// never create an empty log file, but the cache DIR itself is created, as
// Python's _setup_logging does).
#include <sys/stat.h>

#include <cerrno>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

#include "cli/args.hpp"
#include "cli/commands.hpp"
#include "util/errors.hpp"
#include "util/logger.hpp"
#include "util/pathutil.hpp"

namespace {

// mkdir -p (os.makedirs(exist_ok=True) parity; no <filesystem> needed).
// Throws CidxError if any component cannot be created (EEXIST is silently
// ignored — directory already exists is fine, same as Python exist_ok=True).
void makedirs(const std::string &path) {
  std::string cur;
  for (std::size_t i = 0; i <= path.size(); ++i) {
    if (i == path.size() || path[i] == '/') {
      if (!cur.empty()) {
        if (::mkdir(cur.c_str(), 0777) != 0 && errno != EEXIST) {
          throw cidx::CidxError("cache directory: " + cur + ": " +
                                std::strerror(errno));
        }
      }
    }
    if (i < path.size()) {
      cur += path[i];
    }
  }
}

} // namespace

int main(int argc, char **argv) {
  const std::vector<std::string> args(argv + 1, argv + argc);
  try {
    cidx::cli::ParsedArgs parsed = cidx::cli::parse_args(args);
    if (parsed.help_text) { // argparse -h: help on stdout, exit 0
      std::cout << *parsed.help_text;
      return 0;
    }

    cidx::cli::Context ctx;
    ctx.cache_dir = cidx::cli::resolve_cache_dir();
    makedirs(ctx.cache_dir);
    ctx.index_path = cidx::pathutil::join(ctx.cache_dir, "index.db");
    cidx::Logger::root().set_file(
        cidx::pathutil::join(ctx.cache_dir, "cidx.log"));
    ctx.logger = &cidx::Logger::root();
    ctx.out = &std::cout;
    ctx.err = &std::cerr;
    return cidx::cli::run_command(parsed, ctx);
  } catch (const cidx::UsageError &e) {
    std::cerr << e.what();
    return e.exit_code();
  } catch (const cidx::CidxError &e) {
    std::cerr << "error: " << e.what() << "\n";
    return 1;
  } catch (const std::exception &e) {
    std::cerr << "error: " << e.what() << "\n";
    return 1;
  } catch (...) {
    return 1;
  }
}
