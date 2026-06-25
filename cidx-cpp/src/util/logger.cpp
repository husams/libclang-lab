#include "util/logger.hpp"

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <ctime>

namespace cidx {

namespace {

const char *level_name(LogLevel level) {
  switch (level) {
  case LogLevel::kInfo:
    return "INFO";
  case LogLevel::kWarning:
    return "WARNING";
  case LogLevel::kError:
    return "ERROR";
  }
  return "INFO";
}

// Python asctime default: local time, "%Y-%m-%d %H:%M:%S,mmm".
std::string timestamp_now() {
  using namespace std::chrono;
  const auto now = system_clock::now();
  const std::time_t secs = system_clock::to_time_t(now);
  const auto ms =
      duration_cast<milliseconds>(now.time_since_epoch()).count() % 1000;
  std::tm tmv{};
  localtime_r(&secs, &tmv);
  char buf[40];
  const size_t n = std::strftime(buf, sizeof buf, "%Y-%m-%d %H:%M:%S", &tmv);
  char out[48];
  std::snprintf(out, sizeof out, "%.*s,%03d", static_cast<int>(n), buf,
                static_cast<int>(ms));
  return out;
}

} // namespace

Logger::~Logger() {
  if (file_ != nullptr) {
    std::fclose(file_);
  }
}

Logger &Logger::root() {
  static Logger instance;
  return instance;
}

void Logger::set_file(const std::string &path) {
  file_path_ = path; // lazily opened by the first log() call (delay=True)
}

void Logger::log(LogLevel level, const std::string &name,
                 const std::string &msg) {
  if (static_cast<int>(level) < static_cast<int>(LogLevel::kInfo)) {
    return; // INFO floor (Python logger.setLevel(logging.INFO))
  }
  const std::string record = timestamp_now() + " " + level_name(level) + " " +
                             name + ": " + msg + "\n";

  if (!file_path_.empty() && !open_failed_) {
    if (file_ == nullptr) {
      file_ = std::fopen(file_path_.c_str(), "a");
      if (file_ == nullptr) {
        open_failed_ = true; // fall through to the stderr fallback
      }
    }
    if (file_ != nullptr) {
      std::fputs(record.c_str(), file_);
      std::fflush(file_);
      if (static_cast<int>(level) >= static_cast<int>(LogLevel::kWarning)) {
        ++warning_count_; // counts FILE-sink records only (G27)
      }
      return;
    }
  }
  // No file sink: Python last-resort handler parity — WARNING+ to stderr,
  // never counted.
  if (static_cast<int>(level) >= static_cast<int>(LogLevel::kWarning)) {
    std::fputs(record.c_str(), stderr);
  }
}

void progress(const std::string &msg) {
  const char *v = std::getenv("CIDX_PROGRESS"); // NOLINT(concurrency-mt-unsafe)
  if (v == nullptr || v[0] == '\0' || std::strcmp(v, "0") == 0) {
    return; // gated OFF by default
  }
  std::fputs(("[cidx] " + msg + "\n").c_str(), stderr);
  std::fflush(stderr);
}

} // namespace cidx
