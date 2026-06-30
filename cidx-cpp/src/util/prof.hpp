// Tiny env-gated phase profiler (CIDX_PROFILE=1). Single-threaded indexer, so a
// plain accumulator is fine. "Save" = wall spent inside Storage write methods;
// the index_one driver measures parse + total and derives walk = total-parse-save.
#pragma once
#include <chrono>

namespace cidx {
namespace prof {
inline long long &save_ns() { static long long v = 0; return v; }
inline bool enabled() { static int e = -1; if (e < 0) { const char *s = ::getenv("CIDX_PROFILE"); e = (s && s[0] && s[0] != '0') ? 1 : 0; } return e == 1; }
struct SaveTimer {
  std::chrono::high_resolution_clock::time_point t;
  bool on;
  SaveTimer() : on(enabled()) { if (on) t = std::chrono::high_resolution_clock::now(); }
  ~SaveTimer() { if (on) save_ns() += std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::high_resolution_clock::now() - t).count(); }
};
} // namespace prof
} // namespace cidx
