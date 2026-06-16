// shapes.hpp -- an abstract base + concrete shapes (dynamic dispatch).
#ifndef GRAPHLAB_SHAPES_HPP
#define GRAPHLAB_SHAPES_HPP

namespace geo {

// Abstract base: pure-virtual interface. A function taking `const Shape&` and
// calling area()/perimeter() exercises dynamic dispatch through an abstract type.
class Shape {
public:
    virtual ~Shape() = default;
    virtual double area() const = 0;       // pure virtual
    virtual double perimeter() const = 0;  // pure virtual
    // Non-pure virtual with a default body: overridden by some, not all.
    virtual const char* name() const { return "shape"; }
};

class Circle : public Shape {
    double r_;
public:
    explicit Circle(double r);
    double area() const override;        // declared here, defined in shapes.cpp
    double perimeter() const override;
    const char* name() const override;
};

class Rectangle : public Shape {
    double w_, h_;
public:
    Rectangle(double w, double h);
    double area() const override;
    double perimeter() const override;
    // intentionally does NOT override name() -> inherits Shape::name()
};

} // namespace geo

#endif
