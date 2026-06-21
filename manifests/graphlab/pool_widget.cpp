// pool_widget.cpp -- PR1 Layer-0 extraction fixture (cidx entity_edge).
//
// Exercises the factory-construct (kind 15) form: make_unique<Widget>.
// Kept in a separate TU so <memory> is only included here.

#include "pool_widget.hpp"
#include <memory>

namespace graphlab {

// factory-construct (kind 15): std::make_unique<Widget>(x).
std::unique_ptr<Widget> pool_make_unique(int x) {
    return std::make_unique<Widget>(x);
}

// factory-construct (kind 15) via make_shared.
std::shared_ptr<Widget> pool_make_shared(int x) {
    return std::make_shared<Widget>(x);
}

} // namespace graphlab
