// singleton.hpp -- a Singleton CRTP base-class template.
//
// Singleton<T> gives any derived class T a single shared instance, reachable
// via the static method T::instance(), which returns a reference to type T.
// The instance is a function-local static: constructed once on first use and
// the same reference is handed back on every later call.
//
// A derived class inherits as `class T : public Singleton<T>` and befriends
// Singleton<T> so instance() can reach its (private) constructor. Copy/move are
// deleted on the base so the single instance cannot be duplicated.
#ifndef GRAPHLAB_SINGLETON_HPP
#define GRAPHLAB_SINGLETON_HPP

namespace app {

// Singleton<T>: CRTP base. instance() returns a reference to the one T.
template <class T>
class Singleton {
public:
    // instance(): the single shared T, created on first call (T& by reference).
    static T& instance() {
        static T inst;
        return inst;
    }

    Singleton(const Singleton&) = delete;
    Singleton& operator=(const Singleton&) = delete;

protected:
    Singleton() = default;
    ~Singleton() = default;
};

} // namespace app

#endif
