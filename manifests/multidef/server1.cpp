#include "context.hpp"
#include "helpers.hpp"

// Backend 1's implementation of the library-declared symbols.
void Context::reg() { helper_a(); }
int Context::count = seed_a();
