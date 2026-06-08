#include "mathlib.h"
#include <stdio.h>

int main(void) {
    int s = square(5);
    int t = add(s, multiply(2, 3));   /* multiply() used in a second TU */
    printf("%d %d\n", s, t);
    return 0;
}
