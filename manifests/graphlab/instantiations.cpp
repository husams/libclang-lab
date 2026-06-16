// instantiations.cpp -- EXPLICIT template instantiations (a C++ construct in its
// own right, kept here so the project also exercises explicit instantiation).
//
// LIMITATION (documented): libclang does not expose the bodies of *instantiated*
// template methods, even when the instantiation is explicit. So the calls INSIDE
// Stack<int>::summary / Wrapper<int>::label are NOT recorded as edges -- the
// implicit instantiations show up only as call-target stubs in the graph. This
// is the cidx "limits of libclang" gotcha (see scripts/p6_limits.py).
//
// The cross-template call requirement IS captured through full SPECIALIZATIONS,
// whose bodies libclang DOES walk: Wrapper<bool>::label -> describe<bool> is a
// real `calls` edge (see containers.hpp).
#include "containers.hpp"

namespace cont {

template class Wrapper<int>;                       // -> Wrapper<int>::label
template class Stack<int>;                         // -> Stack<int>::summary
template std::string describe<int>(const int&);   // primary fn template, T=int
template int combine<int>(int, int);              // primary fn template, T=int

} // namespace cont
