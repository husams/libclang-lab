#include "util/subprocess.hpp"

#include <cerrno>
#include <chrono>
#include <cstring>

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

namespace cidx {

namespace {

struct Sink {
  int fd;
  std::string *buf;
  bool open;
};

void close_fd(int fd) {
  if (fd >= 0) {
    ::close(fd);
  }
}

RunResult spawn_failure(const std::string &what, int err) {
  RunResult res;
  res.exit_code = 127;
  res.err = what + ": " + std::strerror(err);
  return res;
}

} // namespace

RunResult run(const std::vector<std::string> &argv, double timeout_sec) {
  RunResult res;
  if (argv.empty()) {
    res.exit_code = 127;
    res.err = "empty argv";
    return res;
  }

  int out_pipe[2] = {-1, -1};
  int err_pipe[2] = {-1, -1};
  if (::pipe(out_pipe) != 0) {
    return spawn_failure("pipe", errno);
  }
  if (::pipe(err_pipe) != 0) {
    const int e = errno;
    close_fd(out_pipe[0]);
    close_fd(out_pipe[1]);
    return spawn_failure("pipe", e);
  }
  const int devnull = ::open("/dev/null", O_RDONLY);
  if (devnull < 0) {
    const int e = errno;
    close_fd(out_pipe[0]);
    close_fd(out_pipe[1]);
    close_fd(err_pipe[0]);
    close_fd(err_pipe[1]);
    return spawn_failure("open /dev/null", e);
  }

  posix_spawn_file_actions_t fa;
  posix_spawn_file_actions_init(&fa);
  posix_spawn_file_actions_adddup2(&fa, devnull, 0);
  posix_spawn_file_actions_adddup2(&fa, out_pipe[1], 1);
  posix_spawn_file_actions_adddup2(&fa, err_pipe[1], 2);
  posix_spawn_file_actions_addclose(&fa, devnull);
  posix_spawn_file_actions_addclose(&fa, out_pipe[0]);
  posix_spawn_file_actions_addclose(&fa, out_pipe[1]);
  posix_spawn_file_actions_addclose(&fa, err_pipe[0]);
  posix_spawn_file_actions_addclose(&fa, err_pipe[1]);

  std::vector<char *> cargv;
  cargv.reserve(argv.size() + 1);
  for (const auto &a : argv) {
    cargv.push_back(const_cast<char *>(a.c_str()));
  }
  cargv.push_back(nullptr);

  pid_t pid = -1;
  const int rc =
      ::posix_spawnp(&pid, cargv[0], &fa, nullptr, cargv.data(), environ);
  posix_spawn_file_actions_destroy(&fa);
  close_fd(devnull);
  close_fd(out_pipe[1]);
  close_fd(err_pipe[1]);
  if (rc != 0) {
    close_fd(out_pipe[0]);
    close_fd(err_pipe[0]);
    return spawn_failure("spawn " + argv[0], rc);
  }

  using clock = std::chrono::steady_clock;
  const auto deadline =
      clock::now() + std::chrono::duration_cast<clock::duration>(
                         std::chrono::duration<double>(timeout_sec));

  Sink sinks[2] = {{out_pipe[0], &res.out, true},
                   {err_pipe[0], &res.err, true}};
  char buf[4096];
  while (sinks[0].open || sinks[1].open) {
    const auto remaining =
        std::chrono::duration_cast<std::chrono::milliseconds>(deadline -
                                                              clock::now())
            .count();
    if (remaining <= 0) {
      ::kill(pid, SIGKILL);
      res.timed_out = true;
      break;
    }
    struct pollfd pfds[2];
    int nfds = 0;
    for (const auto &s : sinks) {
      if (s.open) {
        pfds[nfds].fd = s.fd;
        pfds[nfds].events = POLLIN;
        pfds[nfds].revents = 0;
        ++nfds;
      }
    }
    const int pr =
        ::poll(pfds, static_cast<nfds_t>(nfds), static_cast<int>(remaining));
    if (pr < 0) {
      if (errno == EINTR) {
        continue;
      }
      break;
    }
    if (pr == 0) {
      continue; // poll timed out; the deadline check above fires next loop
    }
    int idx = 0;
    for (auto &s : sinks) {
      if (!s.open) {
        continue;
      }
      const struct pollfd &p = pfds[idx++];
      if ((p.revents & (POLLIN | POLLHUP | POLLERR)) == 0) {
        continue;
      }
      const ssize_t got = ::read(s.fd, buf, sizeof buf);
      if (got > 0) {
        s.buf->append(buf, static_cast<size_t>(got));
      } else if (got == 0 || (errno != EINTR && errno != EAGAIN)) {
        close_fd(s.fd);
        s.open = false;
      }
    }
  }
  for (auto &s : sinks) {
    if (s.open) {
      close_fd(s.fd);
      s.open = false;
    }
  }

  int status = 0;
  while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {
  }
  if (WIFEXITED(status)) {
    res.exit_code = WEXITSTATUS(status);
  } else if (WIFSIGNALED(status)) {
    res.exit_code = -WTERMSIG(status); // Python returncode parity
  }
  return res;
}

} // namespace cidx
