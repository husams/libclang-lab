// nested.cpp -- definitions for the nested-namespace symbols, with a couple of
// CROSS-NAMESPACE calls so the call graph spans namespace boundaries.
#include "nested.hpp"

namespace org {
namespace project {
namespace util {

int helper(int x) {
    return net::connect(x + 1);        // util -> net: cross-namespace call
}

int Config::value() const {
    return helper(7);                  // Config::value -> util::helper
}

} // namespace util
} // namespace project
} // namespace org

// Define the compact-form namespace's function.
namespace org::project::net {

int connect(int port) {
    return port * 10;
}

} // namespace org::project::net
