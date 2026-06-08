#include <stdio.h>

static int leaf_a(int x) { return x + 1; }
static int leaf_b(int x) { return x * 2; }

static int mid(int x) {
    return leaf_a(x) + leaf_b(x);
}

static int recurse(int n) {
    if (n <= 1) return 1;
    return n * recurse(n - 1);   /* self-recursive */
}

int compute(int x) {
    int r = mid(x);
    r += recurse(x);
    return r;
}

int main(void) {
    printf("%d\n", compute(5));
    return 0;
}
