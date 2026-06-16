// pipeline.cpp -- the deep call graph and the abstract-parameter function.
#include "pipeline.hpp"

namespace app {

// Leaf of the call chain.
double normalize(double x) {
    return x < 0 ? -x : x;
}

// transform() calls normalize() in TWO places (same function, repeated callee).
double transform(double x) {
    double a = normalize(x);          // call site #1
    double b = normalize(x - 1.0);    // call site #2
    return a + b;
}

// stage_process() -> transform() -> normalize(): another link in the chain.
double stage_process(double x) {
    return transform(x) * 2.0;
}

// Dynamic dispatch through an abstract reference.
double measure(const geo::Shape& s) {
    return s.area() + s.perimeter();  // virtual calls on abstract Shape
}

// FUNCTION OVERLOADS -- three definitions sharing the name `scale`.
int    scale(int x)        { return x * 2; }
double scale(double x)     { return x * 2.0; }
int    scale(int x, int y) { return x * y; }

// HEAP ALLOCATION / DEALLOCATION.
// `new geo::Circle(r)` allocates and calls Circle's constructor.
geo::Shape* make_shape(double r) {
    return new geo::Circle(r);     // new -> geo::Circle::Circle
}
// `delete s` calls the (virtual) destructor through the base pointer.
double consume(geo::Shape* s) {
    double a = s->area();          // dynamic dispatch through a heap pointer
    delete s;                      // delete -> geo::Shape::~Shape (virtual)
    return a;
}

// run_pipeline() calls stage_process() TWICE (repeated call in one function),
// giving main -> run_pipeline -> stage_process -> transform -> normalize.
double run_pipeline(double seed) {
    double r = stage_process(seed);   // call site #1
    r += stage_process(seed + 10.0);  // call site #2
    return r;
}

} // namespace app
