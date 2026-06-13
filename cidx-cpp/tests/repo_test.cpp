// repo_test — git_root walk-up + repo_name INI scan (design D10, port of
// indexer/utils/repo.py). Uses a synthetic .git/config created in a temp dir.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <cstdio>
#include <fstream>
#include <string>
#include <sys/stat.h>
#include <unistd.h>

#include "util/repo.hpp"

namespace {

std::string make_temp_dir() {
  char tmpl[] = "/tmp/cidx_repo_XXXXXX";
  char *d = ::mkdtemp(tmpl);
  REQUIRE(d != nullptr);
  return d;
}

void makedirs(const std::string &path) {
  std::string cur;
  for (std::size_t i = 0; i <= path.size(); ++i) {
    if (i == path.size() || path[i] == '/') {
      if (!cur.empty()) {
        ::mkdir(cur.c_str(), 0755);
      }
    }
    if (i < path.size()) {
      cur += path[i];
    }
  }
}

void write_file(const std::string &path, const std::string &content) {
  std::ofstream out(path);
  REQUIRE(out.good());
  out << content;
}

// Layout: <tmp>/repo/.git/config + <tmp>/repo/src/deep
std::string make_repo(const std::string &tmp, const std::string &config) {
  const std::string repo = tmp + "/repo";
  makedirs(repo + "/.git");
  makedirs(repo + "/src/deep");
  if (!config.empty()) {
    write_file(repo + "/.git/config", config);
  }
  return repo;
}

constexpr char kTypicalConfig[] =
    "[core]\n"
    "\trepositoryformatversion = 0\n"
    "\tfilemode = true\n"
    "[remote \"origin\"]\n"
    "\turl = https://github.com/confluentinc/librdkafka.git\n"
    "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
    "[branch \"master\"]\n"
    "\tremote = origin\n";

} // namespace

TEST_CASE("git_root walks up to the directory containing .git") {
  const std::string tmp = make_temp_dir();
  const std::string repo = make_repo(tmp, kTypicalConfig);

  CHECK(cidx::repo::git_root(repo) == repo);
  SUBCASE("from a nested dir") {
    CHECK(cidx::repo::git_root(repo + "/src/deep") == repo);
  }
  SUBCASE("no .git anywhere up the chain -> nullopt") {
    // /tmp/cidx_repo_*/no-repo has no .git and neither do /tmp or /.
    makedirs(tmp + "/no-repo");
    CHECK(cidx::repo::git_root(tmp + "/no-repo") == std::nullopt);
  }
}

TEST_CASE("repo_name: origin url basename, '.git' suffix stripped") {
  const std::string tmp = make_temp_dir();
  const std::string repo = make_repo(tmp, kTypicalConfig);
  CHECK(cidx::repo::repo_name(repo) == "librdkafka");
}

TEST_CASE("repo_name handles scp-style and slash-terminated urls") {
  const std::string tmp = make_temp_dir();
  SUBCASE("scp-style git@host:user/name.git") {
    const std::string repo = make_repo(
        tmp, "[remote \"origin\"]\n\turl = git@github.com:husam/cidx.git\n");
    CHECK(cidx::repo::repo_name(repo) == "cidx");
  }
  SUBCASE("trailing slash is stripped before the basename") {
    const std::string repo = make_repo(
        tmp, "[remote \"origin\"]\n\turl = https://host/group/proj/\n");
    CHECK(cidx::repo::repo_name(repo) == "proj");
  }
  SUBCASE("no .git suffix") {
    const std::string repo = make_repo(
        tmp, "[remote \"origin\"]\n\turl = https://host/group/tool\n");
    CHECK(cidx::repo::repo_name(repo) == "tool");
  }
}

TEST_CASE("repo_name falls back to basename(root)") {
  const std::string tmp = make_temp_dir();
  SUBCASE("no config file at all") {
    const std::string repo = make_repo(tmp, "");
    CHECK(cidx::repo::repo_name(repo) == "repo");
  }
  SUBCASE("config without an origin remote") {
    const std::string repo =
        make_repo(tmp, "[core]\n\tbare = false\n[remote \"upstream\"]\n"
                       "\turl = https://host/other.git\n");
    CHECK(cidx::repo::repo_name(repo) == "repo");
  }
  SUBCASE("origin section without a url key") {
    const std::string repo =
        make_repo(tmp, "[remote \"origin\"]\n\tfetch = +refs/*:refs/*\n");
    CHECK(cidx::repo::repo_name(repo) == "repo");
  }
}
