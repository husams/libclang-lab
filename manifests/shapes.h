#ifndef SHAPES_H
#define SHAPES_H

#include <stddef.h>

#define MAX_SHAPES 64
#define SQUARE(x) ((x) * (x))

/* A 2D point. */
typedef struct Point {
    double x;
    double y;
} Point;

/* Kinds of shapes we support. */
typedef enum ShapeKind {
    SHAPE_CIRCLE,
    SHAPE_RECTANGLE,
    SHAPE_TRIANGLE
} ShapeKind;

/* A shape: a tagged struct. */
typedef struct Shape {
    ShapeKind kind;
    Point origin;
    double dimensions[3];
    const char *name;
} Shape;

/* Compute the area of a shape. */
double shape_area(const Shape *s);

/* Translate a shape by (dx, dy). */
void shape_translate(Shape *s, double dx, double dy);

/* Sum the areas of an array of shapes. */
double shapes_total_area(const Shape *shapes, size_t count);

#endif /* SHAPES_H */
