// Logger (design D7) — port of the Python logging contract (analysis §1.4):
//   * record format "YYYY-MM-DD HH:MM:SS,mmm LEVEL name: message"
//     (Python "%(asctime)s %(levelname)s %(name)s: %(message)s")
//   * lazy file creation: the log file is opened on the FIRST record only
//     (FileHandler delay=True parity — read-only subcommands never create it)
//   * warning counter counts file-sink records >= WARNING only (G27)
//   * stderr fallback when no file sink is configured (Python last-resort
//     handler parity: WARNING and above only)
// Logger names are plain strings passed per record: "cidx", "cidx.clang".
#pragma once

#include <cstdio>
#include <string>

namespace cidx {

enum class LogLevel : int {
  kInfo = 20, // Python logging.INFO
  kWarning = 30,
  kError = 40,
};

class Logger {
public:
  Logger() = default;
  ~Logger();
  Logger(const Logger &) = delete;
  Logger &operator=(const Logger &) = delete;

  // Process-wide instance used by the CLI ("cidx" hierarchy).
  static Logger &root();

  // Configure the file sink; the file is NOT created until the first record.
  void set_file(const std::string &path);
  const std::string &file_path() const noexcept { return file_path_; }

  void log(LogLevel level, const std::string &name, const std::string &msg);
  void info(const std::string &name, const std::string &msg) {
    log(LogLevel::kInfo, name, msg);
  }
  void warning(const std::string &name, const std::string &msg) {
    log(LogLevel::kWarning, name, msg);
  }
  void error(const std::string &name, const std::string &msg) {
    log(LogLevel::kError, name, msg);
  }

  // Records at >= WARNING written to the FILE sink (stderr fallback excluded).
  int warning_count() const noexcept { return warning_count_; }

private:
  std::string file_path_;
  std::FILE *file_ = nullptr;
  bool open_failed_ = false;
  int warning_count_ = 0;
};


} // namespace cidx
