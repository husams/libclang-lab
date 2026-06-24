// cache.hpp -- a Cache exposing MEMBER FUNCTION TEMPLATES over a type-erased store.
//
// Covers a CLASS holding a std::unordered_map<std::string, std::any>, exposing
// member function templates set<T>() / get<T>(). Each call from the function
// template in UseCache.hpp instantiates Cache::set<T> / Cache::get<T>.
//
// Cache is a Singleton: it derives from Singleton<Cache> (singleton.hpp), keeps
// its constructor private, and is reached through Cache::instance().
#ifndef GRAPHLAB_CACHE_HPP
#define GRAPHLAB_CACHE_HPP

#include <any>
#include <string>
#include <unordered_map>
#include <utility>

#include "singleton.hpp"

namespace app {

class Cache : public Singleton<Cache> {
    // Singleton<Cache>::instance() constructs the one Cache via this ctor.
    friend class Singleton<Cache>;
    Cache() = default;

    std::unordered_map<std::string, std::any> store_;
public:
    // set<T>(): store a typed value under a string key (member template).
    template <class T>
    void set(const std::string& key, T value) {
        store_[key] = std::any(std::move(value));
    }

    // get<T>(): recover the typed value, or T{} if absent (member template).
    template <class T>
    T get(const std::string& key) const {
        auto it = store_.find(key);
        return it == store_.end() ? T{} : std::any_cast<T>(it->second);
    }
};

} // namespace app

#endif
