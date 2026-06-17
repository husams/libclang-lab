// devirt3_caller.cpp -- Phase 3b cross-TU caller fixture.
//
// dispatch_param(A& a) is defined in devirt3.cpp; its ONLY caller is here.
// After `cidx resolve`, call_sites_into(dispatch_param) finds run_cross_tu,
// whose arg-0 provenance is construct/local B.  Under assume_closed_world=True
// the a.rank() site in dispatch_param narrows to {B::rank}.
#include "devirt3.hpp"

namespace graphlab {

void run_cross_tu() {
    chain::B b;
    dispatch_param(b);   // sole caller; passes B by value
}

} // namespace graphlab
