#include <stdint.h>
#include "shapes.h"

#define VERSION 3
#define GREETING "hello"
#define ADD(a, b) ((a) + (b))
#define IS_DEBUG 0

#if IS_DEBUG
#define LOG(x) printf x
#else
#define LOG(x)
#endif

int versioned(void) {
    int v = ADD(VERSION, 1);
    return v;
}
