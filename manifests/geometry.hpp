#ifndef GEOMETRY_HPP
#define GEOMETRY_HPP

#include <string>
#include <vector>

namespace geo {

// Strongly-typed enumeration.
enum class Color { Red, Green, Blue };

// Abstract base class.
class Shape {
public:
    explicit Shape(std::string name);
    virtual ~Shape();

    virtual double area() const = 0;
    const std::string &name() const;

protected:
    std::string name_;
};

// Concrete shape deriving from Shape.
class Circle : public Shape {
public:
    Circle(std::string name, double radius);
    double area() const override;

private:
    double radius_;
};

// A function template.
template <typename T>
T max_of(const T &a, const T &b) {
    return a < b ? b : a;
}

// A class template.
template <typename T>
class Box {
public:
    explicit Box(T value) : value_(value) {}
    const T &get() const { return value_; }

private:
    T value_;
};

}  // namespace geo

#endif  // GEOMETRY_HPP
