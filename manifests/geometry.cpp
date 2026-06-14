#include "geometry.hpp"
#include <cmath>

namespace geo {

Shape::Shape(std::string name) : name_(std::move(name)) {}
Shape::~Shape() = default;

const std::string &Shape::name() const { return name_; }

Circle::Circle(std::string name, double radius)
    : Shape(std::move(name)), radius_(radius) {}

double Circle::area() const { return M_PI * radius_ * radius_; }

// Uses the function template max_of<double>.
// Also instantiates the class template Box<T> with int, double, and Color.
// Box<Color> proves template_arg.ref_id joins to a real (enum) symbol (item 2).
double widest(const std::vector<double> &xs) {
  double best = 0.0;
  for (double x : xs) {
    best = max_of(best, x);
  }
  Box<int> bi(static_cast<int>(best));
  Box<double> bd(best);
  Box<Color> bc(Color::Red); // Color is a user-defined type -> ref_id IS NOT NULL
  (void)bc;
  return bd.get() + bi.get();
}

} // namespace geo
