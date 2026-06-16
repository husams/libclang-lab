// creatures.cpp -- definitions for the multiple-inheritance hierarchy.
#include "creatures.hpp"

namespace zoo {

Amphibian::Amphibian(int stamina) : stamina_(stamina) {}

int Amphibian::rest() { return stamina_ += 1; }

// walk() calls rest() -> a small intra-class call chain through a method.
int Amphibian::walk() { return rest() + 2; }

// swim() also calls rest(): rest() is therefore called from TWO methods.
int Amphibian::swim() { return rest() + 3; }

// Abstract-parameter functions: dispatch through the interface.
int make_it_walk(Walker& w) { return w.walk(); }
int make_it_swim(Swimmer& s) { return s.swim(); }

} // namespace zoo
