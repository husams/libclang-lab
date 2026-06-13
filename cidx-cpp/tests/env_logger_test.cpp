// env_logger_test — falsy-spelling sets (§1.3), Logger contract (D7/G27),
// and subprocess runner (D9; hosted here per design §3 — no separate exe).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <cstdio>
#include <fstream>
#include <regex>
#include <sstream>
#include <string>

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include "util/env.hpp"
#include "util/logger.hpp"
#include "util/subprocess.hpp"

namespace {

std::string make_temp_dir() {
  char tmpl[] = "/tmp/cidx_envlog_XXXXXX";
  char *d = ::mkdtemp(tmpl);
  REQUIRE(d != nullptr);
  return d;
}

bool file_exists(const std::string &path) {
  struct stat st{};
  return ::stat(path.c_str(), &st) == 0;
}

std::string read_file(const std::string &path) {
  std::ifstream in(path, std::ios::binary);
  std::ostringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

// Redirects fd 2 to a file for the lifetime of the object (stderr-fallback
// assertions).
class StderrCapture {
public:
  explicit StderrCapture(const std::string &path) : path_(path) {
    std::fflush(stderr);
    saved_ = ::dup(2);
    const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    ::dup2(fd, 2);
    ::close(fd);
  }
  std::string finish() {
    std::fflush(stderr);
    ::dup2(saved_, 2);
    ::close(saved_);
    return read_file(path_);
  }

private:
  std::string path_;
  int saved_ = -1;
};

} // namespace

// ---- env falsy sets (§1.3) -------------------------------------------------

TEST_CASE("gnuc falsy set is exactly {0, off, none, false}") {
  CHECK(cidx::env_flag_disabled_gnuc("0"));
  CHECK(cidx::env_flag_disabled_gnuc("off"));
  CHECK(cidx::env_flag_disabled_gnuc("none"));
  CHECK(cidx::env_flag_disabled_gnuc("false"));
  // strip + lowercase first (Python .strip().lower())
  CHECK(cidx::env_flag_disabled_gnuc(" OFF "));
  CHECK(cidx::env_flag_disabled_gnuc("False"));
  // NOT in the gnuc set:
  CHECK_FALSE(cidx::env_flag_disabled_gnuc("no")); // headers-set only!
  CHECK_FALSE(cidx::env_flag_disabled_gnuc(""));
  CHECK_FALSE(cidx::env_flag_disabled_gnuc(nullptr));
  CHECK_FALSE(cidx::env_flag_disabled_gnuc("1"));
  CHECK_FALSE(cidx::env_flag_disabled_gnuc("13.4"));
}

TEST_CASE("headers falsy set is exactly {0, false, no, off}") {
  CHECK(cidx::env_flag_false_headers("0"));
  CHECK(cidx::env_flag_false_headers("false"));
  CHECK(cidx::env_flag_false_headers("no"));
  CHECK(cidx::env_flag_false_headers("off"));
  CHECK(cidx::env_flag_false_headers(" No\t"));
  // NOT in the headers set:
  CHECK_FALSE(cidx::env_flag_false_headers("none")); // gnuc-set only!
  CHECK_FALSE(cidx::env_flag_false_headers(""));
  CHECK_FALSE(cidx::env_flag_false_headers(nullptr));
  CHECK_FALSE(cidx::env_flag_false_headers("true"));
}

TEST_CASE("get_env distinguishes unset from empty") {
  ::setenv("CIDX_TEST_ENV_VAR", "value", 1);
  CHECK(cidx::get_env("CIDX_TEST_ENV_VAR") == std::string("value"));
  ::setenv("CIDX_TEST_ENV_VAR", "", 1);
  CHECK(cidx::get_env("CIDX_TEST_ENV_VAR") == std::string(""));
  ::unsetenv("CIDX_TEST_ENV_VAR");
  CHECK_FALSE(cidx::get_env("CIDX_TEST_ENV_VAR").has_value());
}

// ---- Logger (D7, G27) ------------------------------------------------------

TEST_CASE("log file is NOT created before the first record (delay=True)") {
  const std::string dir = make_temp_dir();
  const std::string log = dir + "/cidx.log";
  cidx::Logger logger;
  logger.set_file(log);
  CHECK_FALSE(file_exists(log)); // configured but untouched
  logger.info("cidx", "first record");
  CHECK(file_exists(log));
}

TEST_CASE("record format: YYYY-MM-DD HH:MM:SS,mmm LEVEL name: message") {
  const std::string dir = make_temp_dir();
  const std::string log = dir + "/cidx.log";
  cidx::Logger logger;
  logger.set_file(log);
  logger.info("cidx", "hello record");
  logger.warning("cidx.clang", "tolerated diagnostics");
  const std::string content = read_file(log);
  const std::regex line1(
      R"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO cidx: hello record\n)"
      R"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} WARNING cidx\.clang: tolerated diagnostics\n)");
  CHECK_MESSAGE(std::regex_match(content, line1), "got: " << content);
}

TEST_CASE("warning counter counts only file-sink records >= WARNING") {
  const std::string dir = make_temp_dir();
  cidx::Logger logger;
  logger.set_file(dir + "/cidx.log");
  CHECK(logger.warning_count() == 0);
  logger.info("cidx", "info does not count");
  CHECK(logger.warning_count() == 0);
  logger.warning("cidx", "counts");
  CHECK(logger.warning_count() == 1);
  logger.error("cidx.clang", "also counts");
  CHECK(logger.warning_count() == 2);
  logger.info("cidx", "still not counted");
  CHECK(logger.warning_count() == 2);
}

TEST_CASE("stderr fallback when no file sink is configured") {
  const std::string dir = make_temp_dir();
  cidx::Logger logger; // no set_file
  StderrCapture cap(dir + "/captured_stderr.txt");
  logger.warning("cidx", "to stderr");
  logger.info("cidx", "info is below the last-resort threshold");
  const std::string err = cap.finish();
  CHECK(err.find("WARNING cidx: to stderr") != std::string::npos);
  CHECK(err.find("info is below") == std::string::npos);
  // stderr-fallback records never bump the FILE-sink counter (G27).
  CHECK(logger.warning_count() == 0);
}

// ---- subprocess (D9) -------------------------------------------------------

TEST_CASE("subprocess captures stdout and stderr separately") {
  const auto res =
      cidx::run({"sh", "-c", "echo out-line; echo err-line 1>&2"}, 10.0);
  CHECK(res.exit_code == 0);
  CHECK(res.out == "out-line\n");
  CHECK(res.err == "err-line\n");
  CHECK_FALSE(res.timed_out);
}

TEST_CASE("subprocess exit code is surfaced") {
  const auto res = cidx::run({"sh", "-c", "exit 3"}, 10.0);
  CHECK(res.exit_code == 3);
  CHECK_FALSE(res.timed_out);
}

TEST_CASE("subprocess stdin is empty (/dev/null)") {
  // cat must see immediate EOF and exit 0 with no output — proving stdin is
  // /dev/null, not the test runner's tty/pipe.
  const auto res = cidx::run({"cat"}, 10.0);
  CHECK(res.exit_code == 0);
  CHECK(res.out.empty());
  CHECK_FALSE(res.timed_out);
}

TEST_CASE("subprocess timeout kills the child and reports timed_out") {
  const auto res = cidx::run({"sleep", "30"}, 0.4);
  CHECK(res.timed_out);
  CHECK(res.exit_code == -SIGKILL); // Python returncode parity: -9
}

TEST_CASE("subprocess missing binary -> exit 127, no throw") {
  const auto res = cidx::run({"cidx-no-such-binary-xyz"}, 10.0);
  CHECK(res.exit_code == 127);
  CHECK_FALSE(res.timed_out);
}
