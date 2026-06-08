#include "geometry.hpp"
#include <cmath>

namespace geo {

Shape::Shape(std::string name) : name_(std::move(name)) {}
Shape::~Shape() = default;

const std::string &Shape::name() const { return name_; }

Circle::Circle(std::string name, double radius)
    : Shape(std::move(name)), radius_(radius) {}

double Circle::area() const {
    return M_PI * radius_ * radius_;
}

// Uses the function template max_of<double>.
double widest(const std::vector<double> &xs) {
    double best = 0.0;
    for (double x : xs) {
        best = max_of(best, x);
    }
    return best;
}

}  // namespace geo
