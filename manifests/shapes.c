#include "shapes.h"
#include <math.h>
#include <stdarg.h>

#define PI 3.14159265358979323846

/* File-local helper: not visible outside this translation unit. */
static double circle_area(double radius) {
    return PI * SQUARE(radius);
}

double shape_area(const Shape *s) {
    switch (s->kind) {
        case SHAPE_CIRCLE:
            return circle_area(s->dimensions[0]);
        case SHAPE_RECTANGLE:
            return s->dimensions[0] * s->dimensions[1];
        case SHAPE_TRIANGLE:
            return 0.5 * s->dimensions[0] * s->dimensions[1];
        default:
            return 0.0;
    }
}

void shape_translate(Shape *s, double dx, double dy) {
    s->origin.x += dx;
    s->origin.y += dy;
}

double shapes_total_area(const Shape *shapes, size_t count) {
    double total = 0.0;
    for (size_t i = 0; i < count; i++) {
        total += shape_area(&shapes[i]);
    }
    return total;
}

/* A variadic helper: average of n doubles. */
double average(int n, ...) {
    va_list args;
    va_start(args, n);
    double sum = 0.0;
    for (int i = 0; i < n; i++) {
        sum += va_arg(args, double);
    }
    va_end(args);
    return n > 0 ? sum / n : 0.0;
}
