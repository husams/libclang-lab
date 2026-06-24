// UseCache.cpp -- a function that uses the function template in UseCache.hpp.
#include "UseCache.hpp"

#include <string>

namespace app {

// exercise_cache() instantiates cache_roundtrip<T> for two distinct types,
// giving exercise_cache -> cache_roundtrip<int> / cache_roundtrip<std::string>
// -> Cache::set / Cache::get.
int exercise_cache() {
    Cache& cache = Cache::instance();                           // singleton accessor
    int n = cache_roundtrip<int>(cache, "answer", 42);          // -> cache_roundtrip<int>
    std::string s =
        cache_roundtrip<std::string>(cache, "greet", std::string("hello"));  // -> cache_roundtrip<std::string>
    return n + static_cast<int>(s.size());
}

} // namespace app
