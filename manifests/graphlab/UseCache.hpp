// UseCache.hpp -- a FUNCTION TEMPLATE that uses Cache's MEMBER TEMPLATES.
//
// cache_roundtrip<T>() stores a typed value via Cache::set<T> and reads it back
// via Cache::get<T>. Each instantiation produces calls into the member-template
// instantiations Cache::set<T> / Cache::get<T>.
#ifndef GRAPHLAB_USECACHE_HPP
#define GRAPHLAB_USECACHE_HPP

#include <string>

#include "cache.hpp"

namespace app {

// FUNCTION TEMPLATE: store `value` under `key`, then fetch and recover it.
template <class T>
T cache_roundtrip(Cache& cache, const std::string& key, T value) {
    cache.set(key, value);       // -> Cache::set<T>
    return cache.get<T>(key);    // -> Cache::get<T>
}

} // namespace app

#endif
