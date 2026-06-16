// chain.cpp -- definitions for the A <- B <- C <- D inheritance chain.
#include "chain.hpp"

namespace chain {

int A::rank() const { return 0; }
int B::rank() const { return 1; }
int C::rank() const { return 2; }
int D::rank() const { return 3; }

int top_rank(const A& a) { return a.rank(); }   // dynamic dispatch over the chain

} // namespace chain
