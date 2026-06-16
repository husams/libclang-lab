// pipeline.hpp -- a DEEP call graph + a function over an ABSTRACT type.
#ifndef GRAPHLAB_PIPELINE_HPP
#define GRAPHLAB_PIPELINE_HPP

#include "shapes.hpp"

namespace app {

// Function taking an ABSTRACT class by reference and calling its methods:
// measure() -> Shape::area() + Shape::perimeter()  (both dynamic dispatch).
double measure(const geo::Shape& s);

// Deep call chain: run_pipeline -> stage_process -> transform -> normalize.
double normalize(double x);
double transform(double x);          // calls normalize() (twice)
double stage_process(double x);      // calls transform()
double run_pipeline(double seed);    // calls stage_process() TWICE + measure twice

// FUNCTION OVERLOADS: same name, different signatures. Each is a distinct symbol
// with its own USR, so a call resolves to exactly one overload (distinct edges).
int    scale(int x);                 // overload #1
double scale(double x);              // overload #2 (different parameter type)
int    scale(int x, int y);         // overload #3 (different arity)

// HEAP ALLOCATION: `new` invokes a constructor, `delete` invokes the (virtual)
// destructor. make_shape() uses `new`; consume() dispatches then `delete`s.
geo::Shape* make_shape(double r);    // `new geo::Circle(r)`  -> Circle ctor
double      consume(geo::Shape* s);  // s->area() then `delete s` -> ~Shape (virtual)

} // namespace app

#endif
