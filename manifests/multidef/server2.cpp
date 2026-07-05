#include "context.hpp"
#include "helpers.hpp"

// Backend 2's implementation of the SAME library-declared symbols.
void Context::reg() { helper_b(); }
int Context::count = seed_b();
