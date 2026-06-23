// singleton_crtp.cpp -- CRTP base-class instantiation fixture (cidx entity_edge).
//
// `class Registry : public Singleton<Registry>` is the one template-
// instantiation site that is NOT a variable / member / call / using: the
// template appears as a BASE CLASS.  The extractor must emit, for the
// Singleton<Registry> specialization, an instantiates(5) Layer-0 edge to the
// primary template, so the entity roll-up materialises the chain:
//
//   Registry  --generalizes-->  Singleton<Registry>  --instantiates-->  Singleton
//
// generalizes (not implements) because the Singleton primary carries state
// (count_) and a concrete method, so it is not a pure Interface.  The base stays
// the specialization (its own design entity) instead of collapsing onto the
// primary -- the instantiates edge carries the instance -> primary link.
//
// Names are nested in graphlab::crtp to avoid colliding with graphlab::Cache
// (cache.hpp) under the shared unified compilation database.

namespace graphlab {
namespace crtp {

template <class Derived>
class Singleton {
 public:
  static Derived &instance() {
    static Derived d;
    return d;
  }

 protected:
  Singleton() = default;

 private:
  int count_ = 0;
};

class Registry : public Singleton<Registry> {
 public:
  void put(int key, int value);
  int get(int key) const;

 private:
  int store_[256] = {};
};

}  // namespace crtp
}  // namespace graphlab
