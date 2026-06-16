// nested.hpp -- NESTED NAMESPACES (classic blocks + C++17 compact form).
//
// Symbols get fully-qualified names reflecting the nesting (org::project::util::
// helper, …) and the index records `contains` edges namespace -> child.
#ifndef GRAPHLAB_NESTED_HPP
#define GRAPHLAB_NESTED_HPP

namespace org {
namespace project {

// classic three-level nesting: org::project::util
namespace util {
    int helper(int x);                 // org::project::util::helper
    struct Config {
        int value() const;             // org::project::util::Config::value
    };
}

} // namespace project
} // namespace org

// C++17 compact nested-namespace definition: same depth, one line.
namespace org::project::net {
    int connect(int port);             // org::project::net::connect
}

#endif
