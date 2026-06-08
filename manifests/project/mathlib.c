#include "mathlib.h"

int add(int a, int b) {
    return a + b;
}

int multiply(int a, int b) {
    return a * b;
}

int square(int x) {
    return multiply(x, x);   /* uses multiply() within the same TU */
}
