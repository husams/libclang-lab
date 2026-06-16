// shapes.cpp -- definitions for shapes.hpp (decl-in-header / def-in-.cpp split).
#include "shapes.hpp"

namespace geo {

static constexpr double kPi = 3.14159265358979323846;

Circle::Circle(double r) : r_(r) {}
double Circle::area() const { return kPi * r_ * r_; }
double Circle::perimeter() const { return 2.0 * kPi * r_; }
const char* Circle::name() const { return "circle"; }

Rectangle::Rectangle(double w, double h) : w_(w), h_(h) {}
double Rectangle::area() const { return w_ * h_; }
double Rectangle::perimeter() const { return 2.0 * (w_ + h_); }

} // namespace geo
